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
from typing import Any, Dict, Optional

from batch.batch_service import BatchService
from batch.csv_parser import ParsedRow, parse_csv
from collection.collection_service import CollectionService
from deduplication.domain_utils import looks_like_url
from discovery.discovery_service import DiscoveryService
from pipeline.orchestrator import SupplierIntelligencePipeline, build_limit_scraper_kwargs
from storage.repository import SupplierRepository
from verification.contact_finder_service import ContactFinderService
from verification.factory_facts_service import FactoryFactsService
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


def run_batch_job(
    job_id: str, csv_bytes: bytes,
    recover_dead_domains: bool = False, recovery_product_term: Optional[str] = None,
    default_region: Optional[str] = None,
) -> None:
    """The HTTP equivalent of `main.py batch-upload` -- parses the
    uploaded CSV (batch.csv_parser.parse_csv) and runs every row through
    BatchService.run_batch(), which is just SupplierMatcher.
    resolve_and_store() + CollectionService.collect() per row (no
    second extraction pipeline -- see batch/batch_service.py's own
    docstring). Progress is written incrementally via
    update_pipeline_job_progress as rows complete, since a 50-200 row
    batch (each row potentially launching a real headless browser) can
    run for a long time -- same "poll before completion" mechanism
    sourcing/sourcing_agent.py's own per-candidate progress already
    established. `csv_bytes` is read from the upload BEFORE this is
    scheduled as a background task (the request's UploadFile isn't
    guaranteed to stay valid past the request/response cycle), so this
    function itself never touches the HTTP layer at all.

    `default_region`: same ISO 3166-1 alpha-2 fallback as
    `main.py batch-upload --default-region` -- threaded straight to
    BatchService's own `default_region_fallback` (which threads it to
    CollectionService, which uses it ONLY when a supplier's own country
    isn't resolved yet, never overriding a real known one). Until this
    was wired, POST /batch/upload had no way to supply this at all --
    a real 71-row batch went from 18/71 to 59/71 rows with a phone
    number once a region hint was available, dwarfing every other
    phone-extraction fix combined.
    """
    repo = SupplierRepository()
    repo.mark_pipeline_job_running(job_id)
    try:
        parsed = parse_csv(csv_bytes)
        service = BatchService(repo=repo, default_region_fallback=default_region)

        def _on_progress(outcome) -> None:
            repo.update_pipeline_job_progress(job_id, dataclasses.asdict(outcome))

        outcome = service.run_batch(
            parsed.rows, batch_job_id=job_id, progress_callback=_on_progress,
            recover_dead_domains=recover_dead_domains, recovery_product_term=recovery_product_term,
        )

        stats = dataclasses.asdict(outcome)
        stats["company_name_column"] = parsed.company_name_column
        stats["website_column"] = parsed.website_column
        stats["duplicate_row_count"] = len(parsed.duplicate_row_indices)
        repo.mark_pipeline_job_completed(job_id, stats=stats)
    except Exception as e:
        logger.error("Batch job %s failed: %s", job_id, e)
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


def run_contacts_job(job_id: str, options: Dict[str, Any]) -> None:
    """Runs verification.contact_finder_service.ContactFinderService --
    either one named supplier or a batch of every supplier needing it.
    Same pattern as run_collection_job."""
    repo = SupplierRepository()
    repo.mark_pipeline_job_running(job_id)
    try:
        service = ContactFinderService(repo=repo)
        supplier_id = options.get("supplier_id")
        if supplier_id:
            stats = service.find_contacts(supplier_id)
        else:
            stats = service.find_contacts_pending(
                limit=options.get("limit", 20), force=options.get("force", False),
            )
        repo.mark_pipeline_job_completed(job_id, stats=stats)
    except Exception as e:
        logger.error("Contacts job %s failed: %s", job_id, e)
        repo.mark_pipeline_job_failed(job_id, error=str(e))


