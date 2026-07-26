"""
scrapers/indiamart_scraper.py

IndiaMART supplier scraper backed by the 'zuzka_mach/indiamart-scraper'
Apify actor. IndiaMART's verification signal is TrustSEAL (a paid,
audited badge — the closest analogue to Alibaba's Gold Supplier + Trade
Assurance combination), rather than a "years as gold supplier" tenure
metric — though "member since" is available as a rough tenure proxy.

The Apify client is injectable via the constructor, same pattern as
AlibabaScraper, so this class is fully unit-tested without an
APIFY_TOKEN or network access.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional

from config.settings import APIFY_ACTORS, APIFY_TOKEN
from scrapers.base_scraper import BaseScraper, ScraperResult

logger = logging.getLogger(__name__)


class IndiaMartScraper(BaseScraper):

    def __init__(self, client: Optional[Any] = None, enable_delays: bool = True):
        super().__init__("indiamart", enable_delays=enable_delays)
        self.actor_id = APIFY_ACTORS["indiamart"]
        self._client = client  # injected for tests; built lazily otherwise

    @property
    def client(self) -> Any:
        if self._client is None:
            if not APIFY_TOKEN:
                raise RuntimeError(
                    "APIFY_TOKEN is not set. Add it to your .env file before "
                    "running IndiaMartScraper against real Apify actors, or "
                    "pass `client=` to IndiaMartScraper() to inject one (e.g. in tests)."
                )
            from apify_client import ApifyClient  # imported lazily: optional dep for tests
            self._client = ApifyClient(APIFY_TOKEN)
        return self._client

    def scrape(
        self,
        query: str,
        max_results: int = 50,
        require_trustseal: bool = False,
        min_years_registered: int = 0,
        **kwargs,
    ) -> List[ScraperResult]:
        logger.info(
            "IndiaMART scrape: '%s' max=%d require_trustseal=%s min_years_registered=%d",
            query, max_results, require_trustseal, min_years_registered,
        )

        run_input: Dict[str, Any] = {
            "search": query,
            "maxItems": max_results,
            "proxy": {
                "useApifyProxy": True,
                "apifyProxyGroups": ["RESIDENTIAL"],
                "apifyProxyCountry": "IN",
            },
        }

        try:
            run = self._safe_request(
                self.client.actor(self.actor_id).call,
                run_input=run_input,
                timeout_secs=300,
            )
        except Exception as e:
            logger.error("IndiaMART actor run failed for '%s': %s", query, e)
            return [self.error_result(str(e))]

        results: List[ScraperResult] = []
        try:
            dataset = self.client.dataset(run["defaultDatasetId"])
            for item in dataset.iterate_items():
                if require_trustseal and not item.get("trustSealVerified", False):
                    continue
                years = self._years_registered(item)
                if years is not None and years < min_years_registered:
                    continue
                results.append(ScraperResult(
                    source="indiamart",
                    source_id=str(item.get("sellerId", item.get("id", ""))),
                    raw_data=item,
                    success=True,
                ))
        except Exception as e:
            logger.error("IndiaMART dataset read failed for '%s': %s", query, e)
            return results or [self.error_result(str(e))]

        logger.info(
            "IndiaMART: %d qualifying suppliers for '%s'",
            len(results), query,
        )
        return results

    @staticmethod
    def _years_registered(item: Dict[str, Any]) -> Optional[int]:
        """Derive a rough tenure figure from a 'member since'-style
        field. Returns None (meaning "unknown, don't filter on it")
        rather than 0 when the field is missing or unparseable."""
        member_since = item.get("memberSince") or item.get("registeredSince")
        if not member_since:
            return None
        try:
            year = int(str(member_since)[:4])
            return date.today().year - year
        except (ValueError, TypeError):
            return None
