"""
pipeline/orchestrator.py

The single callable pipeline: scrape -> save raw -> normalise ->
deduplicate/merge -> verify -> score, across every configured source.

This is the piece the original Phase 1 architecture brief called
orchestrator.py (its Stage 1-5 loop) — rebuilt here against the actual
classes built in Phases 1-5, rather than the illustrative sketch from
the brief. Every dependency (repo, matcher, scorer, qichacha verifier,
and the scraper/normalizer pairs themselves) is injectable via the
constructor, so the whole pipeline is unit-testable with fake scrapers
and no network access — see tests/test_pipeline.py.

Usage:
    from pipeline.orchestrator import SupplierIntelligencePipeline
    pipeline = SupplierIntelligencePipeline()
    stats = pipeline.run("LED marker light", sources=["alibaba", "hktdc"])
"""

from __future__ import annotations

import logging
import random
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from config.settings import TRAILER_COMPONENT_SEARCH_TERMS
from deduplication.matcher import SupplierMatcher
from normalizers.alibaba_normalizer import AlibabaNormalizer
from normalizers.base_normalizer import BaseNormalizer
from normalizers.china_1688_normalizer import China1688Normalizer
from normalizers.expo_normalizer import ExpoNormalizer
from normalizers.global_directory_normalizer import GlobalDirectoryNormalizer
from normalizers.google_search_normalizer import GoogleSearchNormalizer
from normalizers.hktdc_normalizer import HKTDCNormalizer
from normalizers.indiamart_normalizer import IndiaMartNormalizer
from normalizers.trade_normalizer import TradeNormalizer
from scrapers.alibaba_scraper import AlibabaScraper
from scrapers.base_scraper import BaseScraper
from scrapers.company_website_finder import CompanyWebsiteFinder
from scrapers.global_directory_scraper import DIRECTORY_SOURCES, GlobalDirectoryScraper
from scrapers.global_trade_scraper import GlobalTradeScraper
from scrapers.google_search_scraper import GoogleSearchScraper
from scrapers.hktdc_scraper import HKTDCScraper
from scrapers.importyeti_scraper import ImportYetiScraper
from scrapers.indiamart_scraper import IndiaMartScraper
from scrapers.own_website_scraper import OwnWebsiteScraper
from scrapers.photo_downloader import PhotoDownloader
from scrapers.scraper_1688 import China1688Scraper
from scrapers.shanghai_expo_scraper import EXHIBITION_SOURCES, ShanghaiExpoScraper
from storage.repository import SupplierRepository
from verification.capability_extractor import CapabilityExtractor
from verification.cert_checker import CertChecker
from verification.facility_address_verifier import (
    AmapAddressVerifier,
    GooglePlacesAddressVerifier,
    select_address_verifier,
)
from verification.factory_photo_verifier import FactoryPhotoVerifier
from verification.linkedin_presence import LinkedInPresenceChecker
from verification.manufacturer_verifier import ManufacturerVerifier
from verification.qichacha import QichachaVerifier
from verification.scorer import SupplierScorer
from verification.website_contact_extractor import (
    best_contact_method,
    country_name_to_region_code,
    extract_contact_details,
)

logger = logging.getLogger(__name__)

# Sources whose raw data describes a *shipment* rather than a company
# profile — these need a second normalizer call (to_shipment_record)
# and a repo.add_shipment_record() write that other sources don't.
# 'volza' (via GlobalTradeScraper) is the UK/broader-coverage
# counterpart to 'importyeti' (US-only) — see scrapers/global_trade_scraper.py.
TRADE_SOURCES = {"importyeti", "panjiva", "volza"}

DEFAULT_STATS_KEYS = (
    "scraped", "scrape_errors", "skipped_incremental", "normalised", "skipped_no_name",
    "created", "merged", "review_queued", "shipment_records",
    "verified", "manufacturer_assessed", "scored", "capability_extracted",
    "contact_emails_added", "contact_phones_added", "contact_forms_recorded", "photos_assessed",
    "website_discovered", "facility_address_verified", "linkedin_checked",
)

# Sources whose scrape() accepts max_results (a real item-count cap the
# scraper itself understands); every other source in this codebase
# accepts max_pages instead. Shared between main.py's --limit and the
# API's PipelineJobRequest.limit specifically so the two callers can't
# drift out of sync the way this would if each kept its own copy --
# already true once, before this was pulled out (main.py's own mapping
# had to be updated by hand each time a new pay-per-event source was
# added tonight).
MAX_RESULTS_SOURCES = {"alibaba", "indiamart", "china_1688", "google"}


