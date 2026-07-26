"""
scrapers/importyeti_scraper.py

ImportYeti trade-data scraper. ImportYeti has no public API, so this
scrapes their public shipment-search result pages. Selectors live in
IMPORTYETI_SELECTORS below rather than hardcoded inline — per the
Phase 1 architecture brief (Gap 1: config-driven scraping selectors),
a site redesign should only require updating this one dict.

KNOWN LIMITATION: some ImportYeti search result pages render via
client-side JS. If a query you know has data returns zero cards here,
the page needs JS execution — swap `self._client.get(url)` below for a
Playwright/Selenium-rendered page fetch. The parsing logic
(`_parse_results`) stays identical either way since it only cares about
the final HTML.

The httpx client is injectable via the constructor so this class can be
unit-tested against canned HTML without hitting the network.
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper, ScraperResult

logger = logging.getLogger(__name__)

# CSS selectors for ImportYeti's public search results. Update this dict
# (not the parsing code) if ImportYeti changes their HTML structure.
IMPORTYETI_SELECTORS = {
    "result_card": "[data-testid='shipment-card'], .shipment-result, .company-result",
    "shipper_name": ".shipper-name, .supplier-name",
    "consignee_name": ".consignee-name, .buyer-name",
    "shipment_date": ".shipment-date, time",
    "hs_code": ".hs-code",
    "product_desc": ".product-description, .description",
    "origin_port": ".origin-port",
    "destination_port": ".destination-port",
    "weight": ".weight",
    "value": ".value, .shipment-value",
    "company_profile_link": "a.company-link, a.profile-link",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}


class ImportYetiScraper(BaseScraper):
    """
    Respectful scraping: exponential-backoff retries via _safe_request,
    randomised delays via _polite_delay between pages, and it stops as
    soon as a page returns zero result cards rather than blindly
    paginating to `max_pages`.
    """

    BASE_URL = "https://www.importyeti.com"

    def __init__(self, http_client: Optional[httpx.Client] = None, enable_delays: bool = True):
        super().__init__("importyeti", enable_delays=enable_delays)
        self._client = http_client or httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True)

    def scrape(self, query: str, max_pages: int = 5, **kwargs) -> List[ScraperResult]:
        results: List[ScraperResult] = []

        for page in range(1, max_pages + 1):
            url = f"{self.BASE_URL}/search?q={urllib.parse.quote(query)}&page={page}"

            try:
                response = self._safe_request(self._client.get, url)
                response.raise_for_status()
            except Exception as e:
                logger.error("ImportYeti page %d failed for '%s': %s", page, query, e)
                if page == 1:
                    return [self.error_result(str(e))]
                break

            page_results = self._parse_results(response.text)
            if not page_results:
                logger.debug("ImportYeti: no more results after page %d for '%s'", page - 1, query)
                break

            results.extend(page_results)
            self._polite_delay(3.0, 6.0)

        logger.info("ImportYeti: %d shipment records for '%s'", len(results), query)
        return results

    def _parse_results(self, html: str) -> List[ScraperResult]:
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select(IMPORTYETI_SELECTORS["result_card"])
        results: List[ScraperResult] = []

        for card in cards:
            try:
                data = {
                    "shipper_name": self._text(card, IMPORTYETI_SELECTORS["shipper_name"]),
                    "consignee_name": self._text(card, IMPORTYETI_SELECTORS["consignee_name"]),
                    "shipment_date": self._text(card, IMPORTYETI_SELECTORS["shipment_date"]),
                    "hs_code": self._text(card, IMPORTYETI_SELECTORS["hs_code"]),
                    "product_desc": self._text(card, IMPORTYETI_SELECTORS["product_desc"]),
                    "origin_port": self._text(card, IMPORTYETI_SELECTORS["origin_port"]),
                    "destination_port": self._text(card, IMPORTYETI_SELECTORS["destination_port"]),
                    "weight_raw": self._text(card, IMPORTYETI_SELECTORS["weight"]),
                    "value_raw": self._text(card, IMPORTYETI_SELECTORS["value"]),
                    "company_profile_url": self._href(card, IMPORTYETI_SELECTORS["company_profile_link"]),
                }

                if data["shipper_name"]:
                    results.append(ScraperResult(
                        source="importyeti",
                        source_id=data.get("company_profile_url", ""),
                        raw_data=data,
                        success=True,
                    ))
            except Exception as e:
                logger.warning("Failed to parse an ImportYeti result card: %s", e)

        return results

    @staticmethod
    def _text(element, selector: str) -> str:
        el = element.select_one(selector)
        return el.get_text(strip=True) if el else ""

    @staticmethod
    def _href(element, selector: str) -> str:
        el = element.select_one(selector)
        return el.get("href", "") if el else ""
