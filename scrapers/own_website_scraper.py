"""
scrapers/own_website_scraper.py

Fetches a supplier's own website content — not a directory listing
about them, but their own corporate site — for
verification.capability_extractor to read.

Why this didn't exist before this addition
--------------------------------------------
Every scraper in this codebase reads a *directory* (Alibaba, HKTDC,
IndiaMART, an exhibition site, a Google result). None of them read the
one source that actually says how a company manufactures, in its own
words, uninfluenced by a directory's listing template: the company's
own corporate website. `suppliers.domain` has been a column since
Phase 1 and is populated by every normalizer that finds one — nothing
downstream of that ever fetched it.

Why this is not a BaseScraper subclass
-----------------------------------------
`BaseScraper.scrape(query, **kwargs)` answers "search this source for
`query`." This class answers a different question — "read *this one
already-identified* supplier's own site" — keyed by domain, not by a
search query, and it isn't looking for new candidate companies, only
enriching one already on file. Forcing it into `scrape(query)`'s shape
would be a worse fit than just being its own small class; it still
follows every other convention this codebase's scrapers use (polite
delay, retry-then-give-up, never raise for an ordinary fetch failure).

Which pages get fetched
-------------------------
The homepage, plus any internal links whose anchor text or href
contains a capability-relevant keyword ("about", "capabilit",
"manufactur", "factory", "facilit", "quality", "certificat"). This is
a deliberately cheap heuristic, not a full-site crawl — a company's
capability claims are almost always on one of a handful of pages, and
crawling the whole site for a 10k-supplier catalogue would be far more
network traffic than the signal is worth. `max_pages` caps the total
fetched (homepage + matched links) per supplier.
"""

from __future__ import annotations

import logging
import time
import random
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, List, Optional

import httpx
from bs4 import BeautifulSoup

from config.settings import REQUEST_DELAYS

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_CAPABILITY_LINK_KEYWORDS: tuple[str, ...] = (
    "about", "capabilit", "manufactur", "factory", "facilit", "production",
    "quality", "certificat", "workshop", "contact",
)

# Filename/path fragments that mean an <img> is almost certainly not a
# facility photo -- logos, icons, tracking pixels, decorative UI
# assets. Deliberately a keyword filter, not a size/dimension check
# (which would need the bytes downloaded first, defeating the point of
# filtering before download). This is a coarse pre-filter; the real
# judgment of whether a surviving image actually shows a factory is
# verification.factory_photo_verifier's job, not this scraper's.
_NON_FACILITY_IMAGE_KEYWORDS: tuple[str, ...] = (
    "logo", "icon", "favicon", "avatar", "sprite", "pixel", "tracking",
    "banner", "badge", "button", "arrow", "bullet",
)
_MAX_IMAGES_PER_PAGE = 5

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)


@dataclass
class OwnWebsitePage:
    url: str
    text: str
    image_urls: List[str] = field(default_factory=list)
    has_contact_form: bool = False


@dataclass
class OwnWebsiteFetchResult:
    domain: str
    pages: List[OwnWebsitePage] = field(default_factory=list)
    success: bool = True
    error: Optional[str] = None


def html_to_text(html: str) -> str:
    """Strip markup to plain readable text. Deliberately dependency-free
    beyond BeautifulSoup, which this codebase already depends on for
    every other scraper. Public (not module-private) since it's also
    reused by verification/catalogue_depth_service.py to convert
    already-saved collection HTML artifacts to text without a second
    live fetch -- the same conversion this module already does for its
    own freshly-fetched pages."""
    soup = BeautifulSoup(_SCRIPT_STYLE_RE.sub(" ", html), "html.parser")
    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