def run_factory_facts_job(job_id: str, options: Dict[str, Any]) -> None:
    """Runs verification.factory_facts_service.FactoryFactsService --
    either one named supplier or a batch of every supplier needing it.
    Same pattern as run_contacts_job."""
    repo = SupplierRepository()
    repo.mark_pipeline_job_running(job_id)
    try:
        service = FactoryFactsService(repo=repo)
        supplier_id = options.get("supplier_id")
        if supplier_id:
            stats = service.find_facts(supplier_id)
        else:
            stats = service.find_facts_pending(
                limit=options.get("limit", 20), force=options.get("force", False),
            )
        repo.mark_pipeline_job_completed(job_id, stats=stats)
    except Exception as e:
        logger.error("Factory facts job %s failed: %s", job_id, e)
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
    """The HTTP equivalent of `main.py discover`. DiscoveryOutcome (or
    DiscoveryToTargetOutcome, when `target_count` is given) is a
    dataclass, not a plain dict -- converted via dataclasses.asdict()
    before mark_pipeline_job_completed's own json.dumps(stats) call,
    which can't serialise a dataclass instance directly.

    A progress_callback is now ALWAYS wired (previously this job type
    had none at all) -- see discovery.discovery_service.
    DiscoveryProgressEvent's own docstring: the first per-candidate-
    detail live progress this codebase has ever surfaced, not just
    aggregate counts like every other job type. `events` accumulates
    for this job's whole lifetime and is capped to the most recent 200
    in the stored progress blob to bound its size -- older events
    aren't lost, they're still in raw_source_data/discovery_runs
    regardless, just not shown in the live feed past that cap.

    Cumulative examined/validated counters are tracked explicitly here
    (bumped by the PREVIOUS round's final totals exactly when
    event.round changes) because each DiscoveryProgressEvent's own
    round_examined/round_validated are scoped to whichever single
    discover() call produced it, not cumulative across
    discover_to_target()'s multiple rounds."""
    repo = SupplierRepository()
    repo.mark_pipeline_job_running(job_id)
    try:
        service = DiscoveryService(repo=repo)
        target_count = options.get("target_count")

        events: list = []
        progress_state = {"round": 1, "completed_examined": 0, "completed_validated": 0}

        def on_progress(event) -> None:
            if event.round != progress_state["round"]:
                progress_state["completed_examined"] += progress_state.get("_last_round_examined", 0)
                progress_state["completed_validated"] += progress_state.get("_last_round_validated", 0)
                progress_state["round"] = event.round
            progress_state["_last_round_examined"] = event.round_examined
            progress_state["_last_round_validated"] = event.round_validated
            events.append(dataclasses.asdict(event))
            repo.update_pipeline_job_progress(job_id, {
                "events": events[-200:],
                "examined": progress_state["completed_examined"] + event.round_examined,
                "candidates_validated": progress_state["completed_validated"] + event.round_validated,
                "round": event.round,
                "target": target_count,
            })

        if target_count:
            outcome = service.discover_to_target(
                options["product"], target_count=target_count,
                category=options.get("category"), country=options.get("country"),
                max_multiplier=options.get("max_multiplier", 5), progress_callback=on_progress,
                recover_dead_domains=options.get("recover_dead_domains", False),
            )
        else:
            outcome = service.discover(
                options["product"], category=options.get("category"), country=options.get("country"),
                max_candidates=options.get("max_candidates", 20), source=options.get("source", "serpapi"),
                progress_callback=on_progress,
                recover_dead_domains=options.get("recover_dead_domains", False),
            )
        repo.mark_pipeline_job_completed(job_id, stats=dataclasses.asdict(outcome))
    except Exception as e:
        logger.error("Discovery job %s failed: %s", job_id, e)
        repo.mark_pipeline_job_failed(job_id, error=str(e))


def run_single_company_job(job_id: str, options: Dict[str, Any]) -> None:
    """The HTTP equivalent of pasting exactly one row into the existing
    batch-upload CSV flow -- reuses batch.batch_service.BatchService.
    run_batch() completely unmodified, just with a single
    batch.csv_parser.ParsedRow built from `options["input_text"]`
    instead of a parsed CSV. When input_text isn't already URL-shaped
    (deduplication.domain_utils.looks_like_url), resolves a domain
    first via scrapers.company_website_finder.CompanyWebsiteFinder --
    the same class main.py find-websites/pipeline.
    run_website_discovery_only already use -- since batch_service.py
    deliberately refuses to guess or search for a website from a bare
    name on its own (see that module's own docstring: 'company_name,
    no website: needs_url -- never guessed or searched')."""
    repo = SupplierRepository()
    repo.mark_pipeline_job_running(job_id)
    try:
        input_text = (options.get("input_text") or "").strip()
        website_finder_reason = None

        if looks_like_url(input_text):
            company_name, website = None, input_text
        else:
            from scrapers.company_website_finder import CompanyWebsiteFinder
            from scrapers.google_search_scraper import GoogleSearchScraper
            from scrapers.own_website_scraper import OwnWebsiteScraper

            finder = CompanyWebsiteFinder(GoogleSearchScraper(), OwnWebsiteScraper())
            finding = finder.find_website(input_text, country=options.get("country"))
            website_finder_reason = finding.reason
            company_name = input_text
            website = finding.domain if finding.validated else None

            if website is None:
                repo.mark_pipeline_job_completed(job_id, stats={
                    "status": "needs_url", "input_text": input_text,
                    "website_finder_reason": website_finder_reason,
                })
                return

        row = ParsedRow(
            row_index=0,
            original_columns={"Company Name": company_name or "", "Website": website or ""},
            company_name=company_name, website=website,
        )
        service = BatchService(repo=repo)

        def on_progress(outcome) -> None:
            repo.update_pipeline_job_progress(job_id, dataclasses.asdict(outcome))

        outcome = service.run_batch([row], batch_job_id=job_id, progress_callback=on_progress)

        batch_rows = repo.get_batch_upload_rows(job_id)
        resolved_supplier_id = batch_rows[0]["supplier_id"] if batch_rows else None

        stats = dataclasses.asdict(outcome)
        stats["resolved_domain"] = website
        stats["resolved_supplier_id"] = resolved_supplier_id
        stats["website_finder_reason"] = website_finder_reason
        repo.mark_pipeline_job_completed(job_id, stats=stats)
    except Exception as e:
        logger.error("Single-company job %s failed: %s", job_id, e)
        repo.mark_pipeline_job_failed(job_id, error=str(e))


