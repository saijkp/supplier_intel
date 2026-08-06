"""
verification/factory_facts_service.py

Orchestrates FactoryFactsExtractor against real suppliers and records
the outcome via storage.repository.SupplierRepository -- same
single/batch/semaphore/idempotency-marker shape as
verification/contact_finder_service.py's ContactFinderService, its
direct template.

A separate, opt-in enrichment stage (like Collect/Verify/Find
contacts) -- never run automatically inside
sourcing/sourcing_agent.py's run() loop. Fetches a supplier's own
website via OwnWebsiteScraper (the same cheap httpx-only fetch every
other standalone extraction stage in this codebase already uses on its
own -- see sourcing_agent.py's own _extract_and_persist_capabilities),
not shared with collection/collection_service.py's Playwright-fetched
pages, since this stage can run independently of a Collect job ever
having been triggered.

Concurrency/safety guards, same reasoning as ContactFinderService (see
its own module docstring for the full rationale):
- A process-wide semaphore caps concurrent find_facts_pending() BATCH
  calls to FACTORY_FACTS_MAX_CONCURRENT_JOBS (default 1).
- A wall-clock budget (FACTORY_FACTS_JOB_MAX_SECONDS) stops a batch
  early, marking it "partial" and safely resumable
  (factory_facts_extracted_at is set on every attempt, matched or not,
  so already-attempted suppliers are skipped next time unless
  force=True).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from config.settings import FACTORY_FACTS_JOB_MAX_SECONDS, FACTORY_FACTS_MAX_CONCURRENT_JOBS
from storage.repository import SupplierRepository
from verification.factory_facts_extractor import FactoryFactsExtractor

logger = logging.getLogger(__name__)

# Shared across FactoryFactsService instances within one process -- see
# module docstring for why this needs to be process-wide, not
# per-instance.
_BATCH_SEMAPHORE = threading.Semaphore(FACTORY_FACTS_MAX_CONCURRENT_JOBS)


class FactoryFactsService:

    def __init__(
        self,
        repo: Optional[SupplierRepository] = None,
        extractor: Optional[FactoryFactsExtractor] = None,
        own_website_scraper: Optional[Any] = None,
        job_max_seconds: int = FACTORY_FACTS_JOB_MAX_SECONDS,
    ):
        self.repo = repo or SupplierRepository()
        self.extractor = extractor or FactoryFactsExtractor()
        if own_website_scraper is not None:
            self.own_website_scraper = own_website_scraper
        else:
            from scrapers.own_website_scraper import OwnWebsiteScraper

            self.own_website_scraper = OwnWebsiteScraper()
        self.job_max_seconds = job_max_seconds

    def find_facts(self, supplier_id: int) -> Dict[str, Any]:
        """Extract factory facts for one supplier by id -- always
        runs, not gated by factory_facts_extracted_at/force (a caller
        who names a specific supplier clearly wants it looked up)."""
        supplier = self.repo.get_supplier(supplier_id)
        if supplier is None:
            raise ValueError(f"No supplier with id={supplier_id}")
        return self._find_facts_one(supplier)

    def _find_facts_one(self, supplier: Dict[str, Any]) -> Dict[str, Any]:
        supplier_id = supplier["id"]
        domain = supplier.get("domain")
        now = datetime.now(timezone.utc).isoformat()

        if not domain:
            self.repo.update_supplier_fields_with_history(
                supplier_id, {"factory_facts_extracted_at": now},
                changed_by="factory_facts_service", change_reason="no domain on file",
            )
            return {"supplier_id": supplier_id, "status": "unavailable", "reason": "no domain on file"}

        try:
            fetch_result = self.own_website_scraper.fetch(domain)
        except Exception as e:  # noqa: BLE001 -- one supplier's fetch failure must never abort a batch
            logger.error("factory_facts_service: own-site fetch failed for supplier #%s: %s", supplier_id, e)
            self.repo.update_supplier_fields_with_history(
                supplier_id, {"factory_facts_extracted_at": now},
                changed_by="factory_facts_service", change_reason=f"own-site fetch failed: {e}",
            )
            return {"supplier_id": supplier_id, "status": "unavailable", "reason": f"own-site fetch failed: {e}"}

        if not fetch_result.success or not fetch_result.pages:
            self.repo.update_supplier_fields_with_history(
                supplier_id, {"factory_facts_extracted_at": now},
                changed_by="factory_facts_service", change_reason="own-site fetch found no usable pages",
            )
            return {"supplier_id": supplier_id, "status": "unavailable", "reason": "own-site fetch found no usable pages"}

        result = self.extractor.extract_from_pages(fetch_result.pages)
        if result is None:
            self.repo.update_supplier_fields_with_history(
                supplier_id, {"factory_facts_extracted_at": now},
                changed_by="factory_facts_service", change_reason="extraction produced no usable result",
            )
            return {"supplier_id": supplier_id, "status": "no_evidence", "reason": "extraction produced no usable result"}

        self.repo.update_supplier_fields_with_history(
            supplier_id,
            {
                "production_lines_notes": result.production_lines_notes,
                "machinery_notes": result.machinery_notes,
                "factory_ownership": result.factory_ownership,
                "factory_facts_extracted_at": now,
            },
            changed_by="factory_facts_service",
            change_reason="factory facts extracted from own website",
        )

        return {
            "supplier_id": supplier_id,
            "status": "extracted",
            "factory_ownership": result.factory_ownership,
        }

    def find_facts_pending(self, limit: int = 20, force: bool = False) -> Dict[str, Any]:
        """Standalone batch pass across every supplier needing
        factory-facts extraction -- mirrors
        ContactFinderService.find_contacts_pending() exactly. See
        module docstring for the concurrency/timeout safeguards this
        applies."""
        acquired = _BATCH_SEMAPHORE.acquire(blocking=False)
        if not acquired:
            return {
                "attempted": 0, "succeeded": 0, "failed": 0, "total_eligible": 0,
                "status": "skipped",
                "reason": "another factory-facts batch is already running on this instance",
            }

        try:
            suppliers = self.repo.get_suppliers_needing_factory_facts(limit=limit, force=force)
            start_time = time.monotonic()
            attempted = 0
            succeeded = 0
            failed = 0
            stopped_early = False

            for supplier in suppliers:
                if time.monotonic() - start_time > self.job_max_seconds:
                    stopped_early = True
                    break
                outcome = self._find_facts_one(supplier)
                attempted += 1
                if outcome["status"] == "extracted":
                    succeeded += 1
                else:
                    failed += 1

            return {
                "attempted": attempted, "succeeded": succeeded, "failed": failed,
                "total_eligible": len(suppliers),
                "status": "partial" if stopped_early else "completed",
            }
        finally:
            _BATCH_SEMAPHORE.release()