def build_limit_scraper_kwargs(
    limit: Optional[int], sources: Optional[List[str]], all_source_names: List[str],
) -> Dict[str, Dict[str, Any]]:
    """Per-source scraper_kwargs asking each source's own natural
    count-limiting parameter to stay near `limit` -- avoids over-
    fetching (and, for pay-per-event actors, overpaying) before
    `results_limit` truncates the results anyway. Returns {} when
    limit is None, matching "no cap" behaviour unchanged.

    `sources`: the caller's explicit -s/sources list, or None/empty to
    mean "every configured source" -- in which case `all_source_names`
    (typically list(pipeline.scrapers.keys())) is used instead, so a
    bare --limit with no explicit source list still avoids
    over-fetching from every source, not just named ones.
    """
    if limit is None:
        return {}
    effective_sources = sources or all_source_names
    scraper_kwargs: Dict[str, Dict[str, Any]] = {}
    for source_name in effective_sources:
        if source_name in MAX_RESULTS_SOURCES:
            scraper_kwargs[source_name] = {"max_results": limit}
        else:
            scraper_kwargs[source_name] = {"max_pages": 1}
    return scraper_kwargs


class SupplierIntelligencePipeline:

    def __init__(
        self,
        repo: Optional[SupplierRepository] = None,
        db_path: Optional[Union[Path, str]] = None,
        matcher: Optional[SupplierMatcher] = None,
        scorer: Optional[SupplierScorer] = None,
        qichacha: Optional[QichachaVerifier] = None,
        scrapers: Optional[Dict[str, BaseScraper]] = None,
        normalizers: Optional[Dict[str, BaseNormalizer]] = None,
        own_website_scraper: Optional[OwnWebsiteScraper] = None,
        capability_extractor: Optional[CapabilityExtractor] = None,
        website_finder: Optional[CompanyWebsiteFinder] = None,
        photo_downloader: Optional[PhotoDownloader] = None,
        factory_photo_verifier: Optional[FactoryPhotoVerifier] = None,
        google_places_verifier: Optional[GooglePlacesAddressVerifier] = None,
        amap_verifier: Optional[AmapAddressVerifier] = None,
        linkedin_checker: Optional[LinkedInPresenceChecker] = None,
    ):
        self.repo = repo or SupplierRepository(db_path=db_path)
        self.matcher = matcher or SupplierMatcher(self.repo)
        self.scorer = scorer or SupplierScorer()
        self.cert_checker = CertChecker(self.repo)
        self.manufacturer_verifier = ManufacturerVerifier()
        self.own_website_scraper = own_website_scraper or OwnWebsiteScraper()
        self.capability_extractor = capability_extractor or CapabilityExtractor()
        self.photo_downloader = photo_downloader or PhotoDownloader()
        self.factory_photo_verifier = factory_photo_verifier or FactoryPhotoVerifier()
        self.google_places_verifier = google_places_verifier or GooglePlacesAddressVerifier()
        self.amap_verifier = amap_verifier or AmapAddressVerifier()

        # Constructing these is always safe even without APIFY_TOKEN /
        # Qichacha credentials configured — every scraper/verifier in
        # this codebase only raises when actually invoked without
        # credentials, never at construction time.
        self.scrapers: Dict[str, BaseScraper] = scrapers if scrapers is not None else {
            "alibaba": AlibabaScraper(),
            "indiamart": IndiaMartScraper(),
            "china_1688": China1688Scraper(),
            "hktdc": HKTDCScraper(),
            "importyeti": ImportYetiScraper(),
            "volza": GlobalTradeScraper(provider="volza"),
            "google": GoogleSearchScraper(),
            **{
                exhibition: ShanghaiExpoScraper(exhibition=exhibition)
                for exhibition in EXHIBITION_SOURCES
            },
            **{
                directory: GlobalDirectoryScraper(directory=directory)
                for directory in DIRECTORY_SOURCES
            },
        }
        self.website_finder = website_finder or CompanyWebsiteFinder(
            google_scraper=self.scrapers.get("google") or GoogleSearchScraper(),
            own_website_scraper=self.own_website_scraper,
        )
        self.linkedin_checker = linkedin_checker or LinkedInPresenceChecker(
            google_scraper=self.scrapers.get("google") or GoogleSearchScraper(),
        )
        self.normalizers: Dict[str, BaseNormalizer] = normalizers if normalizers is not None else {
            "alibaba": AlibabaNormalizer(),
            "indiamart": IndiaMartNormalizer(),
            "china_1688": China1688Normalizer(),
            "hktdc": HKTDCNormalizer(),
            "importyeti": TradeNormalizer(),
            "volza": TradeNormalizer(),
            "google": GoogleSearchNormalizer(),
            **{
                exhibition: ExpoNormalizer()
                for exhibition in EXHIBITION_SOURCES
            },
            **{
                directory: GlobalDirectoryNormalizer()
                for directory in DIRECTORY_SOURCES
            },
        }
        self.qichacha = qichacha or QichachaVerifier()

    # ═════════════════════════════════════════════════════
    # Full pipeline run
    # ═════════════════════════════════════════════════════

    def run(
        self,
        query: str,
        sources: Optional[List[str]] = None,
        scraper_kwargs: Optional[Dict[str, Dict[str, Any]]] = None,
        run_verification: bool = True,
        run_scoring: bool = True,
        run_capability_extraction: bool = False,
        run_website_discovery: bool = False,
        run_facility_verification: bool = False,
        run_linkedin_check: bool = False,
        incremental_days: int = 0,
        force_rescrape: bool = False,
        results_limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Run every stage for `query` against `sources` (defaults to every
        configured source). Returns a stats dict suitable for logging or
        displaying in a CLI summary.

        Each stage is deliberately fault-isolated: a scrape failure on
        one source, a normalisation error on one record, or a dedup
        failure on one candidate is logged and counted, never allowed to
        abort the rest of the run.

        incremental_days: if > 0, skip scraping a (source, query) pair
        that was already scraped within this many days — addresses
        Phase 1 Gap 4 (avoid burning Apify credits / rate limits
        re-scraping data you already have). 0 (the default) means
        always scrape, matching every prior version of this method's
        behaviour. force_rescrape=True scrapes regardless of
        incremental_days, for a one-off "I know it's recent, scrape it
        anyway" run.

        run_capability_extraction: off by default, unlike
        run_verification/run_scoring — it makes real outbound HTTP
        requests to every supplier's own website (not just a
        configured data source) plus an OpenAI call per page, a
        materially different cost/traffic profile than the rest of a
        default run. Turn it on explicitly here, or call
        run_capability_extraction_only() as a separate pass once
        you're ready to pay that cost across the catalogue. Photo
        verification (verification.factory_photo_verifier) and contact
        extraction both ride along with this flag rather than needing
        their own — they reuse the exact same website fetch.

        run_website_discovery: off by default too, and for the same
        reason plus one more — it costs a paid SerpAPI call per
        domain-less supplier (see CompanyWebsiteFinder's own module
        docstring). Runs BEFORE capability extraction deliberately: a
        domain found and validated here becomes visible to
        `get_suppliers_needing_capability_extraction`'s query within
        this same run, so a supplier that started with no usable
        website can still get capability/contact enrichment in one
        pass rather than needing a second `run()` call.

        run_facility_verification: confirms a supplier's claimed
        address resolves to a real place (Google Places outside China,
        Amap within it -- see facility_address_verifier's own module
        docstring for why, and for the real registration friction to
        expect on the China path specifically). Off by default: a paid
        API call per supplier with an address on file.

        run_linkedin_check: confirms a findable LinkedIn company page
        exists, via a Google-search snippet -- never scrapes LinkedIn
        directly. Off by default: another paid SerpAPI call per
        supplier.

        results_limit: hard cap on raw results kept per source, applied
        after that source's own scraper returns -- for a small,
        cost-controlled test run against a source that's never been
        exercised against real credentials before. None (the default)
        means no cap, matching every prior version of this method's
        behaviour. See `_scrape_stage`'s own note on why this is
        enforced centrally rather than trusting each scraper's own
        (inconsistently-named: max_results vs max_pages) internal
        count-limiting parameter alone.
        """
        sources = sources or list(self.scrapers.keys())
        scraper_kwargs = scraper_kwargs or {}
        stats: Dict[str, Any] = {"query": query, "sources": sources}
        stats.update({key: 0 for key in DEFAULT_STATS_KEYS})

        raw_records = self._scrape_stage(
            query, sources, scraper_kwargs, stats,
            incremental_days=incremental_days, force_rescrape=force_rescrape,
            results_limit=results_limit,
        )
        self.repo.log_search(query=query, sources_used=sources, results_count=stats["scraped"])
        self._normalise_and_dedup_stage(raw_records, stats)

        if run_verification:
            self._verification_stage(stats)
        else:
            logger.info("Skipping verification stage (run_verification=False)")

        # Manufacturer assessment doesn't need any external credentials —
        # it works off whatever's already on the supplier record — so it
        # always runs, independent of run_verification. It naturally
        # benefits from having just run _verification_stage first, since
        # that's what populates business_scope / registered_capital_rmb.
        self._manufacturer_verification_stage(stats)

        if run_website_discovery:
            self._website_discovery_stage(stats)

        if run_facility_verification:
            self._facility_verification_stage(stats)

        if run_linkedin_check:
            self._linkedin_check_stage(stats)

        if run_capability_extraction:
            self._capability_extraction_stage(stats)

        if run_scoring:
            self._scoring_stage(stats)
        else:
            logger.info("Skipping scoring stage (run_scoring=False)")

        logger.info("Pipeline run complete for '%s': %s", query, stats)
        return stats

    # ═════════════════════════════════════════════════════
    # Campaign / sweep mode
    # ═════════════════════════════════════════════════════

    def run_campaign(
        self,
        queries: Optional[List[str]] = None,
        sources: Optional[List[str]] = None,
        incremental_days: int = 30,
        delay_seconds: Tuple[float, float] = (1.0, 3.0),
        enable_delays: bool = True,
        **run_kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Sweep the pipeline across multiple queries in one call — e.g.
        every term in config.settings.TRAILER_COMPONENT_SEARCH_TERMS
        (the default) — instead of the one-query-at-a-time usage run()
        supports directly. This is what turns the platform from "a tool
        you point at one search" into a standing discovery engine across
        the whole component catalogue.

        Defaults to incremental_days=30, unlike run()'s default of 0 —
        a sweep is exactly the repeated/scheduled scenario Gap 4 (Phase
        1) was about: you don't want a weekly sweep re-scraping every
        query from scratch every single time.

        Each query is fault-isolated: an unexpected exception from one
        query's run() is logged and counted, and the campaign continues
        with the next query rather than aborting the whole sweep.
        """
        queries = queries or list(TRAILER_COMPONENT_SEARCH_TERMS)
        campaign_stats: Dict[str, Any] = {
            "queries_total": len(queries),
            "queries_run": 0,
            "queries_failed": 0,
            "per_query": [],
        }
        totals: Dict[str, int] = {key: 0 for key in DEFAULT_STATS_KEYS}

        for i, query in enumerate(queries):
            try:
                result = self.run(
                    query, sources=sources, incremental_days=incremental_days, **run_kwargs,
                )
                campaign_stats["queries_run"] += 1
                campaign_stats["per_query"].append(result)
                for key in DEFAULT_STATS_KEYS:
                    totals[key] += result.get(key, 0)
            except Exception as e:
                logger.error("Campaign query '%s' failed: %s", query, e)
                campaign_stats["queries_failed"] += 1

            if enable_delays and i < len(queries) - 1:
                time.sleep(random.uniform(*delay_seconds))

        campaign_stats["totals"] = totals
        logger.info(
            "Campaign complete: %d/%d queries run (%d failed)",
            campaign_stats["queries_run"], campaign_stats["queries_total"], campaign_stats["queries_failed"],
        )
        return campaign_stats

    # ═════════════════════════════════════════════════════
    # Stage 1: scrape + persist raw
    # ═════════════════════════════════════════════════════

    def _scrape_stage(
        self,
        query: str,
        sources: List[str],
        scraper_kwargs: Dict[str, Dict[str, Any]],
        stats: Dict[str, Any],
        incremental_days: int = 0,
        force_rescrape: bool = False,
        results_limit: Optional[int] = None,
    ) -> List[tuple]:
        raw_records = []  # (raw_id, source_name, raw_data)

        for source_name in sources:
            scraper = self.scrapers.get(source_name)
            if scraper is None:
                logger.warning("Unknown source '%s' — skipping (no scraper configured)", source_name)
                continue

            if not force_rescrape and self.repo.was_recently_scraped(source_name, query, incremental_days):
                logger.info(
                    "Skipping %s for '%s' — scraped within the last %d day(s) (incremental_days)",
                    source_name, query, incremental_days,
                )
                stats["skipped_incremental"] += 1
                continue

            kwargs = scraper_kwargs.get(source_name, {})
            logger.info("Scraping %s for '%s'...", source_name, query)
            try:
                results = scraper.scrape(query, **kwargs)
            except Exception as e:
                logger.error("%s scraper raised unexpectedly for '%s': %s", source_name, query, e)
                stats["scrape_errors"] += 1
                continue

            if results_limit is not None:
                # A hard, source-agnostic cap -- independent of whatever
                # count-limiting kwarg (or lack of one) a given scraper
                # happens to accept internally. scraper_kwargs can (and,
                # for a small test run, should) also pass a scraper's own
                # max_results/max_pages to avoid paying for/fetching more
                # than necessary in the first place, but this is the one
                # guarantee that holds regardless: no source ever produces
                # more than results_limit raw records in a single run.
                results = results[:results_limit]

            source_scraped_count = 0
            for result in results:
                if not result.success:
                    stats["scrape_errors"] += 1
                    logger.warning("%s scrape error: %s", source_name, result.error)
                    continue
                raw_id = self.repo.save_raw(
                    source=source_name, raw_data=result.raw_data, source_id=result.source_id,
                )
                raw_records.append((raw_id, source_name, result.raw_data))
                stats["scraped"] += 1
                source_scraped_count += 1

            self.repo.record_source_query_run(source_name, query, results_count=source_scraped_count)

        return raw_records

    # ═════════════════════════════════════════════════════
    # Stages 2+3: normalise, then dedup/merge/create
    # ═════════════════════════════════════════════════════

    def _normalise_and_dedup_stage(self, raw_records: List[tuple], stats: Dict[str, Any]) -> None:
        for raw_id, source_name, raw_data in raw_records:
            normalizer = self.normalizers.get(source_name)
            if normalizer is None:
                logger.warning("No normalizer configured for source '%s' — leaving raw_id=%s pending", source_name, raw_id)
                continue

            try:
                if source_name in TRADE_SOURCES:
                    candidate = normalizer.normalise(raw_data, source=source_name)
                else:
                    candidate = normalizer.normalise(raw_data)
            except Exception as e:
                logger.error("Normalisation failed for raw_id=%s (%s): %s", raw_id, source_name, e)
                self.repo.mark_raw_processed(raw_id, status="failed", error_message=str(e))
                continue

            stats["normalised"] += 1

            if not candidate.get("canonical_name"):
                stats["skipped_no_name"] += 1
                self.repo.mark_raw_processed(raw_id, status="failed", error_message="missing canonical_name")
                continue

            try:
                resolution = self.matcher.resolve_and_store(candidate)
            except Exception as e:
                logger.error("Dedup/store failed for raw_id=%s (%s): %s", raw_id, source_name, e)
                self.repo.mark_raw_processed(raw_id, status="failed", error_message=str(e))
                continue

            action = resolution["action"]
            stats[action] = stats.get(action, 0) + 1

            resolved_supplier_id = resolution.get("supplier_id") or resolution.get("new_supplier_id")
            self.repo.mark_raw_processed(raw_id, golden_record_id=resolved_supplier_id, status="processed")

            if source_name in TRADE_SOURCES and resolved_supplier_id:
                shipment = normalizer.to_shipment_record(
                    raw_data, supplier_id=resolved_supplier_id, source=source_name,
                )
                self.repo.add_shipment_record(shipment)
                stats["shipment_records"] += 1

    # ═════════════════════════════════════════════════════
    # Stage 4: verification
    # ═════════════════════════════════════════════════════

    def _verification_stage(self, stats: Dict[str, Any]) -> None:
        if not (self.qichacha.app_key and self.qichacha.app_secret):
            logger.info("Skipping verification stage: Qichacha credentials not configured.")
            return

        for supplier in self.repo.get_unverified_chinese():
            try:
                verification = self.qichacha.verify(supplier["uscc"])
            except Exception as e:
                logger.error("Qichacha verification failed for supplier #%s: %s", supplier["id"], e)
                continue
            self.repo.update_verification(supplier["id"], verification)
            if verification.get("uscc_verified"):
                stats["verified"] += 1

    def run_verification_only(self) -> Dict[str, Any]:
        """Standalone re-verification pass, useful for a scheduled job
        that re-checks suppliers without re-scraping anything."""
        stats = {"verified": 0}
        self._verification_stage(stats)
        return stats

    # ═════════════════════════════════════════════════════
    # Stage 4.5: manufacturer verification
    # ═════════════════════════════════════════════════════

    def _manufacturer_verification_stage(self, stats: Dict[str, Any], force: bool = False) -> None:
        for supplier in self.repo.get_suppliers_needing_manufacturer_assessment(force=force):
            try:
                result = self.manufacturer_verifier.assess(supplier)
            except Exception as e:
                logger.error("Manufacturer assessment failed for supplier #%s: %s", supplier["id"], e)
                continue
            self.repo.update_manufacturer_verification(supplier["id"], result)
            stats["manufacturer_assessed"] += 1

    def run_manufacturer_assessment_only(self, force: bool = False) -> Dict[str, Any]:
        """Standalone re-assessment pass — e.g. after a batch of Qichacha
        verifications populated business_scope/registered_capital_rmb
        for suppliers that were scraped before this stage existed.
        Pass force=True to re-assess every supplier, not just
        never-assessed ones (see the docstring on
        SupplierRepository.get_suppliers_needing_manufacturer_assessment)."""
        stats = {"manufacturer_assessed": 0}
        self._manufacturer_verification_stage(stats, force=force)
        return stats

    # ═════════════════════════════════════════════════════
    # Stage 4.5: company website discovery (fills in a domain when
    # Alibaba/IndiaMart/Made-in-China gave a name but no usable site)
    # ═════════════════════════════════════════════════════

    def _website_discovery_stage(
        self, stats: Dict[str, Any], force: bool = False, limit: int = 1000
    ) -> None:
        for supplier in self.repo.get_suppliers_needing_website_search(limit=limit, force=force):
            name = supplier.get("canonical_name")
            try:
                result = self.website_finder.find_website(name, country=supplier.get("country"))
                if result.validated and result.domain:
                    self.repo.update_supplier_fields(supplier["id"], {"domain": result.domain})
                    stats["website_discovered"] += 1
                else:
                    logger.info(
                        "Website search for supplier #%s (%r) did not validate: %s",
                        supplier["id"], name, result.reason,
                    )
                self.repo.mark_website_search_attempted(supplier["id"])
            except Exception as e:
                logger.error(
                    "Website discovery failed for supplier #%s: %s", supplier["id"], e
                )
                continue

    def run_website_discovery_only(self, force: bool = False, limit: int = 1000) -> Dict[str, Any]:
        """Standalone pass across every domain-less supplier -- run
        this before `run_capability_extraction_only()` if you want a
        clean two-step sweep rather than opting both flags into a
        single `run()` call."""
        stats = {"website_discovered": 0}
        self._website_discovery_stage(stats, force=force, limit=limit)
        return stats

    # ═════════════════════════════════════════════════════
    # Stage 4.6: own-website capability extraction
    # ═════════════════════════════════════════════════════

    def _capability_extraction_stage(
        self, stats: Dict[str, Any], force: bool = False,
        limit: int = 1000, assess_photos: bool = True,
    ) -> None:
        """`limit` and `assess_photos` exist for cost control on a
        first/calibration run: photo assessment sends up to
        `_MAX_IMAGES_PER_PAGE` images per page to a vision model, which
        is by far the most expensive part of this stage and the least
        validated. Running a small `limit` with `assess_photos=False`
        first, checking the output, and only then widening is the
        cheap way to find out whether the extraction is any good
        before paying for it across a whole catalogue.

        `force=True` also clears every existing capability finding for
        each supplier being re-processed before re-extracting -- see
        `SupplierRepository.clear_capabilities`'s own docstring for why
        this is necessary: `add_capability_finding`'s `INSERT OR
        IGNORE` never updates or removes a stale finding on its own, so
        without this, --force could re-run the AI call forever without
        ever letting a prompt or logic fix's effect actually become
        visible in the results.
        """
        for supplier in self.repo.get_suppliers_needing_capability_extraction(limit=limit, force=force):
            if force:
                self.repo.clear_capabilities(supplier["id"])
            domain = supplier.get("domain")
            try:
                fetch_result = self.own_website_scraper.fetch(domain)
                if not fetch_result.success:
                    logger.warning(
                        "Own-website fetch failed for supplier #%s (%s): %s",
                        supplier["id"], domain, fetch_result.error,
                    )
                    # Still mark as attempted — a site that's down or
                    # gone is a real, durable fact about this supplier
                    # right now, not a reason to retry it every single
                    # run forever. Re-run with force=True later if you
                    # suspect it was transient.
                    self.repo.mark_capability_extraction_attempted(supplier["id"])
                    continue

                findings = self.capability_extractor.extract_from_pages(fetch_result.pages)
                for finding in findings:
                    inserted = self.repo.add_capability_finding(
                        supplier["id"],
                        {
                            "reported_term": finding.reported_term,
                            "canonical_term": finding.canonical_term,
                            "category": finding.category,
                            "relationship": finding.relationship,
                            "confidence": finding.confidence,
                            "evidence": finding.evidence,
                            "source_url": finding.source_url,
                        },
                    )
                    if inserted is not None:
                        stats["capability_extracted"] += 1

                # Contact-detail extraction reuses the exact same
                # fetched pages above -- no second HTTP round trip, and
                # no LLM call at all (see website_contact_extractor's
                # own module docstring for why that's a deliberate
                # design choice, not an oversight). The pages were
                # already paid for by the capability-extraction fetch;
                # this is free additional value from that same cost.
                region_hint = country_name_to_region_code(supplier.get("country"))
                contact_findings = extract_contact_details(fetch_result.pages, default_region=region_hint)
                all_emails: list = []
                all_phones: list = []
                for contact_finding in contact_findings:
                    for email in contact_finding.emails:
                        if email not in all_emails:
                            all_emails.append(email)
                    for phone in contact_finding.phone_numbers:
                        if phone not in all_phones:
                            all_phones.append(phone)

                # best_contact_method's own priority order (email, then
                # phone, then contact form) decides whether a form URL
                # is even worth passing through -- enrich_contact_details
                # applies the same priority again on the stored record,
                # so a form URL passed here is only ever used as the
                # true last resort, never displacing a real email.
                fallback = best_contact_method(contact_findings)
                form_url = fallback["value"] if fallback["method"] == "contact_form" else None

                if all_emails or all_phones or form_url:
                    enrichment = self.repo.enrich_contact_details(
                        supplier["id"], emails=all_emails, phones=all_phones, contact_form_url=form_url,
                    )
                    if enrichment["primary_email_set"] or enrichment["secondary_emails_added"]:
                        stats["contact_emails_added"] += 1
                    if enrichment["primary_phone_set"]:
                        stats["contact_phones_added"] += 1
                    if enrichment["contact_form_url_set"]:
                        stats["contact_forms_recorded"] += 1

                # Photo verification reuses the same fetched pages a
                # third time -- own_website_scraper collected
                # image_urls at fetch time specifically so this could
                # happen at zero extra HTTP cost. Writes into the
                # factory_photo_urls/verdict/assessed_at columns that
                # have existed since Phase 2 with no caller until now.
                image_urls: list = []
                if assess_photos:
                    for page in fetch_result.pages:
                        for url in page.image_urls:
                            if url not in image_urls:
                                image_urls.append(url)
                if image_urls:
                    downloaded = self.photo_downloader.download_all(image_urls)
                    photos_for_assessment = [d.as_assessment_input() for d in downloaded if d.success]
                    if photos_for_assessment:
                        assessment = self.factory_photo_verifier.assess_photos(
                            photos_for_assessment,
                            product_category=", ".join(supplier.get("primary_categories") or []),
                            company_name=supplier.get("canonical_name") or "",
                        )
                        self.repo.record_factory_photo_assessment(
                            supplier["id"], photo_urls=image_urls, verdict=assessment["verdict"],
                        )
                        stats["photos_assessed"] += 1

                self.repo.mark_capability_extraction_attempted(supplier["id"])
            except Exception as e:
                logger.error(
                    "Capability extraction failed for supplier #%s: %s", supplier["id"], e
                )
                continue

    def run_capability_extraction_only(
        self, force: bool = False, limit: int = 1000, assess_photos: bool = True,
    ) -> Dict[str, Any]:
        """Standalone pass, run separately from run() given the
        cost/traffic profile noted on run()'s own docstring — e.g. a
        weekly job that walks the whole supplier catalogue's own
        websites once, independent of any particular search query.

        See `_capability_extraction_stage` for why `limit` and
        `assess_photos` matter on a first run.
        """
        stats = {
            "capability_extracted": 0, "contact_emails_added": 0,
            "contact_phones_added": 0, "contact_forms_recorded": 0, "photos_assessed": 0,
        }
        self._capability_extraction_stage(
            stats, force=force, limit=limit, assess_photos=assess_photos
        )
        return stats

    # ═════════════════════════════════════════════════════
    # Stage 4.7: facility address verification (Google Places outside
    # China, Amap within it -- see facility_address_verifier's own
    # module docstring for why, and for the real registration friction
    # to expect on the China path specifically)
    # ═════════════════════════════════════════════════════

    def _facility_verification_stage(
        self, stats: Dict[str, Any], force: bool = False, limit: int = 1000,
    ) -> None:
        for supplier in self.repo.get_suppliers_needing_facility_address_verification(force=force, limit=limit):
            try:
                verifier = select_address_verifier(
                    supplier.get("country"), self.google_places_verifier, self.amap_verifier
                )
                result = verifier.verify(
                    supplier.get("address") or "", company_name=supplier.get("canonical_name") or "",
                )
                self.repo.mark_facility_address_verification_attempted(
                    supplier["id"], verified=result.verified, source=result.source,
                )
                if result.verified:
                    stats["facility_address_verified"] += 1
            except Exception as e:
                logger.error(
                    "Facility address verification failed for supplier #%s: %s", supplier["id"], e
                )
                continue

    def run_facility_verification_only(self, force: bool = False, limit: int = 1000) -> Dict[str, Any]:
        """Standalone pass across every supplier with an address on
        file that hasn't been checked yet. Address verification only --
        photo verification runs as part of
        run_capability_extraction_only, since it reuses the same
        website fetch rather than needing its own pass."""
        stats = {"facility_address_verified": 0}
        self._facility_verification_stage(stats, force=force, limit=limit)
        return stats

    # ═════════════════════════════════════════════════════
    # Stage 4.8: LinkedIn presence checking
    # ═════════════════════════════════════════════════════

    def _linkedin_check_stage(self, stats: Dict[str, Any], force: bool = False) -> None:
        for supplier in self.repo.get_suppliers_needing_linkedin_check(force=force):
            try:
                result = self.linkedin_checker.check(supplier.get("canonical_name") or "")
                self.repo.mark_linkedin_checked(
                    supplier["id"],
                    linkedin_url=result.linkedin_url if result.presence_confirmed else None,
                )
                if result.presence_confirmed:
                    stats["linkedin_checked"] += 1
            except Exception as e:
                logger.error("LinkedIn check failed for supplier #%s: %s", supplier["id"], e)
                continue

    def run_linkedin_check_only(self, force: bool = False) -> Dict[str, Any]:
        stats = {"linkedin_checked": 0}
        self._linkedin_check_stage(stats, force=force)
        return stats

    # ═════════════════════════════════════════════════════
    # Stage 5: scoring
    # ═════════════════════════════════════════════════════

    def _score_suppliers(self, suppliers: List[Dict[str, Any]], stats: Dict[str, Any]) -> None:
        """Shared by _scoring_stage and run_full_rescore -- batch-fetches
        the provenance (sources) and self-asserted-verification
        (capability_findings) inputs SupplierScorer.score() needs, once
        per call rather than once per supplier."""
        supplier_ids = [s["id"] for s in suppliers]
        sources_by_id = self.repo.get_sources_by_supplier(supplier_ids)
        findings_by_id = self.repo.get_capability_findings_by_supplier(supplier_ids)
        for supplier in suppliers:
            scores = self.scorer.score(
                supplier,
                sources=sources_by_id.get(supplier["id"]),
                capability_findings=findings_by_id.get(supplier["id"]),
            )
            self.repo.update_scores(supplier["id"], scores)
            stats["scored"] += 1

    def _scoring_stage(self, stats: Dict[str, Any]) -> None:
        self._score_suppliers(self.repo.get_unscored(), stats)

    def run_scoring_only(self) -> Dict[str, Any]:
        """Standalone re-scoring pass — e.g. after tweaking
        config.settings.SCORING_WEIGHTS, or after a batch of manual
        edits. Only rescoring currently-unscored suppliers keeps this
        cheap; see run_full_rescore() for a full-database rescore after
        changing the scoring logic itself."""
        stats = {"scored": 0}
        self._scoring_stage(stats)
        return stats

    def run_full_rescore(self) -> Dict[str, Any]:
        """Re-score every supplier regardless of composite_score --
        get_unscored() only catches composite_score == 0 rows, which
        misses every supplier that already has a score under a
        since-changed formula (e.g. after rewriting
        verification/scorer.py or changing SCORING_WEIGHTS)."""
        stats = {"scored": 0}
        self._score_suppliers(self.repo.list_suppliers(limit=1_000_000), stats)
        return stats

    # ═════════════════════════════════════════════════════
    # Certificate maintenance
    # ═════════════════════════════════════════════════════

    def check_certificates(self, days_ahead: int = 90) -> Dict[str, Any]:
        """Convenience wrapper around CertChecker for a periodic
        maintenance job — not part of the main scrape/score run since
        it doesn't touch newly-scraped data."""
        needing_recheck = self.cert_checker.get_suppliers_needing_recheck(days_ahead=days_ahead)
        malformed_e_mark = self.cert_checker.get_suppliers_with_malformed_e_mark()
        return {
            "iso_9001_needing_recheck": needing_recheck,
            "malformed_e_mark": malformed_e_mark,
        }