class OwnWebsiteScraper:
    """Fetches a supplier's own homepage and a handful of
    capability-relevant sub-pages.

    `http_client` is injectable, mirroring
    `scrapers.global_directory_scraper.GlobalDirectoryScraper`'s exact
    convention — a real `httpx.Client` is built lazily if none is
    given, so production code needs no wiring, and tests can pass a
    fake object with a `.get(url)` method and no network at all.
    `enable_delays=False` in tests, matching every other scraper.
    """

    def __init__(
        self,
        http_client: Optional[Any] = None,
        enable_delays: bool = True,
        timeout: float = 15.0,
        max_pages: int = 4,
    ):
        self._client = http_client or httpx.Client(
            headers={"User-Agent": DEFAULT_USER_AGENT}, timeout=timeout, follow_redirects=True
        )
        self.enable_delays = enable_delays
        self.max_pages = max_pages

    def _polite_delay(self) -> None:
        if not self.enable_delays:
            return
        min_sec, max_sec = REQUEST_DELAYS.get("own_website", (1.0, 2.5))
        time.sleep(random.uniform(min_sec, max_sec))

    def _fetch(self, url: str) -> Optional[str]:
        try:
            response = self._client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("own_website: failed to fetch %s: %s", url, exc)
            return None
        content_type = getattr(response, "headers", {}).get("content-type", "html")
        if "html" not in content_type and "text" not in content_type:
            return None
        return response.text

    def _find_capability_links(self, base_url: str, html: str) -> List[str]:
        soup = BeautifulSoup(html, "html.parser")
        found: List[str] = []
        seen: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            anchor_text = (anchor.get_text() or "").strip().lower()
            haystack = f"{href.lower()} {anchor_text}"
            if not any(keyword in haystack for keyword in _CAPABILITY_LINK_KEYWORDS):
                continue
            absolute = urllib.parse.urljoin(base_url, href)
            parsed_base = urllib.parse.urlsplit(base_url)
            parsed_link = urllib.parse.urlsplit(absolute)
            if parsed_link.scheme not in ("http", "https"):
                continue  # never try to fetch a mailto:/tel:/javascript: href as a page
            if parsed_link.netloc and parsed_link.netloc != parsed_base.netloc:
                continue  # never follow off-domain links
            normalised = absolute.split("#")[0]
            if normalised not in seen:
                seen.add(normalised)
                found.append(normalised)
        return found

    def _find_image_urls(self, base_url: str, html: str) -> List[str]:
        """Absolute URLs of images plausibly worth passing to
        `factory_photo_verifier` -- deliberately NOT restricted to the
        same domain the way `_find_capability_links` restricts
        internal navigation, since a company's photos are very
        commonly served from a CDN or image-hosting subdomain that
        isn't their own registered domain. Capped at
        `_MAX_IMAGES_PER_PAGE` to bound download/vision-API cost per
        page.
        """
        soup = BeautifulSoup(html, "html.parser")
        found: List[str] = []
        seen: set[str] = set()
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

    def _has_contact_form(self, html: str) -> bool:
        """A `<form>` whose fields suggest it collects a name, email,
        or message -- the common shape of a "Contact Us" form.
        Deliberately loose (any form with at least one such field
        counts) since the goal is "does this page offer a way to reach
        them at all," not a precise classification of every form type
        a page might have (search boxes, newsletter signups) -- those
        false positives are an acceptable cost against the alternative
        of missing a real contact form due to an overly strict check.
        """
        soup = BeautifulSoup(html, "html.parser")
        contact_field_keywords = ("name", "email", "message", "phone", "enquiry", "inquiry", "subject")
        for form in soup.find_all("form"):
            for field_tag in form.find_all(("input", "textarea")):
                haystack = " ".join(
                    str(field_tag.get(attr, "")) for attr in ("name", "id", "placeholder", "type")
                ).lower()
                if any(keyword in haystack for keyword in contact_field_keywords):
                    return True
        return False

    def fetch(self, domain: str) -> OwnWebsiteFetchResult:
        """`domain` may be a bare domain ("acme-trailer.com") or a full
        URL — either is accepted, matching how `suppliers.domain` is
        actually populated across normalizers (some store bare
        domains, some store full URLs).
        """
        if not domain:
            return OwnWebsiteFetchResult(domain=domain, success=False, error="no domain provided")

        base_url = domain if domain.startswith(("http://", "https://")) else f"https://{domain}"

        pages: List[OwnWebsitePage] = []
        try:
            homepage_html = self._fetch(base_url)
            if homepage_html is None:
                return OwnWebsiteFetchResult(
                    domain=domain, success=False, error=f"could not fetch homepage: {base_url}"
                )
            pages.append(OwnWebsitePage(
                url=base_url, text=html_to_text(homepage_html),
                image_urls=self._find_image_urls(base_url, homepage_html),
                has_contact_form=self._has_contact_form(homepage_html),
            ))

            for link in self._find_capability_links(base_url, homepage_html):
                if len(pages) >= self.max_pages:
                    break
                self._polite_delay()
                page_html = self._fetch(link)
                if page_html:
                    pages.append(OwnWebsitePage(
                        url=link, text=html_to_text(page_html),
                        image_urls=self._find_image_urls(link, page_html),
                        has_contact_form=self._has_contact_form(page_html),
                    ))
        except Exception as e:  # noqa: BLE001 - never let one supplier's fetch abort a batch run
            logger.error("own_website: unexpected error fetching %s: %s", domain, e)
            return OwnWebsiteFetchResult(domain=domain, success=False, error=str(e))

        return OwnWebsiteFetchResult(domain=domain, pages=pages, success=True)
