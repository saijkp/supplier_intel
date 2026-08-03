"""
verification_ai/verification_service.py

Orchestrates CrossChecker -> ConfidenceScorer -> NarrativeGenerator
against a real supplier record and writes the result via
storage.repository.SupplierRepository -- the DI'd entry point
main.py's `verify-ai`/`reverify` commands and POST /verification/jobs,
POST /suppliers/{id}/reverify all call into, following the exact same
constructor convention as pipeline.orchestrator.SupplierIntelligencePipeline
and collection.collection_service.CollectionService (every dependency
Optional[...] = None, defaulting to a real implementation, construction
never requires credentials).

Every verify() call writes ONE verification_history row (even a
no-change re-verify -- "we checked and confirmed nothing changed" is
itself a useful audit fact) and routes its supplier-record writes
through SupplierRepository.update_supplier_fields_with_history(), so a
reverify against an already-known supplier (including any of the
legacy Alibaba/1688-sourced suppliers) diffs and appends to
supplier_change_log instead of silently overwriting -- the concrete
mechanism behind "update existing records when reverified" from the
redesign brief.

Deliberately does NOT re-write is_manufacturer/manufacturer_confidence/
manufacturer_signals on the supplier record, even though CrossChecker
re-runs ManufacturerVerifier.assess() as one of its sub-checks -- those
fields belong to the existing, separately-orchestrated
run_manufacturer_assessment_only pipeline stage; this module only
*reads* that assessment as one input signal toward ai_confidence_score,
never overwrites it, avoiding two independent writers fighting over the
same fields.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from storage.repository import SupplierRepository
from verification.facility_address_verifier import AmapAddressVerifier, GooglePlacesAddressVerifier
from verification.linkedin_presence import LinkedInPresenceChecker
from verification.manufacturer_verifier import ManufacturerVerifier
from verification_ai.confidence_scorer import ConfidenceScorer
from verification_ai.cross_checker import CrossChecker
from verification_ai.narrative_generator import NarrativeGenerator

logger = logging.getLogger(__name__)


def _verdict_label(confidence_score: int) -> str:
    """Same bucketing convention ManufacturerVerifier.assess() already
    uses for is_manufacturer (>=70 confirmed, <=30 contradicted, else
    mixed) -- applied here to the independent ai_confidence_score."""
    if confidence_score >= 70:
        return "corroborated"
    if confidence_score <= 30:
        return "contradicted"
    return "mixed"


class VerificationService:

    def __init__(
        self,
        repo: Optional[SupplierRepository] = None,
        cross_checker: Optional[CrossChecker] = None,
        confidence_scorer: Optional[ConfidenceScorer] = None,
        narrative_generator: Optional[NarrativeGenerator] = None,
        own_website_scraper: Optional[Any] = None,
    ):
        self.repo = repo or SupplierRepository()
        self.confidence_scorer = confidence_scorer or ConfidenceScorer()
        self.narrative_generator = narrative_generator or NarrativeGenerator()
        # Real implementations by default, matching
        # SupplierIntelligencePipeline's own DI convention -- construction
        # never requires credentials, only an actual verify() call does.
        self.cross_checker = cross_checker or CrossChecker(
            manufacturer_verifier=ManufacturerVerifier(),
            google_places_verifier=GooglePlacesAddressVerifier(),
            amap_verifier=AmapAddressVerifier(),
            linkedin_checker=LinkedInPresenceChecker(google_scraper=self._default_google_scraper()),
        )
        if own_website_scraper is not None:
            self.own_website_scraper = own_website_scraper
        else:
            from scrapers.own_website_scraper import OwnWebsiteScraper

            self.own_website_scraper = OwnWebsiteScraper()

    @staticmethod
    def _default_google_scraper() -> Any:
        from scrapers.google_search_scraper import GoogleSearchScraper

        return GoogleSearchScraper()

    def verify(self, supplier_id: int) -> Dict[str, Any]:
        """Always runs for a named supplier -- no force/gating concept
        here, matching CollectionService.collect(supplier_id)'s own
        "a caller who names a specific supplier clearly wants it done"
        convention."""
        supplier = self.repo.get_supplier(supplier_id)
        if supplier is None:
            raise ValueError(f"No supplier with id={supplier_id}")
        return self._verify_one(supplier)

    def _verify_one(self, supplier: Dict[str, Any]) -> Dict[str, Any]:
        supplier_id = supplier["id"]
        capability_findings = self.repo.get_capabilities(supplier_id)

        collected_pages = []
        domain = supplier.get("domain")
        if domain:
            try:
                fetch_result = self.own_website_scraper.fetch(domain)
                if fetch_result.success:
                    collected_pages = fetch_result.pages
            except Exception as e:
                logger.warning("verification_ai: own-site fetch failed for supplier #%s: %s", supplier_id, e)

        cross_result = self.cross_checker.run_checks(
            supplier, collected_pages=collected_pages, capability_findings=capability_findings,
        )
        confidence = self.confidence_scorer.score(cross_result)
        narrative = self.narrative_generator.generate(supplier, cross_result, confidence)

        now = datetime.now(timezone.utc).isoformat()
        fields: Dict[str, Any] = {
            "ai_confidence_score": confidence,
            "ai_confidence_assessed_at": now,
            "last_verified": now,
        }
        if narrative is not None:
            fields["ai_summary"] = narrative.summary
            fields["ai_strengths"] = narrative.strengths
            fields["ai_risks"] = narrative.risks
            fields["ai_suitable_customer_types"] = narrative.suitable_customer_types
            fields["ai_verification_model"] = narrative.model_used

        self.repo.update_supplier_fields_with_history(
            supplier_id, fields, changed_by="verification_service",
            change_reason="ai_cross_check",
        )

        self.repo.record_verification_history(
            supplier_id=supplier_id,
            verification_type="ai_cross_check",
            confidence_score=confidence,
            verdict=_verdict_label(confidence),
            summary=narrative.summary if narrative else None,
            evidence={
                "sub_checks": [
                    {"name": c.name, "verdict": c.verdict, "detail": c.detail} for c in cross_result.sub_checks
                ],
                "inconsistencies": cross_result.inconsistencies,
            },
            model_used=narrative.model_used if narrative else None,
        )

        return {
            "supplier_id": supplier_id,
            "confidence_score": confidence,
            "verdict": _verdict_label(confidence),
            "inconsistencies": cross_result.inconsistencies,
            "narrative_generated": narrative is not None,
        }

    def verify_pending(self, limit: int = 20, force: bool = False) -> Dict[str, Any]:
        """Standalone batch pass -- mirrors
        pipeline.orchestrator.run_facility_verification_only's own
        standalone-pass pattern, backed by
        SupplierRepository.get_suppliers_needing_ai_verification."""
        suppliers = self.repo.get_suppliers_needing_ai_verification(limit=limit, force=force)
        attempted = 0
        succeeded = 0
        failed = 0
        for supplier in suppliers:
            attempted += 1
            try:
                self._verify_one(supplier)
                succeeded += 1
            except Exception as e:  # noqa: BLE001 -- one supplier's failure must never abort the batch
                logger.error("verification_ai: verify failed for supplier #%s: %s", supplier["id"], e)
                failed += 1
        return {
            "attempted": attempted, "succeeded": succeeded, "failed": failed,
            "total_eligible": len(suppliers),
        }

    def reverify(self, supplier_id: int, collection_service: Optional[Any] = None) -> Dict[str, Any]:
        """The concrete "update existing records when reverified"
        workflow: re-collect (force=True equivalent -- a named
        supplier's own collect() always runs) then re-verify, both
        routed through update_supplier_fields_with_history so genuine
        changes append to supplier_change_log rather than being lost.
        collection_service is lazily constructed if not given (avoids
        importing Playwright at VerificationService construction time
        for callers that only ever call verify()/verify_pending())."""
        if collection_service is None:
            from collection.collection_service import CollectionService

            collection_service = CollectionService(repo=self.repo)

        collection_outcome = collection_service.collect(supplier_id)
        verification_outcome = self.verify(supplier_id)
        return {"collection": collection_outcome, "verification": verification_outcome}
