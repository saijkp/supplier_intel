"""
sourcing/sourcing_agent.py

The chat-driven sourcing loop: turns one free-text brief into a
qualified, enriched, downloadable supplier list until `target_count`
qualified suppliers are found or a hard `target_count * max_multiplier`
candidate-examination ceiling is hit -- an explicit cost governance
decision, not a soft target, so a niche brief can honestly fall short
rather than run (and spend) without bound. See
.claude/plans/deep-wibbling-rivest.md for the full design.

Two candidate sources, tried in order, sharing the same examined/
qualified/ceiling budget:
1. The EXISTING supplier database (`storage.repository.
   search_suppliers_full`) -- near-zero cost (no SerpAPI search, no
   candidate-validation LLM call). A match verified within
   FRESHNESS_DAYS skips re-collection/re-verification entirely.
2. Fresh AI Discovery (discovery.discovery_service.DiscoveryService),
   only for whatever shortfall remains once phase 1 is exhausted.

Before either phase, a small trade-shipment enrichment pass
(`_run_trade_shipment_enrichment`) runs once for the brief's product --
real UK/US/EU customs evidence (no paid API) merges onto matching
supplier rows via the SAME dedup engine every other source already
uses, feeding a new cross-check sub-check (see
verification_ai/cross_checker.py) before either phase evaluates anyone.

What "qualified" means, in order, regardless of which phase a
candidate came from:
1. If freshly discovered: passed Discovery Service's own validation
   gates (real search hit, real fetched page, name+product
   corroboration, AND not a self-declared trading company/distributor;
   see discovery/candidate_validator.py).
2. Collection Service successfully visited the site and Verification
   Service ran its cross-checks (both already-existing services, called
   exactly as any other caller would) -- skipped for an existing
   supplier already fresh enough (see FRESHNESS_DAYS).
3. NOT a confirmed trader (from ManufacturerVerifier/Qichacha,
   China-only data, so this only ever excludes on a POSITIVE
   confirmation, never on "we don't know").
4. Has evidence of every one of the brief's required_capabilities (if
   any were stated) -- capability extraction is run here (reusing
   verification.capability_extractor.CapabilityExtractor exactly as
   pipeline/orchestrator.py's own capability-extraction stage does)
   since neither Collection nor Verification Service currently runs it.

A qualifying supplier gets a SourcingDossier written onto its
sourcing_* fields via update_supplier_fields_with_history (audited,
diffed -- same discipline as every other write in verification_ai/).
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from sourcing.brief_parser import BriefParser, BriefParsingError
from sourcing.dossier_generator import SourcingDossierGenerator
from sourcing.schemas import SourcingOutcome, SourcingProgress, StructuredBrief
from storage.repository import SupplierRepository
from verification_ai.cross_checker import CrossCheckResult, SubCheckResult

logger = logging.getLogger(__name__)

DEFAULT_MAX_MULTIPLIER = 5

# Cap on trade-shipment records fetched per source (importyeti/volza) for
# the once-per-run bulk pass -- see run()'s own comment on why this runs
# once, product-level, rather than once per candidate.
TRADE_RESULTS_LIMIT = 40

# How recently an existing supplier must have been verified to skip
# re-collecting/re-verifying it during the "check the existing database
# first" phase -- same threshold concept
# storage.repository.get_suppliers_needing_reverification's own
# --older-than-days sweep already uses, just evaluated in Python here
# since the row is already loaded.
FRESHNESS_DAYS = 30


def _has_all_required_capabilities(
    capability_findings: List[Dict[str, Any]], required_capabilities: List[str],
) -> bool:
    found = {f.get("canonical_term") for f in capability_findings if f.get("canonical_term")}
    return all(term in found for term in required_capabilities)


def _is_stale(supplier: Dict[str, Any]) -> bool:
    """Never verified, or verified more than FRESHNESS_DAYS ago."""
    last_verified = supplier.get("last_verified")
    if not last_verified:
        return True
    try:
        verified_at = datetime.fromisoformat(last_verified)
    except (ValueError, TypeError):
        return True
    if verified_at.tzinfo is None:
        verified_at = verified_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - verified_at > timedelta(days=FRESHNESS_DAYS)


class SourcingAgentService:

    def __init__(
        self,
        repo: Optional[SupplierRepository] = None,
        discovery_service: Optional[Any] = None,
        collection_service: Optional[Any] = None,
        verification_service: Optional[Any] = None,
        brief_parser: Optional[BriefParser] = None,
        dossier_generator: Optional[SourcingDossierGenerator] = None,
        capability_extractor: Optional[Any] = None,
        own_website_scraper: Optional[Any] = None,
        trade_pipeline: Optional[Any] = None,
    ):
        self.repo = repo or SupplierRepository()

        if discovery_service is not None:
            self.discovery_service = discovery_service
        else:
            from discovery.discovery_service import DiscoveryService

            self.discovery_service = DiscoveryService(repo=self.repo)

        if collection_service is not None:
            self.collection_service = collection_service
        else:
            from collection.collection_service import CollectionService

            self.collection_service = CollectionService(repo=self.repo)

        if verification_service is not None:
            self.verification_service = verification_service
        else:
            from verification_ai.verification_service import VerificationService

            self.verification_service = VerificationService(repo=self.repo)

        self.brief_parser = brief_parser or BriefParser()
        self.dossier_generator = dossier_generator or SourcingDossierGenerator()

        if capability_extractor is not None:
            self.capability_extractor = capability_extractor
        else:
            from verification.capability_extractor import CapabilityExtractor

            self.capability_extractor = CapabilityExtractor()

        if own_website_scraper is not None:
            self.own_website_scraper = own_website_scraper
        else:
            from scrapers.own_website_scraper import OwnWebsiteScraper

            self.own_website_scraper = OwnWebsiteScraper()

        if trade_pipeline is not None:
            self.trade_pipeline = trade_pipeline
        else:
            from pipeline.orchestrator import SupplierIntelligencePipeline

            self.trade_pipeline = SupplierIntelligencePipeline(repo=self.repo)

    def _run_trade_shipment_enrichment(self, product: str) -> None:
        """Once per run, before anything else -- a small, no-paid-API
        bulk pass (ImportYeti/Volza are plain HTTP scrapes, not
        Apify/SerpAPI-metered) for `product`, reusing the EXACT existing
        full scrape pipeline (main.py run --sources importyeti,volza)
        rather than any new matching logic. Real UK/US/EU shipment
        evidence merges onto whichever supplier rows match by name via
        the same SupplierMatcher dedup engine everywhere else already
        uses -- both pre-existing catalogue rows and (for rows this same
        sourcing run discovers afterward) any that happen to already
        exist by the time discovery resolves them.

        Own try/except: a trade-data outage or a scraper-selector break
        (see scrapers/global_trade_scraper.py's own docstring -- its
        Volza selectors were never verified against a live site) must
        never block the rest of the run."""
        try:
            self.trade_pipeline.run(
                product, sources=["importyeti", "volza"],
                run_verification=False, run_scoring=False, results_limit=TRADE_RESULTS_LIMIT,
            )
        except Exception as e:  # noqa: BLE001 -- trade-data enrichment is a nice-to-have, never a hard dependency
            logger.warning("sourcing_agent: trade-shipment enrichment failed for %r: %s", product, e)

    def run(
        self, brief_text: str, *, max_multiplier: int = DEFAULT_MAX_MULTIPLIER,
        on_progress: Optional[Callable[[SourcingProgress], None]] = None,
    ) -> SourcingOutcome:
        try:
            brief = self.brief_parser.parse(brief_text)
        except BriefParsingError as e:
            run_id = self.repo.record_sourcing_run(brief_text=brief_text, target_count=0)
            self.repo.complete_sourcing_run(
                run_id, qualified_supplier_ids=[], examined_count=0,
                status="failed", error_message=str(e),
            )
            return SourcingOutcome(
                run_id=run_id, brief=None, qualified_supplier_ids=[],
                examined_count=0, target_count=0, status="failed", error=str(e),
            )

        run_id = self.repo.record_sourcing_run(
            brief_text=brief_text, target_count=brief.target_count,
            structured_brief=dataclasses.asdict(brief),
        )
        self._run_trade_shipment_enrichment(brief.product)
        ceiling = brief.target_count * max_multiplier

        examined = 0
        qualified: List[int] = []
        seen_supplier_ids: set = set()
        countries = brief.countries or [None]

        # Phase 1: check the existing database first -- zero-cost
        # compared to fresh AI Discovery (no SerpAPI search, no
        # candidate-validation LLM call), and the trade-shipment pass
        # above may have just enriched these very rows. Only supplier
        # rows that pass every other qualification gate count against
        # the target; recently-verified ones skip re-collection/
        # re-verification entirely (see _is_stale).
        for country in countries:
            if len(qualified) >= brief.target_count or examined >= ceiling:
                break
            try:
                existing = self.repo.search_suppliers_full(
                    product_query=brief.product, required_capabilities=brief.required_capabilities,
                    country=country, limit=ceiling - examined,
                )
            except Exception as e:  # noqa: BLE001 -- must never block falling through to fresh discovery
                logger.error("sourcing_agent: existing-database search failed for country=%r: %s", country, e)
                continue
            examined = self._process_batch(
                [s["id"] for s in existing], brief, examined=examined, qualified=qualified,
                seen_supplier_ids=seen_supplier_ids, ceiling=ceiling, on_progress=on_progress,
                skip_collection_if_fresh=True,
            )

        # Phase 2: fresh AI Discovery for whatever shortfall remains.
        for country in countries:
            if len(qualified) >= brief.target_count or examined >= ceiling:
                break
            try:
                discovery_outcome = self.discovery_service.discover(
                    brief.product, country=country, max_candidates=ceiling - examined,
                    application=brief.application, key_specifications=brief.key_specifications,
                )
            except Exception as e:  # noqa: BLE001 -- one country's discovery pass failing must never abort the run
                logger.error("sourcing_agent: discovery failed for country=%r: %s", country, e)
                continue
            examined = self._process_batch(
                discovery_outcome.new_supplier_ids, brief, examined=examined, qualified=qualified,
                seen_supplier_ids=seen_supplier_ids, ceiling=ceiling, on_progress=on_progress,
                skip_collection_if_fresh=False,
            )

        self.repo.complete_sourcing_run(run_id, qualified_supplier_ids=qualified, examined_count=examined)
        return SourcingOutcome(
            run_id=run_id, brief=brief, qualified_supplier_ids=qualified,
            examined_count=examined, target_count=brief.target_count, status="completed",
        )

    def _process_batch(
        self, candidate_ids: List[int], brief: StructuredBrief, *,
        examined: int, qualified: List[int], seen_supplier_ids: set, ceiling: int,
        on_progress: Optional[Callable[[SourcingProgress], None]], skip_collection_if_fresh: bool,
    ) -> int:
        """Shared by both the existing-database phase and the fresh-
        discovery phase in run() -- same seen/ceiling/target bookkeeping
        either way, differing only in where candidate_ids came from and
        whether already-fresh suppliers can skip re-collection.
        `qualified` is mutated in place (a list); `examined` is
        returned since it's a plain int."""
        for supplier_id in candidate_ids:
            if supplier_id in seen_supplier_ids:
                continue
            seen_supplier_ids.add(supplier_id)
            if len(qualified) >= brief.target_count or examined >= ceiling:
                break

            examined += 1
            try:
                if self._process_candidate(
                    supplier_id, brief, skip_collection_if_fresh=skip_collection_if_fresh,
                ) is not None:
                    qualified.append(supplier_id)
            except Exception as e:  # noqa: BLE001 -- defence in depth; every sub-call is already fault-isolated
                logger.error("sourcing_agent: processing supplier #%s failed: %s", supplier_id, e)

            if on_progress:
                try:
                    on_progress(SourcingProgress(
                        examined=examined, qualified=len(qualified), target=brief.target_count,
                    ))
                except Exception as e:  # noqa: BLE001 -- a progress-reporting bug must never kill the run
                    logger.error("sourcing_agent: on_progress callback failed: %s", e)

        return examined

    def _process_candidate(
        self, supplier_id: int, brief: StructuredBrief, *, skip_collection_if_fresh: bool = False,
    ) -> Optional[int]:
        """Returns supplier_id if it qualifies, else None. Never raises
        -- called under _process_batch's own per-candidate try/except as
        defence-in-depth, but every sub-call here already degrades
        gracefully on its own (see each service's own module docstring).

        skip_collection_if_fresh=True (the existing-database phase)
        skips collect()/capability-extraction/verify() entirely when
        the record was already verified within FRESHNESS_DAYS -- a
        freshly-discovered candidate (skip_collection_if_fresh=False,
        the default) always refreshes, since it has no prior data at
        all."""
        supplier = self.repo.get_supplier(supplier_id)
        if supplier is None:
            return None

        if not skip_collection_if_fresh or _is_stale(supplier):
            self.collection_service.collect(supplier_id)
            self._extract_and_persist_capabilities(supplier_id)
            self.verification_service.verify(supplier_id)
            supplier = self.repo.get_supplier(supplier_id)
            if supplier is None:
                return None

        # SQLite has no real boolean type -- get_supplier() returns
        # whatever was stored (0/1/None), not Python True/False/None,
        # so an `is False` identity check here would silently never
        # match a confirmed trader. None (unknown) must NOT be treated
        # as a positive manufacturer confirmation either -- it just
        # means "don't exclude on this signal," per the module
        # docstring's point 3.
        is_manufacturer = supplier.get("is_manufacturer")
        if is_manufacturer is not None and not is_manufacturer:
            return None

        capability_findings = self.repo.get_capabilities(supplier_id)
        if brief.required_capabilities and not _has_all_required_capabilities(
            capability_findings, brief.required_capabilities,
        ):
            return None

        cross_check_result = self._latest_cross_check_result(supplier_id)
        dossier = self.dossier_generator.generate(supplier, brief, cross_check_result, capability_findings)
        if dossier is not None:
            self.repo.update_supplier_fields_with_history(
                supplier_id,
                {
                    "sourcing_oem_odm_notes": dossier.oem_odm_capability,
                    "sourcing_factory_notes": dossier.factory_manufacturing_processes,
                    "sourcing_engineering_notes": dossier.engineering_testing_capability,
                    "sourcing_export_notes": dossier.export_experience,
                    "sourcing_volume_suitability": dossier.annual_volume_suitability,
                    "sourcing_payment_terms_notes": dossier.payment_terms_assessment,
                    "sourcing_verification_status": dossier.verification_status,
                },
                changed_by="sourcing_agent",
                change_reason=f"sourcing run for: {brief.product}",
            )

        return supplier_id

    def _extract_and_persist_capabilities(self, supplier_id: int) -> None:
        """Reuses verification.capability_extractor.CapabilityExtractor
        exactly as pipeline/orchestrator.py's own capability-extraction
        stage does -- neither CollectionService nor VerificationService
        currently runs this, so without it, required_capabilities
        filtering and the dossier's OEM/ODM & factory/process narrative
        would always see an empty capability_findings list. A second
        fetch of the supplier's own site (via the same cheap,
        no-JS OwnWebsiteScraper VerificationService already uses for
        its own cross-checks) -- own try/except so a bad page never
        aborts the candidate."""
        supplier = self.repo.get_supplier(supplier_id)
        domain = supplier.get("domain") if supplier else None
        if not domain:
            return
        try:
            fetch_result = self.own_website_scraper.fetch(domain)
            if not fetch_result.success:
                self.repo.mark_capability_extraction_attempted(supplier_id)
                return
            findings = self.capability_extractor.extract_from_pages(fetch_result.pages)
            for finding in findings:
                self.repo.add_capability_finding(
                    supplier_id,
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
            self.repo.mark_capability_extraction_attempted(supplier_id)
        except Exception as e:
            logger.warning("sourcing_agent: capability extraction failed for supplier #%s: %s", supplier_id, e)

    def _latest_cross_check_result(self, supplier_id: int) -> CrossCheckResult:
        """Reconstructs the CrossCheckResult VerificationService.verify()
        already computed and recorded, from verification_history's own
        evidence_json -- avoids a second, duplicate cross-check run (and
        its address/LinkedIn API costs) just to hand the same evidence
        to SourcingDossierGenerator."""
        history = self.repo.get_verification_history(supplier_id, limit=1)
        if not history:
            return CrossCheckResult()
        evidence = history[0].get("evidence_json") or {}
        sub_checks = [
            SubCheckResult(name=c.get("name", ""), verdict=c.get("verdict"), detail=c.get("detail", ""))
            for c in evidence.get("sub_checks", [])
        ]
        return CrossCheckResult(sub_checks=sub_checks, inconsistencies=evidence.get("inconsistencies", []))
