"""
scrapers/alibaba_scraper.py

Alibaba supplier scraper backed by the 'curious_coder/alibaba-scraper'
Apify actor. Uses Chinese residential proxies since Alibaba aggressively
blocks datacenter IPs.

The Apify client is injectable via the constructor so this class can be
fully unit-tested without an APIFY_TOKEN or network access — only
`client` (the lazily-constructed real ApifyClient) needs the token, and
that path is only hit when no client was injected.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from config.settings import APIFY_ACTORS, APIFY_TOKEN
from scrapers.base_scraper import BaseScraper, ScraperResult

logger = logging.getLogger(__name__)


class AlibabaScraper(BaseScraper):
    """
    Filters out non-Gold or too-new suppliers before they ever reach
    raw_source_data, since low-tenure suppliers are rarely worth
    persisting for a trailer-components search — this keeps the raw
    layer from filling up with noise the dedup/scoring stages would
    just discard later anyway.
    """

    def __init__(self, client: Optional[Any] = None, enable_delays: bool = True):
        super().__init__("alibaba", enable_delays=enable_delays)
        self.actor_id = APIFY_ACTORS["alibaba"]
        self._client = client  # injected for tests; built lazily otherwise

    @property
    def client(self) -> Any:
        if self._client is None:
            if not APIFY_TOKEN:
                raise RuntimeError(
                    "APIFY_TOKEN is not set. Add it to your .env file before "
                    "running AlibabaScraper against real Apify actors, or "
                    "pass `client=` to AlibabaScraper() to inject one (e.g. in tests)."
                )
            from apify_client import ApifyClient  # imported lazily: optional dep for tests
            self._client = ApifyClient(APIFY_TOKEN)
        return self._client

    def scrape(
        self,
        query: str,
        max_results: int = 50,
        min_years_gold: int = 3,
        require_trade_assurance: bool = True,
        **kwargs,
    ) -> List[ScraperResult]:
        logger.info(
            "Alibaba scrape: '%s' max=%d min_years_gold=%d",
            query, max_results, min_years_gold,
        )

        run_input: Dict[str, Any] = {
            "search": query,
            "maxItems": max_results,
            "filterByTradeAssurance": require_trade_assurance,
            "filterByVerified": True,
            "proxy": {
                "useApifyProxy": True,
                "apifyProxyGroups": ["RESIDENTIAL"],
                "apifyProxyCountry": "CN",  # Chinese residential IPs look natural to Alibaba
            },
        }

        try:
            run = self._safe_request(
                self.client.actor(self.actor_id).call,
                run_input=run_input,
                timeout_secs=300,
            )
        except Exception as e:
            logger.error("Alibaba actor run failed for '%s': %s", query, e)
            return [self.error_result(str(e))]

        results: List[ScraperResult] = []
        try:
            dataset = self.client.dataset(run["defaultDatasetId"])
            for item in dataset.iterate_items():
                years = item.get("yearsAsGoldSupplier", 0) or 0
                if years < min_years_gold:
                    continue
                results.append(ScraperResult(
                    source="alibaba",
                    source_id=str(item.get("supplierId", "")),
                    raw_data=item,
                    success=True,
                ))
        except Exception as e:
            logger.error("Alibaba dataset read failed for '%s': %s", query, e)
            return results or [self.error_result(str(e))]

        logger.info(
            "Alibaba: %d qualifying suppliers (>= %d gold years) for '%s'",
            len(results), min_years_gold, query,
        )
        return results
