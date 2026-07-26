"""
scrapers/global_trade_scraper.py

Config-driven scraper for broader-coverage trade-intelligence providers
(Volza is configured by default; Trade Data Monitor or others could be
added the same way). This exists specifically to address the UK
trade-data gap: scrapers.importyeti_scraper only covers US customs
records, so `confirmed_shipments_uk` — which drives the largest single
share of verification.scorer's export dimension — was structurally
almost unreachable no matter how good a supplier actually was. Providers
configured here claim broader country coverage, including UK-bound
shipments, unlike ImportYeti.

IMPORTANT — NOT VERIFIED AGAINST A LIVE SITE:
I don't have network access to browse these providers' actual current
HTML structure, or confirm which of their access tiers expose real
company-level shipment records without a paid subscription. TRADE_PROVIDERS
below is a best-effort configuration, written in the same shape as every
other scraper in this codebase (base URL + search template + selectors)
specifically so fixing it — once you have real access — is a config
change, not a rewrite. Before relying on this:
  1. Confirm whether the provider's free tier exposes real shipment
     records at all, or only teaser/aggregate data behind a paywall
     (Volza in particular gates most per-shipment detail).
  2. Confirm the actual search URL structure and result-card selectors
     against the live site.
  3. Check the provider's terms of service before scraping at all —
     several trade-data sites explicitly prohibit automated scraping in
     their ToS even where the data itself is publicly viewable, which
     is a different question from whether it's technically possible.
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import Any, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper, ScraperResult

logger = logging.getLogger(__name__)

# Each entry fully describes one trade-data provider: how to build a
# search URL (`{query}` and `{page}` substituted in), how to find
# shipment cards in its HTML, and — crucially, unlike ImportYeti — an
# explicit default_consignee_country matching whatever country-filtered
# page this search URL points at, rather than a single hardcoded
# assumption baked into the normalizer. Add new providers/country pages
# here, not in code.
TRADE_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "volza": {
        "name": "Volza",
        "search_url_template": (
            "https://www.volza.com/p/{query}/import/import-in-united-kingdom/?page={page}"
        ),
        "default_consignee_country": "United Kingdom",
        "selectors": {
            "result_card": ".shipment-row, [data-testid='shipment-row']",
            "shipper_name": ".exporter-name, .supplier-name",
            "consignee_name": ".importer-name, .buyer-name",
            "consignee_country": ".destination-country",
            "shipment_date": ".shipment-date, time",
            "hs_code": ".hs-code",
            "product_desc": ".product-description, .description",
            "origin_port": ".origin-port",
            "destination_port": ".destination-port",
            "weight": ".weight",
            "value": ".value, .shipment-value",
            "company_profile_link": "a.exporter-link, a.profile-link",
        },
    },
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}


class GlobalTradeScraper(BaseScraper):
    """
    `provider` selects which TRADE_PROVIDERS entry to use, and becomes
    this scraper's `source_name` — so results are saved to
    raw_source_data with source='volza' (etc.), and
    pipeline.orchestrator.TRADE_SOURCES / default scrapers/normalizers
    dicts key off that same name.
    """

    def __init__(
        self,
        provider: str = "volza",
        http_client: Optional[httpx.Client] = None,
        enable_delays: bool = True,
    ):
        if provider not in TRADE_PROVIDERS:
            raise ValueError(
                f"Unknown trade provider '{provider}'. Add it to TRADE_PROVIDERS "
                f"or choose one of: {list(TRADE_PROVIDERS)}"
            )
        super().__init__(provider, enable_delays=enable_delays)
        self.provider = provider
        self.config = TRADE_PROVIDERS[provider]
        self._client = http_client or httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True)

    def scrape(self, query: str, max_pages: int = 5, **kwargs) -> List[ScraperResult]:
        results: List[ScraperResult] = []
        template = self.config["search_url_template"]

        for page in range(1, max_pages + 1):
            url = template.format(query=urllib.parse.quote(query), page=page)
            try:
                response = self._safe_request(self._client.get, url)
                response.raise_for_status()
            except Exception as e:
                logger.error("%s page %d failed for '%s': %s", self.provider, page, query, e)
                if page == 1:
                    return [self.error_result(str(e))]
                break

            page_results = self._parse_results(response.text)
            if not page_results:
                logger.debug("%s: no more results after page %d for '%s'", self.provider, page - 1, query)
                break

            results.extend(page_results)
            self._polite_delay(3.0, 6.0)

        logger.info("%s: %d shipment records for '%s'", self.provider, len(results), query)
        return results

    def _parse_results(self, html: str) -> List[ScraperResult]:
        selectors = self.config["selectors"]
        default_country = self.config.get("default_consignee_country", "")
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select(selectors["result_card"])
        results: List[ScraperResult] = []

        for card in cards:
            try:
                consignee_country = self._text(card, selectors["consignee_country"])
                data = {
                    "shipper_name": self._text(card, selectors["shipper_name"]),
                    "consignee_name": self._text(card, selectors["consignee_name"]),
                    # Prefer whatever the page actually shows; fall back
                    # to this provider's configured country only if the
                    # page didn't expose one — unlike ImportYeti, this
                    # is a per-provider config value, not a blanket
                    # assumption hardcoded into the normalizer.
                    "consignee_country": consignee_country or default_country,
                    "shipment_date": self._text(card, selectors["shipment_date"]),
                    "hs_code": self._text(card, selectors["hs_code"]),
                    "product_desc": self._text(card, selectors["product_desc"]),
                    "origin_port": self._text(card, selectors["origin_port"]),
                    "destination_port": self._text(card, selectors["destination_port"]),
                    "weight_raw": self._text(card, selectors["weight"]),
                    "value_raw": self._text(card, selectors["value"]),
                    "company_profile_url": self._href(card, selectors["company_profile_link"]),
                }

                if data["shipper_name"]:
                    results.append(ScraperResult(
                        source=self.provider,
                        source_id=data.get("company_profile_url", ""),
                        raw_data=data,
                        success=True,
                    ))
            except Exception as e:
                logger.warning("Failed to parse a %s result card: %s", self.provider, e)

        return results

    @staticmethod
    def _text(element, selector: str) -> str:
        if not selector:
            return ""
        el = element.select_one(selector)
        return el.get_text(strip=True) if el else ""

    @staticmethod
    def _href(element, selector: str) -> str:
        if not selector:
            return ""
        el = element.select_one(selector)
        return el.get("href", "") if el else ""
