"""
collection/site_collector.py

Playwright-based alternative to
scrapers.own_website_scraper.OwnWebsiteScraper -- executes JS, takes
real screenshots, and follows a broader page-keyword heuristic (adds
"product"/"catalog"/"download"/"cert" to own_website_scraper's own
capability-page keywords), routed through an injectable rotating-proxy
provider. Output (collection.schemas.CollectionResult/CollectedPage) is
deliberately duck-type compatible with OwnWebsiteFetchResult/
OwnWebsitePage -- see collection/schemas.py's own docstring -- so this
is an additive, injectable alternative reachable through the same seam
(verification.capability_extractor.CapabilityExtractor.extract_from_pages
and verification.website_contact_extractor.extract_contact_details both
already accept anything with `.url`/`.text`), not a forced replacement.
OwnWebsiteScraper stays the cheap httpx-only default for the existing
extract-capabilities pipeline stage.

Sync Playwright API, not async -- see the redesign plan
(.claude/plans/deep-wibbling-rivest.md) for the concrete reasoning:
api/jobs.py's background jobs already run on a worker thread via
Starlette's BackgroundTasks (confirmed: run_in_threadpool), so a
sync_playwright() context confined to one such job call is Playwright's
own supported usage pattern, and the codebase is otherwise 100%
synchronous outside FastAPI's own request lifecycle.

Which pages get visited
------------------------
Homepage, plus internal links whose href/anchor text matches
own_website_scraper's own capability keywords ("about", "capabilit",
"manufactur", "factory", "facilit", "production", "quality",
"certificat", "workshop", "contact") EXTENDED with "product", "catalog",
"download", "cert", and a company-history tier ("history", "heritage",
"legacy", "commitment", "responsibility", "milestone", "story") --
richer than OwnWebsiteScraper's own set since Collection Service's
brief explicitly wants product pages and downloads/catalogues, not
just capability-adjacent pages.

Homepage-anchor discovery (_find_relevant_links) is the primary path,
but a real gap was found live: a page can be linked from the homepage
nav yet match none of these keywords in either its URL or anchor text
(Mansfield Engineered Components' "Commitment" and "Single Source
Responsibility" pages -- the only pages on the whole site that said the
company does metal stamping). _fetch_sitemap_page_urls supplements
discovery with whatever /sitemap.xml (or /sitemap_index.xml) lists,
relevance-filtered the same way, so a page the homepage's own anchor
text doesn't name usefully still gets a chance.

_find_image_urls/_has_contact_form are reimplemented here (not imported
from OwnWebsiteScraper) because they're private instance methods on
that class, not standalone functions -- reaching across to call another
class's private methods would be more fragile than the small amount of
duplication this avoids. html_to_text IS a standalone module function
there and is imported directly.

Iframe-embedded contact widgets
--------------------------------
page.content() only ever returns the MAIN document's HTML. A large
modern enterprise site's actual contact mechanism is very commonly a
third-party form widget (Marketo/HubSpot/etc.) embedded via <iframe> --
a genuinely separate document the main HTML never contains. Found
live: nVent's real "Contact Us" page was correctly discovered and
visited (481 relevant links found, this exact page prioritised first)
but still produced zero contact info and no detected contact form.
_collect_iframe_html reads every child frame Playwright already has
attached (page.frames) and feeds that into CONTACT extraction only
(html_to_text/_has_contact_form/_find_mailto_emails/_find_tel_phones)
-- never into image/social/download-link/facility-photo extraction,
which need each fragment's own correct base URL and would be actively
wrong applied against an iframe's separate origin.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from pathlib import Path
from typing import Any, List, Optional, Tuple

from bs4 import BeautifulSoup

from collection.artifact_store import ArtifactStore
from collection.proxy_provider import NoProxyProvider, ProxyProvider
from collection.schemas import CertificateDocument, CollectedPage, CollectionResult
from config.settings import COLLECTION_PAGE_TIMEOUT_MS, MAX_CERTIFICATE_DOWNLOADS
from scrapers.own_website_scraper import html_to_text

logger = logging.getLogger(__name__)

# own_website_scraper's own capability-page keywords, extended with
# product/catalogue/download/certification terms -- see module
# docstring for why Collection Service's page selection is broader.
_RELEVANT_LINK_KEYWORDS: Tuple[str, ...] = (
    "about", "company", "capabilit", "manufactur", "factory", "facilit", "production",
    "quality", "certificat", "workshop", "contact",
    "product", "catalog", "catalogue", "download", "cert",
    "impressum", "imprint",  # legal-disclosure page (DE/AT/CH etc.) -- a
                              # reliable address source batch_service.py's
                              # address extraction looks for specifically.
    "history", "heritage", "legacy", "commitment", "responsibility", "milestone", "story",
    # A company-history/heritage page is real evidence-bearing content
    # (founding date, when a capability was added, headcount) that
    # neither a generic "about" keyword nor anchor text catches once a
    # site names it something else -- found live: Mansfield Engineered
    # Components' own "Commitment" (history) and "Single Source
    # Responsibility" pages, neither of which contain "about",
    # "capabilit", or "manufactur" in URL or anchor text, ended up the
    # ONLY pages on the whole site that actually said the company does
    # metal stamping -- a real supplier nearly got tagged
    # "miscategorised" over a page-discovery gap, not a real absence of
    # evidence. See _fetch_sitemap_page_urls below for the other half
    # of this fix -- these pages weren't linked with matching anchor
    # text from the homepage at all, only reachable via the sitemap.
)
# "company" (added alongside "about" -- gap found auditing Nifco/nifco.com):
# batch_service.py's _address_candidate_sources' about-tier already
# matches "about" OR "company" in a page's URL, but this list -- which
# decides what even gets DISCOVERED as a candidate link in the first
# place -- was missing "company" entirely. A site whose company-profile
# page URL contains "company" but not "about" (nifco.com/company/
# overview.html, non-Latin anchor text so the anchor-text half of the
# match can't save it either) was never being visited at all, address
# extraction or not.

# Subset of _RELEVANT_LINK_KEYWORDS that reliably carries an address --
# the exact tiers batch_service.py's _address_candidate_sources looks
# for (contact page, impressum/imprint page, about/company page). Used
# by _prioritise_relevant_links to give these first claim on the page
# budget below -- see that function's own docstring for why this
# exists (a real gap-analysis finding: a genuine contact page was
# consistently losing its budget slot to blog/product links that
# merely appeared earlier in the homepage's HTML).
_PRIORITY_LINK_KEYWORDS: Tuple[str, ...] = (
    "contact", "about", "company", "impressum", "imprint",
    "history", "heritage", "legacy", "commitment", "responsibility", "milestone", "story",
)

# Same non-facility-image filter own_website_scraper._find_image_urls uses.
_NON_FACILITY_IMAGE_KEYWORDS: Tuple[str, ...] = (
    "logo", "icon", "favicon", "avatar", "sprite", "pixel", "tracking",
    "banner", "badge", "button", "arrow", "bullet",
)
_MAX_IMAGES_PER_PAGE = 5

_CONTACT_FIELD_KEYWORDS: Tuple[str, ...] = (
    "name", "email", "message", "phone", "enquiry", "inquiry", "subject",
)

_SOCIAL_DOMAINS: Tuple[str, ...] = (
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "youtube.com", "wechat.com", "weibo.com", "tiktok.com",
)

_DOWNLOAD_EXTENSIONS: Tuple[str, ...] = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx")

_MAX_PAGES_DEFAULT = 6

# How long to wait for <body> after "domcontentloaded" -- see
# _visit_and_collect's own comment for why "load" (waiting for every
# image/tracker to finish) is the wrong default for image-heavy
# factory sites; this is a cheap best-effort sanity check that the DOM
# actually rendered something, not a substitute for it.
_BODY_SELECTOR_WAIT_MS = 3000

# Certificate/quality-standard document detection (Procurement Decision
# Engine Phase 3) -- matched against download_links' URL/filename text
# alone, not page content (these are links to separate files, not pages
# this collector fetches text for).
_CERTIFICATE_KEYWORDS: Tuple[str, ...] = (
    "iso", "iatf", "ts16949", "ce", "rohs", "reach", "ul", "e-mark", "emark", "ohsas", "certificate",
)


def _build_candidate_urls(domain: str, source_url: Optional[str]) -> List[str]:
    """Ordered, deduplicated base-URL candidates to try for a bare
    hostname. Many hosts (a lot of Chinese-hosted sites in particular)
    only resolve on www -- blindly upgrading a discovered
    "http://www.X" to "https://X" (dropping www AND forcing https in
    one step) breaks those. So: the URL exactly as it was originally
    given (if any -- e.g. a CSV row's raw website column), then
    https://www.X, then https://X, then http://www.X, cheapest/most
    likely first. www is never stripped unless a bare-host candidate
    actually loads.

    Only called for a bare hostname -- see collect()'s scheme check;
    a caller-supplied `domain` that's already a full URL (e.g. a test
    server, or an explicit override) is used exactly as given instead,
    with nothing to guess."""
    candidates: List[str] = []
    if source_url:
        candidates.append(
            source_url if source_url.startswith(("http://", "https://")) else f"https://{source_url}"
        )
    bare = domain[4:] if domain.lower().startswith("www.") else domain
    candidates += [f"https://www.{bare}", f"https://{bare}", f"http://www.{bare}"]

    seen: set = set()
    ordered: List[str] = []
    for candidate in candidates:
        key = candidate.rstrip("/").lower()
        if key not in seen:
            seen.add(key)
            ordered.append(candidate)
    return ordered


def _find_certificate_candidates(download_links: List[str]) -> List[Tuple[str, str]]:
    """Returns (url, matched_keyword) pairs, first-match-wins per URL,
    in encounter order, deduplicated."""
    candidates: List[Tuple[str, str]] = []
    seen: set = set()
    for url in download_links:
        if url in seen:
            continue
        haystack = url.lower()
        for keyword in _CERTIFICATE_KEYWORDS:
            if keyword in haystack:
                seen.add(url)
                candidates.append((url, keyword))
                break
    return candidates


def _find_relevant_links(base_url: str, html: str) -> List[str]:
    """Same-domain links matching _RELEVANT_LINK_KEYWORDS -- excludes
    download-extension links (a "catalog.pdf" link matches the
    "catalog" keyword too, but must be recorded via
    _find_download_links, not navigated to as if it were an HTML page)."""
    soup = BeautifulSoup(html, "html.parser")
    found: List[str] = []
    seen: set = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if any(href.lower().split("?")[0].endswith(ext) for ext in _DOWNLOAD_EXTENSIONS):
            continue
        anchor_text = (anchor.get_text() or "").strip().lower()
        haystack = f"{href.lower()} {anchor_text}"
        if not any(keyword in haystack for keyword in _RELEVANT_LINK_KEYWORDS):
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        parsed_base = urllib.parse.urlsplit(base_url)
        parsed_link = urllib.parse.urlsplit(absolute)
        if parsed_link.netloc and parsed_link.netloc != parsed_base.netloc:
            continue  # never follow off-domain links for page navigation
        normalised = absolute.split("#")[0]
        if normalised not in seen:
            seen.add(normalised)
            found.append(normalised)
    return found


def _prioritise_relevant_links(links: List[str]) -> List[str]:
    """Re-orders _find_relevant_links' output so contact/about/
    impressum-tier pages (_PRIORITY_LINK_KEYWORDS) get first claim on
    _collect_with's page budget (_MAX_PAGES_DEFAULT) -- previously
    visited in whatever order they appeared in the homepage's HTML, so
    a real contact page reachable in one click could still lose its
    budget slot to five blog/product links that merely appeared earlier
    in the page source. Stable sort: within each tier, original
    discovery order is preserved, so this only ever reorders, never
    drops, a candidate."""
    return sorted(links, key=lambda link: 0 if any(k in link.lower() for k in _PRIORITY_LINK_KEYWORDS) else 1)


# WordPress/Yoast-style sitemap locations, cheapest/most common first --
# tried the same way _build_candidate_urls tries homepage variants: stop
# at the first one that actually returns content.
_SITEMAP_PATHS: Tuple[str, ...] = ("/sitemap.xml", "/sitemap_index.xml")

# A sitemap INDEX (root <sitemapindex> wrapping several <sitemap><loc>
# entries pointing at other feeds -- the common WordPress/Yoast shape,
# e.g. mansfieldec.com/sitemap.xml wrapping sitemap-misc.xml/
# page-sitemap.xml/feeds/sitemap.xml) is followed one level deep, capped
# here so a site with dozens of paginated post-sitemaps can't consume
# the whole page-visit budget on sitemap fetches before a single real
# page is even visited.
_MAX_SITEMAP_SUB_FEEDS = 5


def _extract_sitemap_locs(xml_text: str) -> List[str]:
    """Every <loc> URL in a sitemap or sitemap-index XML document, in
    document order. A plain regex, not an XML parser -- a sitemap feed
    is simple enough (no attributes worth reading, no structure beyond
    the wrapping <urlset>/<sitemapindex> tag) that pulling in a real XML
    parser isn't worth it, and a mildly malformed real-world feed (a
    stray unescaped character elsewhere in the document) still degrades
    gracefully here instead of raising."""
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml_text, flags=re.IGNORECASE)


def _fetch_sitemap_page_urls(context: Any, base_url: str, timeout_ms: int) -> List[str]:
    """Same-domain page URLs discovered via /sitemap.xml (or
    /sitemap_index.xml), fetched as a raw HTTP GET on the already-open
    browser context (context.request, same technique
    _download_certificates uses) rather than a full page navigation --
    a sitemap is a plain XML file with nothing to render, so it doesn't
    cost a page-visit-budget slot to check.

    Strictly an additive fallback alongside _find_relevant_links'
    homepage-anchor discovery, which stays the primary path -- this
    exists for pages no homepage link reaches with matching URL/anchor
    text at all. Found live: Mansfield Engineered Components' own
    "Commitment" (company history) and "Single Source Responsibility"
    pages are both linked from its homepage nav, but neither the URL
    slug nor the anchor text matched any capability keyword, so
    _find_relevant_links never surfaced them -- yet those two pages
    were the ONLY place on the entire site that said the company does
    metal stamping. The sitemap lists every page regardless of how (or
    whether) the homepage links to it, so it catches exactly this gap.

    Never raises -- a missing, 404, or malformed sitemap is a normal,
    common case, not an error worth surfacing."""
    parsed_base = urllib.parse.urlsplit(base_url)
    root = f"{parsed_base.scheme}://{parsed_base.netloc}"

    def _fetch(url: str) -> str:
        try:
            response = context.request.get(url, timeout=timeout_ms)
            if not response.ok:
                return ""
            return response.text()
        except Exception:
            return ""

    xml_text = ""
    for path in _SITEMAP_PATHS:
        xml_text = _fetch(root + path)
        if xml_text:
            break
    if not xml_text:
        return []

    locs = _extract_sitemap_locs(xml_text)
    if "<sitemapindex" in xml_text.lower():
        page_urls: List[str] = []
        for sub_feed_url in locs[:_MAX_SITEMAP_SUB_FEEDS]:
            sub_xml = _fetch(urllib.parse.urljoin(root, sub_feed_url))
            if sub_xml:
                page_urls.extend(_extract_sitemap_locs(sub_xml))
        locs = page_urls

    same_domain: List[str] = []
    seen: set = set()
    for loc in locs:
        absolute = urllib.parse.urljoin(root, loc)
        parsed = urllib.parse.urlsplit(absolute)
        if parsed.netloc != parsed_base.netloc:
            continue  # never follow off-domain links, same rule _find_relevant_links applies
        normalised = absolute.split("#")[0]
        if normalised not in seen:
            seen.add(normalised)
            same_domain.append(normalised)
    return same_domain


def _filter_relevant_sitemap_urls(urls: List[str]) -> List[str]:
    """Same _RELEVANT_LINK_KEYWORDS filter _find_relevant_links applies
    to homepage anchors (href + anchor text), but against the URL alone
    -- a sitemap entry carries no anchor text to match against."""
    return [u for u in urls if any(k in u.lower() for k in _RELEVANT_LINK_KEYWORDS)]


def _extract_footer_text(html: str) -> str:
    """Text content of the page's <footer> element, if any -- company
    address/registration details are disproportionately likely to live
    here. Empty string if no <footer> tag is present, matching
    html_to_text's own "nothing found" convention -- a natural fit for
    batch_service.py's address-extraction candidate-building, which
    treats an empty string as "this tier has no candidate."""
    soup = BeautifulSoup(html, "html.parser")
    footer = soup.find("footer")
    if footer is None:
        return ""
    text = footer.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _find_image_urls(base_url: str, html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    found: List[str] = []
    seen: set = set()
    for img in soup.find_all("img", src=True):
        src = img["src"]
        if any(keyword in src.lower() for keyword in _NON_FACILITY_IMAGE_KEYWORDS):
            continue
        absolute = urllib.parse.urljoin(base_url, src)
        normalised = absolute.split("#")[0]
        if normalised.startswith(("http://", "https://")) and normalised not in seen:
            seen.add(normalised)
            found.append(normalised)
        if len(found) >= _MAX_IMAGES_PER_PAGE:
            break
    return found



# Checked against an <img>'s own alt text -- a stronger, per-image
# signal than the page-level check below, since it names the specific
# photo rather than just the page it's on.
_FACILITY_PHOTO_ALT_KEYWORDS: Tuple[str, ...] = (
    "factory", "workshop", "production line", "production facility",
    "our plant", "manufacturing plant", "assembly line", "factory floor",
    "shop floor", "warehouse",
)

# Checked against the PAGE's own URL -- an image on a page that's
# itself about the factory/facility (an About Us / Facility page) is a
# plausible facility photo even without matching alt text, since alt
# text is frequently missing or generic ("image1.jpg") on real sites.
_FACILITY_PAGE_URL_KEYWORDS: Tuple[str, ...] = (
    "factory", "facilit", "about", "production", "workshop", "plant",
)

_MAX_FACILITY_PHOTOS_PER_PAGE = 5


def _extract_facility_photo_urls(base_url: str, html: str) -> List[str]:
    """Heuristic candidate factory/facility photos on this page -- for
    the buyer's own manual reverse-image-search review (criterion C),
    never an automated verdict about whether a photo is genuine. An
    image counts as a candidate if its own alt text names a facility
    (_FACILITY_PHOTO_ALT_KEYWORDS), OR it appears on a page that's
    itself facility-flavoured (_FACILITY_PAGE_URL_KEYWORDS) -- the
    page-level fallback exists because alt text alone would miss most
    real photos (frequently missing or generic), and this is meant to
    also catch an image gallery on an About Us/Facility page even when
    each individual <img> tag says nothing useful on its own.
    Deliberately approximate -- reuses the same logo/icon exclusion
    filter _find_image_urls already established rather than trying to
    be precise here, since the actual verification step is a human
    opening each URL, not this heuristic."""
    page_is_facility_flavoured = any(kw in base_url.lower() for kw in _FACILITY_PAGE_URL_KEYWORDS)
    soup = BeautifulSoup(html, "html.parser")
    found: List[str] = []
    seen: set = set()
    for img in soup.find_all("img", src=True):
        src = img["src"]
        if any(keyword in src.lower() for keyword in _NON_FACILITY_IMAGE_KEYWORDS):
            continue
        alt_text = (img.get("alt") or "").lower()
        alt_matches = any(keyword in alt_text for keyword in _FACILITY_PHOTO_ALT_KEYWORDS)
        if not (alt_matches or page_is_facility_flavoured):
            continue
        absolute = urllib.parse.urljoin(base_url, src)
        normalised = absolute.split("#")[0]
        if normalised.startswith(("http://", "https://")) and normalised not in seen:
            seen.add(normalised)
            found.append(normalised)
        if len(found) >= _MAX_FACILITY_PHOTOS_PER_PAGE:
            break
    return found


# Cap on how many child frames get read per page -- a pathological
# page (ad-heavy, many embedded widgets) shouldn't be able to blow up
# the time budget just fetching frame content. Real corporate contact/
# chat/form widgets are a handful at most; found live neither nVent
# nor Tratos needed anywhere close to this to have their real contact
# widget's frame included.
_MAX_IFRAMES_PER_PAGE = 10


def _collect_iframe_html(page: Any) -> str:
    """Concatenated HTML of every child frame (iframe) attached to
    `page` -- e.g. a Marketo/HubSpot/embedded contact-form widget --
    for CONTACT extraction only (html_to_text/_has_contact_form/
    _find_mailto_emails/_find_tel_phones), never for image/social/
    download-link/facility-photo extraction, which need each
    fragment's own correct base URL for resolving relative hrefs and
    would be actively wrong applied against an iframe's separate
    origin.

    Real gap found live: nVent's real "Contact Us" page (correctly
    discovered and visited -- 481 relevant links found, this exact
    page prioritised first) still produced zero extracted contact
    info and no detected contact form. page.content() only returns
    the MAIN document's HTML -- a large modern enterprise site's
    actual contact mechanism is very commonly a third-party form
    widget embedded via <iframe>, a genuinely separate document
    page.content() never sees at all, not a rendering-timing issue
    _BODY_SELECTOR_WAIT_MS could fix.

    Playwright's page.frames already includes every attached frame
    (same-origin or cross-origin -- unlike a same-origin-policy-
    restricted plain JS `document`, this operates via CDP with full
    page access), so no extra navigation/fetch is needed here, just
    reading what's already loaded. Never raises: a frame that can't be
    read (detached, mid-navigation, or any other transient state) is
    skipped, same per-item fault isolation as every other step in this
    module -- one bad frame must never lose the rest of the page's
    real content."""
    try:
        frames = page.frames
    except Exception:
        return ""
    fragments: List[str] = []
    for frame in frames:
        if frame is page.main_frame:
            continue
        if len(fragments) >= _MAX_IFRAMES_PER_PAGE:
            break
        try:
            fragments.append(frame.content())
        except Exception:
            continue
    return "\n".join(fragments)


def _has_contact_form(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    for form in soup.find_all("form"):
        for field_tag in form.find_all(("input", "textarea")):
            haystack = " ".join(
                str(field_tag.get(attr, "")) for attr in ("name", "id", "placeholder", "type")
            ).lower()
            if any(keyword in haystack for keyword in _CONTACT_FIELD_KEYWORDS):
                return True
    return False


def _find_social_links(html: str) -> List[str]:
    """Outbound links to known social platforms -- never followed, only
    recorded (same "link only, never scrape/log in" discipline
    verification.linkedin_presence already establishes for LinkedIn,
    generalised here to every platform)."""
    soup = BeautifulSoup(html, "html.parser")
    found: List[str] = []
    seen: set = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if any(domain in href.lower() for domain in _SOCIAL_DOMAINS):
            if href not in seen:
                seen.add(href)
                found.append(href)
    return found


def _find_download_links(base_url: str, html: str) -> List[str]:
    """Absolute URLs of PDF/Office-document links -- catalogues, spec
    sheets, certificates."""
    soup = BeautifulSoup(html, "html.parser")
    found: List[str] = []
    seen: set = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if any(href.lower().split("?")[0].endswith(ext) for ext in _DOWNLOAD_EXTENSIONS):
            absolute = urllib.parse.urljoin(base_url, href)
            if absolute not in seen:
                seen.add(absolute)
                found.append(absolute)
    return found


def _find_mailto_emails(html: str) -> List[str]:
    """Raw `mailto:` href values (the address only -- scheme and any
    `?subject=`/`?body=` query string stripped) -- feeds
    verification.website_contact_extractor.extract_mailto_emails via
    CollectedPage.mailto_emails. A real, common pattern this exists
    for: a page's VISIBLE link text is "Click Here" or an icon
    (deters naive text-scraping) while the real address sits untouched
    in the href for any actual browser -- invisible to a regex scan
    over rendered text alone (see extract_mailto_emails's docstring
    for the real site that surfaced this)."""
    soup = BeautifulSoup(html, "html.parser")
    found: List[str] = []
    seen: set = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if href.lower().startswith("mailto:"):
            address = href[len("mailto:"):].split("?")[0].strip()
            if address and address not in seen:
                seen.add(address)
                found.append(address)
    return found


def _find_tel_phones(html: str) -> List[str]:
    """Raw `tel:` href values (scheme and any trailing query string
    stripped) -- feeds verification.website_contact_extractor.
    extract_tel_phones via CollectedPage.tel_phones. Same rationale as
    _find_mailto_emails: a `tel:` href is machine-readable even when
    the visible link text isn't a plain number ("Call Us", an icon)."""
    soup = BeautifulSoup(html, "html.parser")
    found: List[str] = []
    seen: set = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if href.lower().startswith("tel:"):
            number = href[len("tel:"):].split("?")[0].strip()
            if number and number not in seen:
                seen.add(number)
                found.append(number)
    return found


class SiteCollector:

    def __init__(
        self,
        proxy_provider: Optional[ProxyProvider] = None,
        artifact_store: Optional[ArtifactStore] = None,
        max_pages: int = _MAX_PAGES_DEFAULT,
        page_timeout_ms: int = COLLECTION_PAGE_TIMEOUT_MS,
        playwright_factory: Optional[Any] = None,
    ):
        self.proxy_provider = proxy_provider or NoProxyProvider()
        self.artifact_store = artifact_store or ArtifactStore()
        self.max_pages = max_pages
        self.page_timeout_ms = page_timeout_ms
        # Injectable for tests -- a zero-arg callable returning a
        # sync_playwright()-context-manager-shaped object (anything
        # with `.chromium.launch(...)`), so tests don't need a real
        # browser launch just to exercise extraction logic. Production
        # code leaves this None and uses the real sync_playwright().
        self._playwright_factory = playwright_factory

    def _launch(self, playwright: Any) -> Tuple[Any, Any]:
        proxy_config = self.proxy_provider.get_proxy_config()
        launch_kwargs: dict = {"headless": True}
        if proxy_config:
            launch_kwargs["proxy"] = proxy_config
        browser = playwright.chromium.launch(**launch_kwargs)
        context = browser.new_context()
        context.set_default_timeout(self.page_timeout_ms)
        return browser, context

    def collect(self, supplier_id: int, domain: str, source_url: Optional[str] = None) -> CollectionResult:
        """Never raises -- a single supplier's collection failure must
        never abort a batch run (same discipline as every other
        pipeline stage in this codebase).

        `source_url`: the raw website string as originally given (e.g.
        a CSV row's website column), tried first among the homepage
        candidates -- see _build_candidate_urls. Optional: callers that
        only have a bare `domain` on file (collect_pending(), a direct
        `main.py collect <id>`) simply omit it and fall back to the
        generated www/scheme permutations."""
        if not domain:
            return CollectionResult(domain=domain, success=False, error="no domain provided")

        if domain.startswith(("http://", "https://")):
            # Caller already gave a full URL (e.g. a test server) --
            # nothing to disambiguate, use it exactly as given.
            candidates = [domain]
        else:
            candidates = _build_candidate_urls(domain, source_url)

        run_dir = self.artifact_store.new_run_dir(supplier_id)
        relative_dir = self.artifact_store.relative_path(run_dir)
        provider_name = type(self.proxy_provider).__name__

        try:
            if self._playwright_factory is not None:
                return self._collect_with(self._playwright_factory(), candidates, domain, run_dir, relative_dir, provider_name)
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                return self._collect_with(p, candidates, domain, run_dir, relative_dir, provider_name)
        except Exception as e:  # noqa: BLE001 -- one supplier's collection must never abort a batch
            logger.error("collection: unexpected error collecting %s: %s", domain, e)
            return CollectionResult(
                domain=domain, success=False, error=str(e),
                artifacts_dir=relative_dir, proxy_provider=provider_name,
            )

    def _collect_with(
        self, playwright: Any, candidates: List[str], domain: str, run_dir: Path, relative_dir: str, provider_name: str,
    ) -> CollectionResult:
        browser, context = self._launch(playwright)
        try:
            pages: List[CollectedPage] = []
            page = context.new_page()

            homepage = None
            base_url = None
            for candidate in candidates:
                homepage = self._visit_and_collect(page, candidate, 0, run_dir)
                if homepage is not None:
                    base_url = candidate
                    break

            if homepage is None:
                return CollectionResult(
                    domain=domain, success=False,
                    error=f"could not load homepage -- tried {', '.join(candidates)}",
                    artifacts_dir=relative_dir, proxy_provider=provider_name,
                )
            if len(candidates) > 1:
                logger.info("collection: %s resolved via %s", domain, base_url)
            homepage_page, homepage_html = homepage
            pages.append(homepage_page)

            # homepage_page.url (the real post-redirect location, see
            # _visit_and_collect), not `base_url` (the pre-redirect
            # candidate that was requested) -- see _visit_and_collect's
            # own comment for why this matters for the same-domain check.
            relevant_links = _find_relevant_links(homepage_page.url, homepage_html)

            # Sitemap-discovered pages are a supplementary source, not a
            # replacement -- homepage-anchor discovery above stays
            # primary and keeps its original order; anything the
            # sitemap finds that isn't already in that list is appended
            # (still relevance-filtered) so it competes fairly in
            # _prioritise_relevant_links below rather than jumping the
            # whole queue. See _fetch_sitemap_page_urls for why this
            # exists.
            sitemap_urls = _fetch_sitemap_page_urls(context, homepage_page.url, self.page_timeout_ms)
            for url in _filter_relevant_sitemap_urls(sitemap_urls):
                if url not in relevant_links:
                    relevant_links.append(url)

            relevant_links = _prioritise_relevant_links(relevant_links)
            # Real diagnostic value beyond this one investigation: a
            # supplier reporting "success" with suspiciously little
            # extracted (e.g. neither contact info nor a contact form
            # found despite visiting every page in the budget) is
            # otherwise a dead end to debug -- there was no visibility
            # into whether the crawl found zero candidate links at all,
            # or found some but they turned out to have nothing useful,
            # without this. homepage_html's length is included since a
            # near-empty relevant_links list and a near-empty homepage
            # both point at "page didn't really render", while a
            # substantial homepage with zero relevant links points at a
            # real _find_relevant_links/keyword-matching gap instead.
            # WARNING, not INFO -- production's configured log level
            # (SUPPLIER_INTEL_LOG_LEVEL) filters out INFO entirely,
            # confirmed live: this line never appeared while the
            # existing WARNING-level "could not load homepage" lines
            # elsewhere in this module always do. A diagnostic line that
            # never actually surfaces anywhere real is worse than
            # useless -- it looks like coverage that isn't there.
            logger.warning(
                "collection: %s -- %d relevant link(s) found (homepage html %d chars): %s",
                domain, len(relevant_links), len(homepage_html), relevant_links[:10],
            )
            for i, link in enumerate(relevant_links, start=1):
                if len(pages) >= self.max_pages:
                    break
                visited = self._visit_and_collect(page, link, i, run_dir)
                if visited is not None:
                    pages.append(visited[0])

            certificate_documents = self._download_certificates(context, run_dir, pages)

            return CollectionResult(
                domain=domain, pages=pages, success=True,
                artifacts_dir=relative_dir, proxy_provider=provider_name,
                certificate_documents=certificate_documents,
                resolved_url=base_url,
            )
        finally:
            browser.close()

    def _download_certificates(
        self, context: Any, run_dir: Path, pages: List[CollectedPage],
    ) -> List[CertificateDocument]:
        """Downloads up to MAX_CERTIFICATE_DOWNLOADS certificate-looking
        files found across all collected pages' download_links, via a
        raw HTTP GET on the already-open browser context
        (context.request) -- no extra page navigation needed. One
        file failing (bad URL, timeout, non-2xx) is caught and skipped,
        never aborts collection -- same per-item fault isolation as
        every other step in this codebase."""
        all_download_links = [link for page in pages for link in page.download_links]
        candidates = _find_certificate_candidates(all_download_links)[:MAX_CERTIFICATE_DOWNLOADS]
        documents: List[CertificateDocument] = []
        for url, keyword in candidates:
            try:
                response = context.request.get(url)
                if not response.ok:
                    continue
                filename = url.split("?")[0].rstrip("/").split("/")[-1] or "certificate"
                saved_path = self.artifact_store.save_download(run_dir, filename, response.body())
                documents.append(CertificateDocument(
                    url=url, matched_keyword=keyword, filename=filename,
                    artifact_path=str(saved_path.relative_to(run_dir)),
                ))
            except Exception as e:
                logger.warning("collection: certificate download failed for %s: %s", url, e)
        return documents

    def _visit_and_collect(
        self, page: Any, url: str, index: int, run_dir: Path,
    ) -> Optional[Tuple[CollectedPage, str]]:
        try:
            # "domcontentloaded", not the default "load" -- "load" waits
            # for every image/tracker/analytics request on the page to
            # finish, and a lot of the real target sites here (Chinese
            # factory sites especially) are image-heavy enough that this
            # alone was eating the whole page_timeout_ms budget and
            # reporting a false failure on an otherwise-reachable site
            # (an ordinary GET returns the document in well under a
            # second). We only need the HTML document itself.
            response = page.goto(url, timeout=self.page_timeout_ms, wait_until="domcontentloaded")
        except Exception as e:
            logger.warning("collection: failed to load %s: %s", url, e)
            return None

        # page.goto() only raises for a network-level failure (DNS,
        # timeout, connection refused) -- an HTTP error/block response
        # still "loads" successfully as far as Playwright is concerned,
        # since there IS a valid document, just not the real site.
        # Found live: northumbrianroads.co.uk's WAF returns a real, tiny
        # "403 - Forbidden" HTML page (own <title> says so) for this
        # collector's traffic -- the goto() succeeded, the 403 page's
        # thin content correctly had zero relevant links, so the crawl
        # silently stopped at 1 page and reported success with no real
        # data at all, rather than the genuine failure it actually was.
        # Same treatment as a network-level failure (return None) --
        # for the HOMEPAGE this feeds the same "could not load
        # homepage" fallback the next candidate URL already uses; for a
        # SECONDARY page it's simply skipped rather than counted as
        # visited with a WAF page's irrelevant text.
        if response is not None and response.status >= 400:
            logger.warning(
                "collection: %s returned HTTP %s (blocked/error response) -- "
                "treating as a failed load, not real content", url, response.status,
            )
            return None

        try:
            # Best-effort: domcontentloaded can in rare cases fire
            # before <body> is actually parsed/rendered (e.g. a
            # heavily-scripted redirect or streamed response). Not a
            # substitute for "load" -- doesn't wait for JS frameworks
            # to finish mounting -- just cheap insurance that what we're
            # about to read isn't an empty shell. Failure here is never
            # fatal: we still extract whatever page.content() gives us.
            page.wait_for_selector("body", timeout=_BODY_SELECTOR_WAIT_MS)
        except Exception:
            pass

        # page.url, not the requested `url` -- a plain http(s)://domain
        # candidate very commonly redirects to a www (or https) variant
        # (found via a real gap-analysis run: plasticmold.net redirects
        # to www.plasticmold.net, and every one of its internal links is
        # absolute to the www host). Every function below that resolves
        # a relative href to an absolute URL needs the page's REAL
        # final location as its base, not the pre-redirect one it was
        # asked to load -- using the stale `url` here made
        # _find_relevant_links' same-domain check (in _collect_with,
        # which uses this page's .url as its own base_url) compare
        # "www.plasticmold.net" links against a "plasticmold.net" base
        # and wrongly reject literally every link as off-domain,
        # silently capping some suppliers' crawl at just the homepage.
        resolved_url = page.url or url

        html = page.content()
        html_path = self.artifact_store.save_html(run_dir, index, url, html)

        # See _collect_iframe_html's own docstring for the real gap
        # this closes (nVent's real Contact page, correctly visited,
        # producing zero contact info because its actual form widget
        # lives in an embedded iframe page.content() never sees).
        # Scoped to contact extraction ONLY -- image/social/download/
        # facility-photo extraction below still use `html` alone, since
        # those need each fragment's own base URL to resolve relative
        # hrefs correctly and an iframe's separate origin would be
        # actively wrong applied there.
        iframe_html = _collect_iframe_html(page)
        contact_html = f"{html}\n{iframe_html}" if iframe_html else html

        screenshot_relpath = None
        try:
            png_bytes = page.screenshot(full_page=True)
            screenshot_path = self.artifact_store.save_screenshot(run_dir, index, url, png_bytes)
            screenshot_relpath = str(screenshot_path.relative_to(run_dir))
        except Exception as e:
            logger.warning("collection: screenshot failed for %s: %s", url, e)

        collected = CollectedPage(
            url=resolved_url,
            text=html_to_text(contact_html),
            image_urls=_find_image_urls(resolved_url, html),
            has_contact_form=_has_contact_form(contact_html),
            screenshot_path=screenshot_relpath,
            html_path=str(html_path.relative_to(run_dir)),
            social_links=_find_social_links(html),
            download_links=_find_download_links(resolved_url, html),
            footer_text=_extract_footer_text(html),
            facility_photo_urls=_extract_facility_photo_urls(resolved_url, html),
            mailto_emails=_find_mailto_emails(contact_html),
            tel_phones=_find_tel_phones(contact_html),
        )
        return collected, html
