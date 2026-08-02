"""
scrapers/alibaba_scraper.py

Alibaba supplier scraper backed by the 'zen-studio/alibaba-scraper'
Apify actor (config.settings.APIFY_ACTORS["alibaba"]) -- replaces
'curious_coder/alibaba-scraper', confirmed dead on Apify's own
platform (a real "Actor with this name was not found" from their
public API). zen-studio's actor is a different actor entirely, not
just a renamed one, with its own input/output schema -- confirmed by
fetching its real input schema and running a live --limit-capped test
against it, not assumed from documentation.

Schema differences from the dead actor that mattered here:
- Input is `keywords: [str]` (a list — searched per-keyword) and
  `resultType`, not a bare `search: str`. resultType="suppliers" is
  used deliberately, not the default "products": it returns one
  manufacturer/factory profile per result, matching this codebase's
  one-record-per-company normalizer -- "products" mode instead
  nests a supplier under each product listing, the wrong granularity
  and a real risk of the same company appearing many times.
- No `proxy` block: this actor manages its own proxying, unlike the
  old one which needed apifyProxyGroups=["RESIDENTIAL"] specified
  explicitly.
- No "years as Gold Supplier" concept in the output at all (Alibaba's
  own site has apparently moved on from that trust badge) -- see
  `min_years_gold`'s own note below for how that parameter's intent
  is preserved anyway.

The Apify client is injectable via the constructor so this class can be
fully unit-tested without an APIFY_TOKEN or network access — only
`client` (the lazily-constructed real ApifyClient) needs the token, and
that path is only hit when no client was injected.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from config.settings import APIFY_ACTORS, APIFY_TOKEN
from scrapers.base_scraper import BaseScraper, ScraperResult

logger = logging.getLogger(__name__)


class AlibabaScraper(BaseScraper):
    """
    Asks the actor itself to only return Verified Manufacturers (and,
    by default, Trade-Assurance suppliers) via input filters, rather
    than fetching everything and discarding low-trust suppliers after
    the fact -- this keeps the raw layer from filling up with noise
    the dedup/scoring stages would just discard later anyway, and we
    never pay Apify for a result we'd have thrown away regardless.
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
        """
        min_years_gold no longer has a literal meaning against this
        actor -- there is no "years as Gold Supplier" field anywhere
        in zen-studio/alibaba-scraper's output, so filtering on it
        after the fact (the old behaviour) is no longer possible.
        Kept as a backward-compatible knob rather than dropped or
        renamed: min_years_gold > 0 (the default) now means "only
        Verified Manufacturers," pushed to the actor as a real input
        filter (verifiedManufacturer) rather than a post-fetch
        discard -- strictly better, since we never pay for a result
        we'd have thrown away anyway. Pass min_years_gold=0 for an
        unfiltered pull.
        """
        want_verified_manufacturer = min_years_gold > 0
        logger.info(
            "Alibaba scrape: '%s' max=%d verifiedManufacturer=%s",
            query, max_results, want_verified_manufacturer,
        )

        run_input: Dict[str, Any] = {
            "resultType": "suppliers",
            "keywords": [query],
            "maxResults": max_results,
            "tradeAssurance": require_trade_assurance,
            "verifiedManufacturer": want_verified_manufacturer,
        }

        try:
            run = self._safe_request(
                self.client.actor(self.actor_id).call,
                run_input=run_input,
                # apify-client 3.x renamed the old timeout_secs=<int> kwarg
                # to run_timeout=<timedelta> -- confirmed by inspecting the
                # actually-installed SDK's ActorClient.call() signature
                # directly, since this had never been exercised against a
                # real APIFY_TOKEN before. max_items is a second,
                # platform-enforced cap on top of run_input's own maxItems:
                # Apify's own docs describe it as also limiting billing for
                # a per-result-charged actor, not just the item count, so
                # this is worth setting even though maxItems above already
                # asks the actor itself to stop at max_results.
                run_timeout=timedelta(seconds=300),
                max_items=max_results,
                # This actor is PAY_PER_EVENT (~$0.004-0.005 per supplier
                # result). Apify's own platform aborts a pay-per-event run
                # at a real $0.00 cost ceiling unless a run explicitly
                # authorises spend via max_total_charge_usd -- confirmed
                # live: an early test run here got "aborted because it
                # reached its maximum cost of $0.00" and returned zero
                # results despite everything else being correct. The
                # multiplier is a deliberately generous (~10x) ceiling
                # over the primary per-result price, not a prediction of
                # actual spend -- actual spend is bounded far tighter by
                # max_items/maxResults above; this is a backstop, not the
                # real budget control.
                max_total_charge_usd=Decimal(str(round(0.05 + max_results * 0.05, 4))),
            )
        except Exception as e:
            logger.error("Alibaba actor run failed for '%s': %s", query, e)
            return [self.error_result(str(e))]

        if run is None:
            logger.error("Alibaba actor run for '%s' returned no Run object (likely aborted)", query)
            return [self.error_result("actor run returned no result (aborted before completing?)")]

        # No post-fetch filtering here anymore -- verifiedManufacturer/
        # tradeAssurance above already asked the actor itself to only
        # return matching suppliers, so anything that comes back is
        # already qualified. supplierId is a guess at the new actor's
        # real key pending a live run confirming it (see this module's
        # own docstring on the schema differences); id/companyId cover
        # the other plausible spellings meanwhile.
        results: List[ScraperResult] = []
        try:
            # apify-client 3.x's ActorClient.call() returns a typed Run
            # model (pydantic), not a dict -- run.default_dataset_id, not
            # run["defaultDatasetId"], confirmed by inspecting the real
            # Run model's fields directly (same live-run discovery as
            # the run_timeout/max_total_charge_usd fixes above).
            dataset = self.client.dataset(run.default_dataset_id)
            for item in dataset.iterate_items():
                source_id = item.get("supplierId") or item.get("id") or item.get("companyId") or ""
                results.append(ScraperResult(
                    source="alibaba",
                    source_id=str(source_id),
                    raw_data=item,
                    success=True,
                ))
        except Exception as e:
            logger.error("Alibaba dataset read failed for '%s': %s", query, e)
            return results or [self.error_result(str(e))]

        logger.info("Alibaba: %d suppliers for '%s'", len(results), query)
        return results
