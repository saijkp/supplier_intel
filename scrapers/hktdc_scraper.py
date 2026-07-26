"""
scrapers/hktdc_scraper.py

HKTDC (Hong Kong Trade Development Council) supplier directory scraper.
HKTDC has no public API, so this scrapes their public sourcing directory
search results. Selectors live in HKTDC_SELECTORS (not hardcoded inline)
per Gap 1 from the Phase 1 architecture brief — a site redesign should
only require updating this one dict.

Respectful scraping: exponential-backoff retries via _safe_request,
randomised delays between pages via _polite_delay, and pagination stops
as soon as a page returns zero supplier cards rather than blindly
paginating to `max_pages`.

The httpx client is injectable via the constructor so this class is
unit-tested against canned HTML without hitting the network.
"""

from __future__ import annotations

import logging
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper, ScraperResult

logger = logging.getLogger(__name__)

# CSS selectors for HKTDC's public supplier directory. Update this dict
# (not the parsing code) if HKTDC changes their HTML structure.
HKTDC_SELECTORS = {
    "supplier_card": ".supplier-list-item, [data-testid='supplier-card']",
    "company_name": ".company-name, h3",
    "country": ".country, .location",
    "product_tag": ".product-tag",
    "company_link": "a.company-link",
    "description": ".description",
    "profile_link": "a.profile-link",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}


class HKTDCScraper(BaseScraper):

    BASE_URL = "https://sourcing.hktdc.com"
    SEARCH_PATH = "/en/search"

    def __init__(self, http_client: Optional[httpx.Client] = None, enable_delays: bool = True):
        super().__init__("hktdc", enable_delays=enable_delays)
        self._client = http_client or httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True)

    def scrape(
        self,
        query: str,
        category: str = "auto-parts-accessories",
        max_pages: int = 5,
        **kwargs,
    ) -> List[ScraperResult]:
        results: List[ScraperResult] = []

        for page in range(1, max_pages + 1):
            url = (
                f"{self.BASE_URL}{self.SEARCH_PATH}"
                f"?category={category}&keyword={query}&page={page}"
            )
            try:
                response = self._safe_request(self._client.get, url)
                response.raise_for_status()
            except Exception as e:
                logger.error("HKTDC page %d failed for '%s': %s", page, query, e)
                if page == 1:
                    return [self.error_result(str(e))]
                break

            page_results = self._parse_supplier_list(response.text)
            if not page_results:
                logger.debug("HKTDC: no more results after page %d for '%s'", page - 1, query)
                break

            results.extend(page_results)
            self._polite_delay(3.0, 6.0)

        logger.info("HKTDC: %d suppliers for '%s'", len(results), query)
        return results

    def _parse_supplier_list(self, html: str) -> List[ScraperResult]:
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select(HKTDC_SELECTORS["supplier_card"])
        results: List[ScraperResult] = []

        for card in cards:
            try:
                data = {
                    "company_name": self._text(card, HKTDC_SELECTORS["company_name"]),
                    "country": self._text(card, HKTDC_SELECTORS["country"]),
                    "products": [
                        p.get_text(strip=True)
                        for p in card.select(HKTDC_SELECTORS["product_tag"])
                    ],
                    "website": self._href(card, HKTDC_SELECTORS["company_link"]),
                    "description": self._text(card, HKTDC_SELECTORS["description"]),
                    "hktdc_profile_url": self._href(card, HKTDC_SELECTORS["profile_link"]),
                }

                if data["company_name"]:
                    results.append(ScraperResult(
                        source="hktdc",
                        source_id=data.get("hktdc_profile_url", ""),
                        raw_data=data,
                        success=True,
                    ))
            except Exception as e:
                logger.warning("Failed to parse an HKTDC supplier card: %s", e)

        return results

    @staticmethod
    def _text(element, selector: str) -> str:
        el = element.select_one(selector)
        return el.get_text(strip=True) if el else ""

    @staticmethod
    def _href(element, selector: str) -> str:
        el = element.select_one(selector)
        return el.get("href", "") if el else ""
