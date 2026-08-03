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
"download", "cert" -- richer than OwnWebsiteScraper's own set since
Collection Service's brief explicitly wants product pages and
downloads/catalogues, not just capability-adjacent pages.

_find_image_urls/_has_contact_form are reimplemented here (not imported
from OwnWebsiteScraper) because they're private instance methods on
that class, not standalone functions -- reaching across to call another
class's private methods would be more fragile than the small amount of
duplication this avoids. _html_to_text IS a standalone module function
there and is imported directly.
"""

from __future__ import annotations

import logging
import urllib.parse
from pathlib import Path
from typing import Any, List, Optional, Tuple

from bs4 import BeautifulSoup

from collection.artifact_store import ArtifactStore
from collection.proxy_provider import NoProxyProvider, ProxyProvider
from collection.schemas import CollectedPage, CollectionResult
from config.settings import COLLECTION_PAGE_TIMEOUT_MS
from scrapers.own_website_scraper import _html_to_text

logger = logging.getLogger(__name__)

# own_website_scraper's own capability-page keywords, extended with
# product/catalogue/download/certification terms -- see module
# docstring for why Collection Service's page selection is broader.
_RELEVANT_LINK_KEYWORDS: Tuple[str, ...] = (
    "about", "capabilit", "manufactur", "factory", "facilit", "production",
    "quality", "certificat", "workshop", "contact",
    "product", "catalog", "catalogue", "download", "cert",
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

    def collect(self, supplier_id: int, domain: str) -> CollectionResult:
        """Never raises -- a single supplier's collection failure must
        never abort a batch run (same discipline as every other
        pipeline stage in this codebase)."""
        if not domain:
            return CollectionResult(domain=domain, success=False, error="no domain provided")

        base_url = domain if domain.startswith(("http://", "https://")) else f"https://{domain}"
        run_dir = self.artifact_store.new_run_dir(supplier_id)
        relative_dir = self.artifact_store.relative_path(run_dir)
        provider_name = type(self.proxy_provider).__name__

        try:
            if self._playwright_factory is not None:
                return self._collect_with(self._playwright_factory(), base_url, domain, run_dir, relative_dir, provider_name)
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                return self._collect_with(p, base_url, domain, run_dir, relative_dir, provider_name)
        except Exception as e:  # noqa: BLE001 -- one supplier's collection must never abort a batch
            logger.error("collection: unexpected error collecting %s: %s", domain, e)
            return CollectionResult(
                domain=domain, success=False, error=str(e),
                artifacts_dir=relative_dir, proxy_provider=provider_name,
            )

    def _collect_with(
        self, playwright: Any, base_url: str, domain: str, run_dir: Path, relative_dir: str, provider_name: str,
    ) -> CollectionResult:
        browser, context = self._launch(playwright)
        try:
            pages: List[CollectedPage] = []
            page = context.new_page()

            homepage = self._visit_and_collect(page, base_url, 0, run_dir)
            if homepage is None:
                return CollectionResult(
                    domain=domain, success=False, error=f"could not load homepage: {base_url}",
                    artifacts_dir=relative_dir, proxy_provider=provider_name,
                )
            homepage_page, homepage_html = homepage
            pages.append(homepage_page)

            for i, link in enumerate(_find_relevant_links(base_url, homepage_html), start=1):
                if len(pages) >= self.max_pages:
                    break
                visited = self._visit_and_collect(page, link, i, run_dir)
                if visited is not None:
                    pages.append(visited[0])

            return CollectionResult(
                domain=domain, pages=pages, success=True,
                artifacts_dir=relative_dir, proxy_provider=provider_name,
            )
        finally:
            browser.close()

    def _visit_and_collect(
        self, page: Any, url: str, index: int, run_dir: Path,
    ) -> Optional[Tuple[CollectedPage, str]]:
        try:
            page.goto(url, timeout=self.page_timeout_ms)
        except Exception as e:
            logger.warning("collection: failed to load %s: %s", url, e)
            return None

        html = page.content()
        html_path = self.artifact_store.save_html(run_dir, index, url, html)

        screenshot_relpath = None
        try:
            png_bytes = page.screenshot(full_page=True)
            screenshot_path = self.artifact_store.save_screenshot(run_dir, index, url, png_bytes)
            screenshot_relpath = str(screenshot_path.relative_to(run_dir))
        except Exception as e:
            logger.warning("collection: screenshot failed for %s: %s", url, e)

        collected = CollectedPage(
            url=url,
            text=_html_to_text(html),
            image_urls=_find_image_urls(url, html),
            has_contact_form=_has_contact_form(html),
            screenshot_path=screenshot_relpath,
            html_path=str(html_path.relative_to(run_dir)),
            social_links=_find_social_links(html),
            download_links=_find_download_links(url, html),
        )
        return collected, html