def run_supplier_correction_job(job_id: str, supplier_id: int, options: Dict[str, Any]) -> None:
    """The HTTP equivalent of `python main.py correct-supplier
    <supplier_id> --clear-domain` -- exists because this codebase's
    production database is a SQLite file on a Railway volume, not
    network-reachable, so an already-deployed bad record (e.g. a
    false-match domain) can only be corrected over HTTP, through the
    running service itself, not by running the CLI command from
    elsewhere. Business logic lives in
    batch.supplier_correction.SupplierCorrectionService, shared with
    that CLI command -- see this module's own docstring for why
    BackgroundTasks (not a synchronous response) here too: a real
    SerpAPI search + fetch + LLM call + Playwright collection can
    easily take longer than a proxy is willing to hold a request
    open."""
    repo = SupplierRepository()
    repo.mark_pipeline_job_running(job_id)
    try:
        from batch.supplier_correction import SupplierCorrectionService

        service = SupplierCorrectionService(repo=repo)
        result = service.correct_domain(supplier_id, reason=options.get("reason"))
        repo.mark_pipeline_job_completed(job_id, stats=result)
    except ValueError as e:
        repo.mark_pipeline_job_failed(job_id, error=str(e))
    except Exception as e:
        logger.error("Supplier correction job %s (supplier #%s) failed: %s", job_id, supplier_id, e)
        repo.mark_pipeline_job_failed(job_id, error=str(e))


def run_sourcing_job(job_id: str, options: Dict[str, Any]) -> None:
    """The HTTP equivalent of one chat message on the frontend's Source
    tab. A sourcing run can easily take minutes (many discover->collect
    ->verify cycles per candidate), so unlike
    /discovery/backfill-product-keywords this genuinely needs the async
    job/poll pattern, not a synchronous response.

    SourcingOutcome is a dataclass (like DiscoveryOutcome) -- converted
    via dataclasses.asdict() the same way, EXCEPT its own `brief` field
    is itself a StructuredBrief dataclass; asdict() already recurses
    into nested dataclasses, so this is still the one conversion call
    the every other job runner already uses, not a new pattern.

    Progress: SourcingAgentService.run()'s on_progress callback writes
    running counts onto this same job row via
    repo.update_pipeline_job_progress -- GET /pipeline/jobs/{job_id}
    (already polled every few seconds by the frontend) picks these up
    for free via PipelineJobResponse.progress, no new polling mechanism.
    """
    from sourcing.schemas import SourcingProgress
    from sourcing.sourcing_agent import SourcingAgentService

    repo = SupplierRepository()
    repo.mark_pipeline_job_running(job_id)
    try:
        service = SourcingAgentService(repo=repo)

        def on_progress(progress: SourcingProgress) -> None:
            repo.update_pipeline_job_progress(job_id, dataclasses.asdict(progress))

        outcome = service.run(
            options["brief_text"], max_multiplier=options.get("max_multiplier", 5),
            on_progress=on_progress,
        )
        if outcome.status == "failed":
            repo.mark_pipeline_job_failed(job_id, error=outcome.error or "sourcing run failed")
        else:
            repo.mark_pipeline_job_completed(job_id, stats=dataclasses.asdict(outcome))
    except Exception as e:
        logger.error("Sourcing job %s failed: %s", job_id, e)
        repo.mark_pipeline_job_failed(job_id, error=str(e))
