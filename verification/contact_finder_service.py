"""
verification/contact_finder_service.py

Orchestrates ApolloContactFinder against real suppliers and records the
outcome via storage.repository.SupplierRepository -- the DI'd entry
point main.py's `find-contacts` command and POST /contacts/jobs both
call into. Same single/batch/semaphore/idempotency-marker shape as
collection/collection_service.py's CollectionService, its direct
template.

A separate, opt-in enrichment stage (like Collect/Verify) -- never run
automatically inside sourcing/sourcing_agent.py's run() loop. See
verification/apollo_contact_finder.py's module docstring for the
Apollo API contract and cost governance this wraps.

Concurrency/safety guards, same reasoning as CollectionService (see its
own module docstring for the full rationale -- this repeats only the
specifics):
- A process-wide semaphore caps concurrent find_contacts_pending()
  BATCH calls to CONTACTS_MAX_CONCURRENT_JOBS (default 1).
- A wall-clock budget (CONTACTS_JOB_MAX_SECONDS) stops a batch early,
  marking it "partial" and safely resumable (contacts_found_at is set
  on every attempt, matched or not, so already-attempted suppliers are
  skipped next time unless force=True).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from config.settings import CONTACTS_JOB_MAX_SECONDS, CONTACTS_MAX_CONCURRENT_JOBS
from storage.repository import SupplierRepository
from verification.apollo_contact_finder import ApolloContactFinder

logger = logging.getLogger(__name__)

# Shared across ContactFinderService instances within one process -- see
# module docstring for why this needs to be process-wide, not
# per-instance.
_BATCH_SEMAPHORE = threading.Semaphore(CONTACTS_MAX_CONCURRENT_JOBS)


class ContactFinderService:

    def __init__(
        self,
        repo: Optional[SupplierRepository] = None,
        apollo_finder: Optional[ApolloContactFinder] = None,
        job_max_seconds: int = CONTACTS_JOB_MAX_SECONDS,
    ):
        self.repo = repo or SupplierRepository()
        self.apollo_finder = apollo_finder or ApolloContactFinder()
        self.job_max_seconds = job_max_seconds

    def find_contacts(self, supplier_id: int) -> Dict[str, Any]:
        """Find contacts for one supplier by id -- always runs, not
        gated by contacts_found_at/force (a caller who names a specific
        supplier clearly wants it looked up)."""
        supplier = self.repo.get_supplier(supplier_id)
        if supplier is None:
            raise ValueError(f"No supplier with id={supplier_id}")
        return self._find_contacts_one(supplier)

    def _find_contacts_one(self, supplier: Dict[str, Any]) -> Dict[str, Any]:
        supplier_id = supplier["id"]
        company_name = supplier.get("canonical_name") or ""
        domain = supplier.get("domain")

        result = self.apollo_finder.find_contacts(company_name, domain=domain)

        contacts_payload = [
            {
                "name": c.name, "title": c.title, "email": c.email,
                "phone": c.phone, "linkedin_url": c.linkedin_url,
                "role_category": c.role_category,
            }
            for c in result.contacts
        ]

        self.repo.update_supplier_fields_with_history(
            supplier_id,
            {
                "key_contacts": contacts_payload,
                "contacts_found_at": datetime.now(timezone.utc).isoformat(),
            },
            changed_by="contact_finder_service",
            change_reason=result.reason,
        )

        return {
            "supplier_id": supplier_id,
            "status": result.source,
            "contacts_found": len(contacts_payload),
            "reason": result.reason,
        }

    def find_contacts_pending(self, limit: int = 20, force: bool = False) -> Dict[str, Any]:
        """Standalone batch pass across every supplier needing contact
        lookup -- mirrors CollectionService.collect_pending() exactly.
        See module docstring for the concurrency/timeout safeguards
        this applies."""
        acquired = _BATCH_SEMAPHORE.acquire(blocking=False)
        if not acquired:
            return {
                "attempted": 0, "succeeded": 0, "failed": 0, "total_eligible": 0,
                "status": "skipped",
                "reason": "another contacts batch is already running on this instance",
            }

        try:
            suppliers = self.repo.get_suppliers_needing_contacts(limit=limit, force=force)
            start_time = time.monotonic()
            attempted = 0
            succeeded = 0
            failed = 0
            stopped_early = False

            for supplier in suppliers:
                if time.monotonic() - start_time > self.job_max_seconds:
                    stopped_early = True
                    break
                outcome = self._find_contacts_one(supplier)
                attempted += 1
                if outcome["status"] == "apollo":
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
