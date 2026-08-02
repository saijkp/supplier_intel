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

import logging
from typing import Any, Dict

from pipeline.orchestrator import SupplierIntelligencePipeline, build_limit_scraper_kwargs
from storage.repository import SupplierRepository

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
