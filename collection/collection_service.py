"""
collection/collection_service.py

Orchestrates SiteCollector against real suppliers and records the
outcome via storage.repository.SupplierRepository -- the DI'd
entry point main.py's `collect` command and POST /collection/jobs both
call into, following the exact same pattern
pipeline.orchestrator.SupplierIntelligencePipeline's constructor uses
(every dependency Optional[...] = None, defaulting to a real
implementation, construction never requires credentials).

Concurrency/safety guards for the current single-instance Railway
deployment (no real task queue, see the redesign plan's "Collection
Service safeguards" section):
- Sequential processing only in collect_pending() -- no concurrent
  Playwright contexts in v1 (thread-safety risk, real memory pressure
  on a modest plan from multiple headless Chromium instances).
- A process-wide semaphore caps concurrent collect_pending() BATCH
  calls to COLLECTION_MAX_CONCURRENT_JOBS (default 1) -- so firing two
  HTTP requests at once doesn't launch two simultaneous batches on this
  instance.
- A wall-clock budget (COLLECTION_JOB_MAX_SECONDS) since
  BackgroundTasks has no built-in timeout unlike a real task queue -- a
  batch that runs past budget stops early, reports itself "partial",
  and is safely resumable on the next call (already-collected suppliers
  have collection_status set, so they're skipped next time unless
  force=True).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from collection.proxy_provider import ProxyProvider, select_proxy_provider
from collection.site_collector import SiteCollector
from config.settings import COLLECTION_JOB_MAX_SECONDS, COLLECTION_MAX_CONCURRENT_JOBS
from storage.repository import SupplierRepository

logger = logging.getLogger(__name__)

# Shared across CollectionService instances within one process -- see
# module docstring for why this needs to be process-wide, not
# per-instance.
_BATCH_SEMAPHORE = threading.Semaphore(COLLECTION_MAX_CONCURRENT_JOBS)


class CollectionService:

    def __init__(
        self,
        repo: Optional[SupplierRepository] = None,
        site_collector: Optional[SiteCollector] = None,
        proxy_provider: Optional[ProxyProvider] = None,
        job_max_seconds: int = COLLECTION_JOB_MAX_SECONDS,
    ):
        self.repo = repo or SupplierRepository()
        self.proxy_provider = proxy_provider or select_proxy_provider()
        self.site_collector = site_collector or SiteCollector(proxy_provider=self.proxy_provider)
        self.job_max_seconds = job_max_seconds

    def collect(self, supplier_id: int) -> Dict[str, Any]:
        """Collect against one supplier by id -- always runs, not
        gated by collection_status/force (a caller who names a specific
        supplier clearly wants it collected)."""
        supplier = self.repo.get_supplier(supplier_id)
        if supplier is None:
            raise ValueError(f"No supplier with id={supplier_id}")
        return self._collect_one(supplier)

    def _collect_one(self, supplier: Dict[str, Any]) -> Dict[str, Any]:
        supplier_id = supplier["id"]
        domain = supplier.get("domain")
        started_at = datetime.now(timezone.utc).isoformat()
        provider_name = type(self.proxy_provider).__name__

        if not domain:
            self.repo.record_collection_run(
                supplier_id=supplier_id, status="failed", error_message="no domain on file",
                proxy_provider=provider_name, started_at=started_at,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            return {"supplier_id": supplier_id, "status": "failed", "pages_visited": 0, "error": "no domain on file"}

        try:
            result = self.site_collector.collect(supplier_id, domain)
        except Exception as e:  # noqa: BLE001 -- SiteCollector already never raises; this is defence in depth,
            # matching every other pipeline stage's per-supplier fault isolation in this codebase.
            logger.error("collection: unexpected error for supplier #%s: %s", supplier_id, e)
            self.repo.record_collection_run(
                supplier_id=supplier_id, status="failed", error_message=str(e),
                proxy_provider=provider_name, started_at=started_at,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            return {"supplier_id": supplier_id, "status": "failed", "pages_visited": 0, "error": str(e)}

        completed_at = datetime.now(timezone.utc).isoformat()
        status = "success" if result.success else "failed"
        self.repo.record_collection_run(
            supplier_id=supplier_id, status=status, pages_visited=len(result.pages),
            artifacts_dir=result.artifacts_dir, proxy_provider=result.proxy_provider,
            error_message=result.error, started_at=started_at, completed_at=completed_at,
        )
        return {
            "supplier_id": supplier_id, "status": status,
            "pages_visited": len(result.pages), "error": result.error,
        }

    def collect_pending(self, limit: int = 20, force: bool = False) -> Dict[str, Any]:
        """Standalone batch pass across every supplier needing
        collection -- mirrors pipeline.orchestrator's own
        run_capability_extraction_only/run_facility_verification_only
        standalone-pass pattern. See module docstring for the
        concurrency/timeout safeguards this applies.
        """
        acquired = _BATCH_SEMAPHORE.acquire(blocking=False)
        if not acquired:
            return {
                "attempted": 0, "succeeded": 0, "failed": 0, "total_eligible": 0,
                "status": "skipped",
                "reason": "another collection batch is already running on this instance",
            }

        try:
            suppliers = self.repo.get_suppliers_needing_collection(limit=limit, force=force)
            start_time = time.monotonic()
            attempted = 0
            succeeded = 0
            failed = 0
            stopped_early = False

            for supplier in suppliers:
                if time.monotonic() - start_time > self.job_max_seconds:
                    stopped_early = True
                    break
                outcome = self._collect_one(supplier)
                attempted += 1
                if outcome["status"] == "success":
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
