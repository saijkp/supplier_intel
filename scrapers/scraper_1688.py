"""
scrapers/scraper_1688.py

1688.com scraper backed by the 'webdata_labs/1688-scraper' Apify actor
(config.settings.APIFY_ACTORS["china_1688"]). 1688 is Alibaba's
domestic Chinese wholesale platform -- distinct from Alibaba.com (the
international storefront scrapers/alibaba_scraper.py covers), evaluated
as a second candidate specifically because 1688 sellers skew more
toward genuine manufacturers/wholesalers selling within China, rather
than Alibaba.com's international-trade-oriented mix of manufacturers
and trading companies -- see this actor's own `merchantType` filter
below, which encodes 1688's own factory-verification badge directly.

Real input schema confirmed by fetching Apify's public actor API
(api.apify.com/v2/acts/webdata_labs~1688-scraper/builds/<latest>),
not assumed from the actor's marketing description. Key points that
mattered here:
- `searchQueries` (list) is the keyword-search input; `offerUrls`/
  `offerIds`/`supplierUrls` are alternate direct-lookup modes not used
  here.
- No "return one profile per supplier" mode exists (unlike
  zen-studio/alibaba-scraper's resultType="suppliers") -- this actor's
  unit is always a PRODUCT, with the supplier nested inside. The same
  supplier can legitimately appear across multiple product results;
  this is left to storage.deduplication's own matcher to collapse
  rather than deduplicated here, matching how every other source in
  this codebase already relies on the shared matcher rather than
  scraper-side dedup.
- `merchantType="superFactory"` asks 1688 itself for factory-verified
  sellers only (1688's own stronger badge, distinct from the more
  generic "verifiedMerchant" tier) -- pushed as a real input filter,
  not a post-fetch discard, for the same reason AlibabaScraper does
  this: never pay for a result that would just be thrown away.
- PAY_PER_EVENT (~$0.003/product, cheaper than zen-studio's Alibaba.com
  actor) with no separate start fee. Same Apify-platform $0.00 cost-
  ceiling behaviour as AlibabaScraper (see that module's own note) --
  max_total_charge_usd is set explicitly for the same reason.
- `proxyConfiguration` is accepted but explicitly documented as
  ignored: the actor always uses its own CN residential proxy and
  bakes that cost into the per-product price, so it's omitted here
  rather than sent as dead input.

The Apify client is injectable via the constructor so this class can be
fully unit-tested without an APIFY_TOKEN or network access -- same
pattern as AlibabaScraper/IndiaMartScraper.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from config.settings import APIFY_ACTORS, APIFY_TOKEN
from scrapers.base_scraper import BaseScraper, ScraperResult

logger = logging.getLogger(__name__)


class China1688Scraper(BaseScraper):
    """
    Asks the actor itself to only return factory-verified suppliers
    (merchantType="superFactory") via an input filter, same reasoning
    as AlibabaScraper: never pay Apify for a result that would just be
    discarded as low-trust after the fact.
    """

    def __init__(self, client: Optional[Any] = None, enable_delays: bool = True):
        super().__init__("china_1688", enable_delays=enable_delays)
        self.actor_id = APIFY_ACTORS["china_1688"]
        self._client = client  # injected for tests; built lazily otherwise

    @property
    def client(self) -> Any:
        if self._client is None:
            if not APIFY_TOKEN:
                raise RuntimeError(
                    "APIFY_TOKEN is not set. Add it to your .env file before "
                    "running China1688Scraper against real Apify actors, or "
                    "pass `client=` to China1688Scraper() to inject one (e.g. in tests)."
                )
            from apify_client import ApifyClient  # imported lazily: optional dep for tests
            self._client = ApifyClient(APIFY_TOKEN)
        return self._client

    def scrape(
        self,
        query: str,
        max_results: int = 20,
        require_super_factory: bool = True,
        **kwargs,
    ) -> List[ScraperResult]:
        merchant_type = "superFactory" if require_super_factory else "any"
        logger.info(
            "1688 scrape: '%s' max=%d merchantType=%s",
            query, max_results, merchant_type,
        )

        run_input: Dict[str, Any] = {
            "searchQueries": [query],
            "maxProducts": max_results,
            "merchantType": merchant_type,
        }

        try:
            run = self._safe_request(
                self.client.actor(self.actor_id).call,
                run_input=run_input,
                # See AlibabaScraper's identical notes on both of these
                # -- same apify-client SDK, same PAY_PER_EVENT platform
                # behaviour, same fixes required.
                run_timeout=timedelta(seconds=300),
                max_items=max_results,
                max_total_charge_usd=Decimal(str(round(0.05 + max_results * 0.03, 4))),
            )
        except Exception as e:
            logger.error("1688 actor run failed for '%s': %s", query, e)
            return [self.error_result(str(e))]

        if run is None:
            logger.error("1688 actor run for '%s' returned no Run object (likely aborted)", query)
            return [self.error_result("actor run returned no result (aborted before completing?)")]

        results: List[ScraperResult] = []
        try:
            dataset = self.client.dataset(run.default_dataset_id)
            for item in dataset.iterate_items():
                source_id = item.get("offerId") or item.get("productId") or item.get("id") or ""
                results.append(ScraperResult(
                    source="china_1688",
                    source_id=str(source_id),
                    raw_data=item,
                    success=True,
                ))
        except Exception as e:
            logger.error("1688 dataset read failed for '%s': %s", query, e)
            return results or [self.error_result(str(e))]

        logger.info("1688: %d products for '%s'", len(results), query)
        return results
