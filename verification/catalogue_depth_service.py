"""
verification/catalogue_depth_service.py

Orchestrates CatalogueDepthExtractor against real suppliers and
records the outcome via storage.repository.SupplierRepository -- same
single/batch/semaphore/idempotency-marker shape as
verification/factory_facts_service.py's FactoryFactsService, its
direct template.

Deliberately reads from a supplier's already-collected pages on disk
(collection.site_collector.SiteCollector's own HTML artifacts under
COLLECTION_ARTIFACTS_DIR/<supplier_id>/<run_id>/) rather than
FactoryFactsService's live OwnWebsiteScraper.fetch() -- the whole
point of this stage is catalogue-depth signals from pages already
paid for and crawled during a prior `batch-upload`/`collect` run, not
a second fetch of the same site. A supplier with no successful
collection_runs row on file has nothing to read and is marked
attempted with status "unavailable" -- this stage never falls back to
a live crawl.

Source URL caveat (read before relying on the evidence column's
links): SiteCollector's own artifact filenames are a lossy, truncated
slug of the original URL (see collection/artifact_store.py's
`_slugify`, capped at 40 chars) -- the real URL a given HTML file was
fetched from is never persisted anywhere after that collection run
ends (confirmed: neither `collection_runs` nor any other table stores
a per-page URL, only a `pages_visited` count and the run's
`artifacts_dir`). Reconstructing a guessed web URL from that slug and
presenting it as the real source would violate this codebase's own
grounded-extraction discipline (never present an inferred value as a
stated fact -- see CLAUDE.md standing rule 3, applied here to the
SOURCE itself, not just the extracted content). So `source_url` on
every finding from this stage is the LOCAL ARTIFACT FILE PATH, not a
web URL -- genuinely traceable (open it directly to verify the
extraction), but not clickable the way every other evidence column's
source_url is. Worth fixing at the SiteCollector layer (persist a
real URL manifest per run) if this evidence needs to be reviewed by
someone without local file access.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import COLLECTION_ARTIFACTS_DIR, CATALOGUE_DEPTH_JOB_MAX_SECONDS, CATALOGUE_DEPTH_MAX_CONCURRENT_JOBS
from storage.repository import SupplierRepository
from verification.catalogue_depth_extractor import CatalogueDepthExtractor
from verification.website_contact_extractor import parking_page_reason

logger = logging.getLogger(__name__)

# Shared across CatalogueDepthService instances within one process --
# see module docstring for why this needs to be process-wide, not
# per-instance.
_BATCH_SEMAPHORE = threading.Semaphore(CATALOGUE_DEPTH_MAX_CONCURRENT_JOBS)


@dataclass
class _ArtifactPage:
    """Minimal `.url`/`.text`-shaped object -- exactly what
    CatalogueDepthExtractor.extract_from_pages expects, built from an
    on-disk HTML artifact rather than a live fetch. `url` is the local
    artifact file path, not a web URL -- see module docstring."""
    url: str
    text: str


class CatalogueDepthService:

    def __init__(
        self,
        repo: Optional[SupplierRepository] = None,
        extractor: Optional[CatalogueDepthExtractor] = None,
        artifacts_dir: Optional[Path] = None,
        job_max_seconds: int = CATALOGUE_DEPTH_JOB_MAX_SECONDS,
    ):
        self.repo = repo or SupplierRepository()
        self.extractor = extractor or CatalogueDepthExtractor()
        self.artifacts_dir = Path(artifacts_dir) if artifacts_dir is not None else COLLECTION_ARTIFACTS_DIR
        self.job_max_seconds = job_max_seconds

    def extract(self, supplier_id: int) -> Dict[str, Any]:
        """Extract catalogue-depth signals for one supplier by id --
        always runs, not gated by catalogue_depth_extracted_at/force (a
        caller who names a specific supplier clearly wants it looked
        up)."""
        supplier = self.repo.get_supplier(supplier_id)
        if supplier is None:
            raise ValueError(f"No supplier with id={supplier_id}")
        return self._extract_one(supplier)

    def _load_artifact_pages(self, supplier_id: int) -> List[_ArtifactPage]:
        """Most recent SUCCESSFUL collection run's HTML artifacts,
        converted to text via the same html_to_text helper
        scrapers.own_website_scraper uses for its own freshly-fetched
        pages. Empty list (never raises) if there's no successful run,
        the directory is missing, or it has no .html files on disk --
        all real, coverable-by-a-caller outcomes, not exceptional
        ones."""
        from scrapers.own_website_scraper import html_to_text

        runs = self.repo.get_collection_runs(supplier_id, limit=50)
        latest_success = next((r for r in runs if r.get("status") == "success" and r.get("artifacts_dir")), None)
        if latest_success is None:
            return []

        run_dir = self.artifacts_dir / latest_success["artifacts_dir"]
        if not run_dir.is_dir():
            return []

        pages = []
        for html_path in sorted(run_dir.glob("*.html")):
            try:
                html = html_path.read_text(encoding="utf-8", errors="ignore")
            except OSError as e:
                logger.warning("catalogue_depth_service: could not read %s: %s", html_path, e)
                continue
            pages.append(_ArtifactPage(url=str(html_path), text=html_to_text(html)))
        return pages

    def _extract_one(self, supplier: Dict[str, Any]) -> Dict[str, Any]:
        supplier_id = supplier["id"]
        now = datetime.now(timezone.utc).isoformat()

        pages = self._load_artifact_pages(supplier_id)
        if not pages:
            self.repo.mark_catalogue_depth_extraction_attempted(supplier_id)
            return {
                "supplier_id": supplier_id, "status": "unavailable",
                "reason": "no successful collection artifacts on disk for this supplier",
            }

        usable_pages = []
        skipped_parking = 0
        for page in pages:
            reason = parking_page_reason(page.text)
            if reason:
                skipped_parking += 1
                continue
            usable_pages.append(page)

        findings = self.extractor.extract_from_pages(usable_pages)

        stored = 0
        for finding in findings:
            row_id = self.repo.add_catalogue_signal_finding(
                supplier_id,
                {"signal_type": finding.signal_type, "evidence": finding.evidence, "source_url": finding.source_url},
            )
            if row_id is not None:
                stored += 1

        self.repo.mark_catalogue_depth_extraction_attempted(supplier_id)
        return {
            "supplier_id": supplier_id, "status": "extracted",
            "pages_read": len(pages), "pages_skipped_parking": skipped_parking,
            "findings_stored": stored,
        }

    def extract_pending(self, limit: int = 20, force: bool = False) -> Dict[str, Any]:
        """Standalone batch pass across every supplier needing
        catalogue-depth extraction -- mirrors
        FactoryFactsService.find_facts_pending() exactly. See module
        docstring for the concurrency/timeout safeguards this
        applies."""
        acquired = _BATCH_SEMAPHORE.acquire(blocking=False)
        if not acquired:
            return {
                "attempted": 0, "extracted": 0, "unavailable": 0, "total_eligible": 0,
                "status": "skipped",
                "reason": "another catalogue-depth batch is already running on this instance",
            }

        try:
            suppliers = self.repo.get_suppliers_needing_catalogue_depth_extraction(limit=limit, force=force)
            start_time = time.monotonic()
            attempted = 0
            extracted = 0
            unavailable = 0
            stopped_early = False

            for supplier in suppliers:
                if time.monotonic() - start_time > self.job_max_seconds:
                    stopped_early = True
                    break
                outcome = self._extract_one(supplier)
                attempted += 1
                if outcome["status"] == "extracted":
                    extracted += 1
                else:
                    unavailable += 1

            return {
                "attempted": attempted, "extracted": extracted, "unavailable": unavailable,
                "total_eligible": len(suppliers),
                "status": "partial" if stopped_early else "completed",
            }
        finally:
            _BATCH_SEMAPHORE.release()
