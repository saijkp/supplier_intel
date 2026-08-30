"""
scrapers/playwright_website_scraper.py

A real-browser fallback for scrapers.own_website_scraper.OwnWebsiteScraper
-- some sites return nothing useful (or nothing at all) to a plain httpx
GET: a bot-challenge page, a JS-only single-page app that renders its
real content client-side, or a WAF that blocks httpx's own User-Agent/
TLS fingerprint but not a real headless Chromium. Found live: several
large, obviously-real trailer-axle manufacturers (Lippert, across all
three of its own domain variants, and Dexter Axle/Group) were being
lost entirely to httpx-level fetch failures during candidate
validation -- discarding a real, findable candidate on an
infrastructure limitation, not any real signal about the company.

Deliberately minimal compared to collection.site_collector.SiteCollector
-- no screenshots, no certificate downloads, no supplier_id/artifact-
directory bookkeeping. This exists ONLY to answer "can a real browser
read this page at all," the same narrow question OwnWebsiteScraper.fetch()
answers for a candidate validator -- so it returns the exact same
OwnWebsiteFetchResult/OwnWebsitePage shape, making it a drop-in
alternative fetcher wherever that interface is already expected
(discovery.candidate_validator.CandidateValidator's `website_fetcher`/
`playwright_fetcher` seam) rather than a second, differently-shaped
result type callers would need to special-case.

Reuses OwnWebsiteScraper's own capability-link discovery/prioritisation
(_find_capability_links / _prioritise_capability_links, both
@staticmethod specifically so they're reusable here) so both fetchers
choose the SAME sub-pages to visit -- the only difference between them
is HOW each page is fetched (a raw httpx GET vs a real rendered browser
page), never WHICH pages get chosen.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from scrapers.own_website_scraper import (
    DEFAULT_USER_AGENT,
    OwnWebsiteFetchResult,
    OwnWebsitePage,
    OwnWebsiteScraper,
    html_to_text,
)

logger = logging.getLogger(__name__)


class PlaywrightWebsiteScraper:
    """Same public interface as OwnWebsiteScraper (`.fetch(domain) ->
    OwnWebsiteFetchResult`), backed by a real headless browser instead
    of httpx. `playwright_factory`, mirroring collection.site_collector.
    SiteCollector's own injectable seam, is a zero-arg callable
    returning a sync_playwright()-context-manager-shaped object (tests
    pass a fake, production code leaves this None and uses the real
    sync_playwright())."""

    def __init__(
        self,
        max_pages: int = 4,
        page_timeout_ms: int = 20_000,
        playwright_factory: Optional[Any] = None,
    ):
        self.max_pages = max_pages
        self.page_timeout_ms = page_timeout_ms
        self._playwright_factory = playwright_factory

    def fetch(self, domain: str) -> OwnWebsiteFetchResult:
        """Never raises -- one candidate's failure must never abort a
        discovery/validation run (same discipline as every other
        scraper in this codebase)."""
        if not domain:
            return OwnWebsiteFetchResult(domain=domain, success=False, error="no domain provided")

        base_url = domain if domain.startswith(("http://", "https://")) else f"https://{domain}"

        try:
            if self._playwright_factory is not None:
                return self._fetch_with(self._playwright_factory(), domain, base_url)
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                return self._fetch_with(p, domain, base_url)
        except Exception as e:  # noqa: BLE001 -- one candidate's failure must never abort a batch/discovery run
            logger.warning("playwright_website: unexpected error fetching %s: %s", domain, e)
            return OwnWebsiteFetchResult(domain=domain, success=False, error=str(e))

    def _fetch_with(self, playwright: Any, domain: str, base_url: str) -> OwnWebsiteFetchResult:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=DEFAULT_USER_AGENT)
            context.set_default_timeout(self.page_timeout_ms)
            page = context.new_page()

            homepage = self._visit(page, base_url)
            if homepage is None:
                return OwnWebsiteFetchResult(
                    domain=domain, success=False, error=f"could not load homepage: {base_url}"
                )
            homepage_page, homepage_html = homepage
            pages: List[OwnWebsitePage] = [homepage_page]

            links = OwnWebsiteScraper._prioritise_capability_links(
                OwnWebsiteScraper._find_capability_links(homepage_page.final_url, homepage_html)
            )
            for link in links:
                if len(pages) >= self.max_pages:
                    break
                visited = self._visit(page, link)
                if visited is not None:
                    pages.append(visited[0])

            return OwnWebsiteFetchResult(domain=domain, pages=pages, success=True)
        finally:
            browser.close()

    def _visit(self, page: Any, url: str) -> Optional[Tuple[OwnWebsitePage, str]]:
        """Returns (OwnWebsitePage, raw_html) so the caller can also run
        capability-link discovery against the same fetch -- None on any
        navigation failure or an HTTP error status, matching
        OwnWebsiteScraper._fetch()'s own "skip this page, don't abort
        the whole fetch" behaviour for a sub-page."""
        try:
            response = page.goto(url, wait_until="domcontentloaded")
        except Exception as e:
            logger.warning("playwright_website: failed to load %s: %s", url, e)
            return None
        if response is not None and response.status >= 400:
            logger.warning("playwright_website: %s returned HTTP %s", url, response.status)
            return None
        html = page.content()
        final_url = page.url or url
        return OwnWebsitePage(url=url, final_url=final_url, text=html_to_text(html)), html
