"""
api/jobs.py

Executes a queued pipeline job in the background. Runs via FastAPI's
`BackgroundTasks`, in-process on the same worker that received the
request -- see `SupplierRepository`'s own note on `create_pipeline_job`
for why that's a deliberate, disclosed choice for a single-instance
deployment rather than a real task queue (Celery/Redis). If this API
ever runs across multiple Railway replicas, this is the first thing
that needs to change: `BackgroundTasks` only runs on the instance that
received the HTTP request, so a second replica polling
`/pipeline/jobs/{id}` would see the job but never execute it.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Dict

from collection.collection_service import CollectionService
from discovery.discovery_service import DiscoveryService
from pipeline.orchestrator import SupplierIntelligencePipeline, build_limit_scraper_kwargs
from storage.repository import SupplierRepository
from verification_ai.verification_service import VerificationService

logger = logging.getLogger(__name__)


def run_pipeline_job(job_id: str, query: str, options: Dict[str, Any]) -> None:
    """Runs synchronously on whatever thread FastAPI's BackgroundTasks
    schedules it on, after the HTTP response for the job-creation
    request has already been sent. Never raises -- any failure is
    caught and written into the job record itself
    (`mark_pipeline_job_failed`), since there is no HTTP request left
    to return an error to by the time this runs.
    """
    repo = SupplierRepository()
    repo.mark_pipeline_job_running(job_id)
    try:
        pipeline = SupplierIntelligencePipeline(repo=repo)
        sources = options.get("sources")
        limit = options.get("limit")
        # "limit" (the request-facing name) -> "results_limit"
        # (pipeline.run's actual parameter name) -- everything else
        # passes straight through unchanged. build_limit_scraper_kwargs
        # is the same helper main.py's own --limit uses, so a job
        # triggered here gets the identical per-source
        # max_results/max_pages treatment a CLI run would, not just the
        # after-the-fact results_limit truncation.
        run_kwargs = {k: v for k, v in options.items() if k not in ("sources", "limit")}
        scraper_kwargs = build_limit_scraper_kwargs(limit, sources, list(pipeline.scrapers.keys()))
        stats = pipeline.run(
            query, sources=sources, scraper_kwargs=scraper_kwargs, results_limit=limit, **run_kwargs,
        )
        repo.mark_pipeline_job_completed(job_id, stats=stats)
    except Exception as e:
        logger.error("Pipeline job %s failed: %s", job_id, e)
        repo.mark_pipeline_job_failed(job_id, error=str(e))


_ENRICHMENT_STAGES = ("find_websites", "extract_capabilities", "verify_facilities")


def run_enrichment_job(job_id: str, stage: str, options: Dict[str, Any]) -> None:
    """Runs one of the standalone, query-independent enrichment passes
    -- the HTTP equivalent of main.py's `find-websites`/
    `extract-capabilities`/`verify-facilities` commands. Same
    never-raises-to-the-caller contract as run_pipeline_job, and reuses
    the exact same pipeline_jobs table/lifecycle so it's trackable via
    the existing GET /pipeline/jobs/{id}.
    """
    repo = SupplierRepository()
    repo.mark_pipeline_job_running(job_id)
    try:
        if stage not in _ENRICHMENT_STAGES:
            raise ValueError(f"Unknown enrichment stage: {stage!r} (expected one of {_ENRICHMENT_STAGES})")

        pipeline = SupplierIntelligencePipeline(repo=repo)
        force = options.get("force", False)
        limit = options.get("limit")

        if stage == "find_websites":
            stats = pipeline.run_website_discovery_only(force=force, limit=limit if limit is not None else 1000)
        elif stage == "extract_capabilities":
            stats = pipeline.run_capability_extraction_only(
                force=force, limit=limit if limit is not None else 1000,
                assess_photos=options.get("assess_photos", False),
            )
        else:  # verify_facilities
            stats = pipeline.run_facility_verification_only(force=force, limit=limit if limit is not None else 1000)

        repo.mark_pipeline_job_completed(job_id, stats=stats)
    except Exception as e:
        logger.error("Enrichment job %s (%s) failed: %s", job_id, stage, e)
        repo.mark_pipeline_job_failed(job_id, error=str(e))


def run_collection_job(job_id: str, options: Dict[str, Any]) -> None:
    """The HTTP equivalent of `main.py collect` -- either one named
    supplier (options['supplier_id']) or a batch of every supplier
    needing it (options['pending']). Reuses the exact same
    pipeline_jobs table/lifecycle as run_pipeline_job/run_enrichment_job
    (same never-raises-to-the-caller contract), constructed fresh here
    rather than reusing a module-level CollectionService instance so
    each job gets its own SupplierRepository connection, matching every
    other job function in this file.
    """
    repo = SupplierRepository()
    repo.mark_pipeline_job_running(job_id)
    try:
        service = CollectionService(repo=repo)
        supplier_id = options.get("supplier_id")
        if supplier_id:
            stats = service.collect(supplier_id)
        else:
            stats = service.collect_pending(
                limit=options.get("limit", 20), force=options.get("force", False),
            )
        repo.mark_pipeline_job_completed(job_id, stats=stats)
    except Exception as e:
        logger.error("Collection job %s failed: %s", job_id, e)
        repo.mark_pipeline_job_failed(job_id, error=str(e))


def run_verification_job(job_id: str, options: Dict[str, Any]) -> None:
    """The HTTP equivalent of `main.py verify-ai` -- either one named
    supplier or a batch of every supplier needing it. Same pattern as
    run_collection_job."""
    repo = SupplierRepository()
    repo.mark_pipeline_job_running(job_id)
    try:
        service = VerificationService(repo=repo)
        supplier_id = options.get("supplier_id")
        if supplier_id:
            stats = service.verify(supplier_id)
        else:
            stats = service.verify_pending(
                limit=options.get("limit", 20), force=options.get("force", False),
            )
        repo.mark_pipeline_job_completed(job_id, stats=stats)
    except Exception as e:
        logger.error("Verification job %s failed: %s", job_id, e)
        repo.mark_pipeline_job_failed(job_id, error=str(e))


def run_reverify_job(job_id: str, supplier_id: int) -> None:
    """The HTTP equivalent of `main.py reverify --supplier-id` -- always
    single-supplier (re-collect then re-verify); the batch
    `--older-than-days` mode is CLI-only for now, since it's meant for
    an operator-run sweep, not a single HTTP-triggered job."""
    repo = SupplierRepository()
    repo.mark_pipeline_job_running(job_id)
    try:
        service = VerificationService(repo=repo)
        stats = service.reverify(supplier_id)
        repo.mark_pipeline_job_completed(job_id, stats=stats)
    except Exception as e:
        logger.error("Reverify job %s (supplier #%s) failed: %s", job_id, supplier_id, e)
        repo.mark_pipeline_job_failed(job_id, error=str(e))


def run_discovery_job(job_id: str, options: Dict[str, Any]) -> None:
    """The HTTP equivalent of `main.py discover`. DiscoveryOutcome is a
    dataclass, not a plain dict -- converted via dataclasses.asdict()
    before mark_pipeline_job_completed's own json.dumps(stats) call,
    which can't serialise a dataclass instance directly."""
    repo = SupplierRepository()
    repo.mark_pipeline_job_running(job_id)
    try:
        service = DiscoveryService(repo=repo)
        outcome = service.discover(
            options["product"], category=options.get("category"), country=options.get("country"),
            max_candidates=options.get("max_candidates", 20),
        )
        repo.mark_pipeline_job_completed(job_id, stats=dataclasses.asdict(outcome))
    except Exception as e:
        logger.error("Discovery job %s failed: %s", job_id, e)
        repo.mark_pipeline_job_failed(job_id, error=str(e))
