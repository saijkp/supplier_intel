"""
api/app.py

The web API layer wrapping supplier_intel's existing pipeline and
query functions for a frontend (e.g. a Netlify-hosted UI) to call over
HTTP. Everything here is a thin wrapper -- no business logic lives in
this file; it all still lives in `pipeline/orchestrator.py` and
`storage/repository.py`, exactly as before this layer existed. This
file's only jobs are: HTTP routing, request validation, auth, and
converting between raw SQLite rows and the Pydantic models in
`api/models.py`.

Auth: every route except `/health` requires a bearer token (see
`api/auth.py`). CORS: configured from `ALLOWED_ORIGINS`, empty by
default -- a browser-based frontend can't call this at all until its
origin is explicitly added.

Long-running operations: `POST /pipeline/jobs` returns immediately
with a job id; the actual pipeline run happens in the background (see
`api/jobs.py`) and the frontend polls `GET /pipeline/jobs/{id}` for
status. A synchronous endpoint that blocked for the several minutes a
full run with capability extraction can take would time out most
HTTP clients and reverse proxies long before it finished.

Running locally:
    uvicorn api.app:app --reload

Running on Railway: see the repository README for the exact start
command and the persistent-volume setup this needs so SQLite survives
redeploys.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, Response

from api.auth import require_api_token
from api.jobs import (
    run_collection_job,
    run_contacts_job,
    run_discovery_job,
    run_enrichment_job,
    run_factory_facts_job,
    run_pipeline_job,
    run_reverify_job,
    run_sourcing_job,
    run_verification_job,
)
from api.models import (
    BuyerProfileRequest,
    BuyerProfileResponse,
    CollectionJobRequest,
    CommercialSearchResult,
    ContactsJobRequest,
    DiscoveryJobRequest,
    EnrichmentJobRequest,
    FactoryFactsJobRequest,
    PipelineJobRequest,
    PipelineJobResponse,
    ProcurementOutcomeRequest,
    ProcurementOutcomeResponse,
    SourcingRunRequest,
    SourcingRunResponse,
    SupplierSearchResult,
    VerificationJobRequest,
)
from config.settings import ALLOWED_ORIGINS
from storage.database import initialise_schema
from storage.repository import SupplierRepository


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialise_schema()
    yield


app = FastAPI(
    title="Supplier Intelligence API",
    description="Search, verify, and enrich trailer-component suppliers.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


def get_repo() -> SupplierRepository:
    return SupplierRepository()


def _to_search_result(row: Dict[str, Any]) -> SupplierSearchResult:
    """Explicit field-by-field construction, not
    `SupplierSearchResult.model_validate(row)` -- SQLite stores
    booleans as 0/1 integers, and a couple of fields genuinely need
    that coerced rather than trusting Pydantic's own type coercion to
    happen to do the right thing here. See this module's own docstring
    for the reasoning.
    """
    return SupplierSearchResult(
        id=row["id"],
        canonical_name=row["canonical_name"],
        country=row.get("country"),
        domain=row.get("domain"),
        composite_score=row.get("composite_score"),
        evidence_coverage=row.get("evidence_coverage"),
        recommendation=row.get("recommendation"),
        is_manufacturer=(
            bool(row["is_manufacturer"]) if row.get("is_manufacturer") is not None else None
        ),
        primary_email=row.get("primary_email"),
        primary_phone=row.get("primary_phone"),
        contact_form_url=row.get("contact_form_url"),
        linkedin_url=row.get("linkedin_url"),
        factory_photo_verdict=row.get("factory_photo_verdict"),
        facility_address_verified=(
            bool(row["facility_address_verified"])
            if row.get("facility_address_verified") is not None else None
        ),
        year_established=row.get("year_established"),
        alibaba_years=row.get("alibaba_years"),
        matched_capabilities=row.get("matched_capabilities") or [],
        ai_confidence_score=row.get("ai_confidence_score"),
        ai_summary=row.get("ai_summary"),
        ai_strengths=row.get("ai_strengths") or [],
        ai_risks=row.get("ai_risks") or [],
        ai_suitable_customer_types=row.get("ai_suitable_customer_types") or [],
        collection_status=row.get("collection_status"),
        sourcing_oem_odm_notes=row.get("sourcing_oem_odm_notes"),
        sourcing_factory_notes=row.get("sourcing_factory_notes"),
        sourcing_engineering_notes=row.get("sourcing_engineering_notes"),
        sourcing_export_notes=row.get("sourcing_export_notes"),
        sourcing_volume_suitability=row.get("sourcing_volume_suitability"),
        sourcing_payment_terms_notes=row.get("sourcing_payment_terms_notes"),
        sourcing_verification_status=row.get("sourcing_verification_status"),
        key_contacts=row.get("key_contacts") or [],
        ai_confidence_breakdown=row.get("ai_confidence_breakdown") or [],
        procurement_recommendation=row.get("procurement_recommendation"),
        procurement_recommendation_reason=row.get("procurement_recommendation_reason"),
        certificate_document_urls=row.get("certificate_document_urls") or [],
        production_lines_notes=row.get("production_lines_notes"),
        machinery_notes=row.get("machinery_notes"),
        factory_ownership=row.get("factory_ownership"),
        confirmed_shipments_uk=row.get("confirmed_shipments_uk") or 0,
        confirmed_shipments_eu=row.get("confirmed_shipments_eu") or 0,
        confirmed_shipments_us=row.get("confirmed_shipments_us") or 0,
        exports_to_uk=bool(row.get("exports_to_uk")),
        exports_to_eu=bool(row.get("exports_to_eu")),
        exports_to_us=bool(row.get("exports_to_us")),
        active_export_countries=row.get("active_export_countries") or [],
        employee_count=row.get("employee_count"),
        factory_size_sqm=row.get("factory_size_sqm"),
    )


def _to_job_response(job: Dict[str, Any]) -> PipelineJobResponse:
    return PipelineJobResponse(
        id=job["id"],
        status=job["status"],
        query=job["query"],
        stats=job.get("stats"),
        progress=job.get("progress"),
        error=job.get("error"),
        created_at=_stringify(job.get("created_at")),
        started_at=_stringify(job.get("started_at")),
        completed_at=_stringify(job.get("completed_at")),
    )


def _stringify(value: Any) -> Optional[str]:
    return str(value) if value is not None else None


@app.get("/health")
def health() -> Dict[str, str]:
    """No auth -- this is what Railway (or any uptime monitor) hits to
    confirm the service is alive. Deliberately reveals nothing about
    configuration or data."""
    return {"status": "ok"}


@app.get(
    "/suppliers/search",
    response_model=List[SupplierSearchResult],
    dependencies=[Depends(require_api_token)],
)
def search_suppliers(
    product: Optional[str] = None,
    require: List[str] = Query(default=[]),
    manufacturers_only: bool = False,
    country: Optional[str] = None,
    min_score: Optional[int] = None,
    limit: int = 25,
    include_capabilities: bool = False,
    repo: SupplierRepository = Depends(get_repo),
) -> List[SupplierSearchResult]:
    """Thin HTTP wrapper over `search_suppliers_full` -- see that
    method's own docstring for the actual matching semantics
    (`require` is AND, not OR; `country` is exact match, not fuzzy).

    `search_suppliers_full` only populates `matched_capabilities` when
    `require` is set (it's a byproduct of the capability-filter join,
    not a general-purpose enrichment) -- a plain browse query gets
    `matched_capabilities: []` for every result otherwise. Passing
    `include_capabilities=true` backfills it per result (same
    `repo.get_capabilities(id)` call `GET /suppliers/{id}` already
    makes), for callers that need OEM/engineering/certification
    evidence without narrowing results to a specific required term --
    the Compare/rank UI's reason for existing. Defaults to False so
    the default Search behaviour/cost profile is unchanged.
    """
    try:
        results = repo.search_suppliers_full(
            product_query=product,
            required_capabilities=require,
            manufacturers_only=manufacturers_only,
            country=country,
            min_score=min_score,
            limit=limit,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if include_capabilities and not require:
        for row in results:
            row["matched_capabilities"] = repo.get_capabilities(row["id"])
    return [_to_search_result(r) for r in results]


@app.get(
    "/suppliers/{supplier_id}",
    response_model=SupplierSearchResult,
    dependencies=[Depends(require_api_token)],
)
def get_supplier(
    supplier_id: int, repo: SupplierRepository = Depends(get_repo)
) -> SupplierSearchResult:
    supplier = repo.get_supplier(supplier_id)
    if supplier is None:
        raise HTTPException(status_code=404, detail="Supplier not found")
    supplier["matched_capabilities"] = repo.get_capabilities(supplier_id)
    return _to_search_result(supplier)


@app.post(
    "/pipeline/jobs",
    response_model=PipelineJobResponse,
    status_code=202,
    dependencies=[Depends(require_api_token)],
)
def create_pipeline_job(
    request: PipelineJobRequest,
    background_tasks: BackgroundTasks,
    repo: SupplierRepository = Depends(get_repo),
) -> PipelineJobResponse:
    """Returns 202 Accepted immediately with a job id -- the actual
    run happens in the background. Poll `GET /pipeline/jobs/{id}` for
    status. See this module's own docstring for why this can't be a
    synchronous endpoint.
    """
    job_id = str(uuid.uuid4())
    options = request.model_dump(exclude={"query"})
    repo.create_pipeline_job(job_id=job_id, query=request.query, options=options)
    background_tasks.add_task(run_pipeline_job, job_id, request.query, options)
    job = repo.get_pipeline_job(job_id)
    return _to_job_response(job)


@app.post(
    "/pipeline/enrichment-jobs",
    response_model=PipelineJobResponse,
    status_code=202,
    dependencies=[Depends(require_api_token)],
)
def create_enrichment_job(
    request: EnrichmentJobRequest,
    background_tasks: BackgroundTasks,
    repo: SupplierRepository = Depends(get_repo),
) -> PipelineJobResponse:
    """Same async job/poll pattern as POST /pipeline/jobs, but for the
    standalone enrichment passes (find-websites/extract-capabilities/
    verify-facilities) that run across every existing supplier needing
    them rather than against a fresh search query."""
    job_id = str(uuid.uuid4())
    options = request.model_dump(exclude={"stage"})
    repo.create_pipeline_job(job_id=job_id, query=f"[enrichment] {request.stage}", options=options)
    background_tasks.add_task(run_enrichment_job, job_id, request.stage, options)
    job = repo.get_pipeline_job(job_id)
    return _to_job_response(job)


@app.post(
    "/collection/jobs",
    response_model=PipelineJobResponse,
    status_code=202,
    dependencies=[Depends(require_api_token)],
)
def create_collection_job(
    request: CollectionJobRequest,
    background_tasks: BackgroundTasks,
    repo: SupplierRepository = Depends(get_repo),
) -> PipelineJobResponse:
    """Same async job/poll pattern as POST /pipeline/jobs, but for
    collection.collection_service.CollectionService (visits supplier
    websites with a real headless browser, saves HTML/screenshots/
    extracted contact+product data) -- the HTTP equivalent of
    `main.py collect`."""
    if not request.supplier_id and not request.pending:
        raise HTTPException(status_code=422, detail="Specify either supplier_id or pending.")
    job_id = str(uuid.uuid4())
    options = request.model_dump()
    label = f"[collection] supplier #{request.supplier_id}" if request.supplier_id else "[collection] pending batch"
    repo.create_pipeline_job(job_id=job_id, query=label, options=options)
    background_tasks.add_task(run_collection_job, job_id, options)
    job = repo.get_pipeline_job(job_id)
    return _to_job_response(job)


@app.post(
    "/contacts/jobs",
    response_model=PipelineJobResponse,
    status_code=202,
    dependencies=[Depends(require_api_token)],
)
def create_contacts_job(
    request: ContactsJobRequest,
    background_tasks: BackgroundTasks,
    repo: SupplierRepository = Depends(get_repo),
) -> PipelineJobResponse:
    """Same async job/poll pattern as POST /collection/jobs, but for
    verification.contact_finder_service.ContactFinderService (named
    Procurement/Sales/CEO contacts via Apollo.io). A separate, opt-in
    enrichment stage -- never triggered automatically inside a
    Sourcing Agent run."""
    if not request.supplier_id and not request.pending:
        raise HTTPException(status_code=422, detail="Specify either supplier_id or pending.")
    job_id = str(uuid.uuid4())
    options = request.model_dump()
    label = f"[contacts] supplier #{request.supplier_id}" if request.supplier_id else "[contacts] pending batch"
    repo.create_pipeline_job(job_id=job_id, query=label, options=options)
    background_tasks.add_task(run_contacts_job, job_id, options)
    job = repo.get_pipeline_job(job_id)
    return _to_job_response(job)


@app.post(
    "/factory-facts/jobs",
    response_model=PipelineJobResponse,
    status_code=202,
    dependencies=[Depends(require_api_token)],
)
def create_factory_facts_job(
    request: FactoryFactsJobRequest,
    background_tasks: BackgroundTasks,
    repo: SupplierRepository = Depends(get_repo),
) -> PipelineJobResponse:
    """Same async job/poll pattern as POST /contacts/jobs, but for
    verification.factory_facts_service.FactoryFactsService (production
    lines/machinery/factory-ownership facts extracted from the
    supplier's own website). A separate, opt-in enrichment stage --
    never triggered automatically inside a Sourcing Agent run."""
    if not request.supplier_id and not request.pending:
        raise HTTPException(status_code=422, detail="Specify either supplier_id or pending.")
    job_id = str(uuid.uuid4())
    options = request.model_dump()
    label = (
        f"[factory-facts] supplier #{request.supplier_id}"
        if request.supplier_id else "[factory-facts] pending batch"
    )
    repo.create_pipeline_job(job_id=job_id, query=label, options=options)
    background_tasks.add_task(run_factory_facts_job, job_id, options)
    job = repo.get_pipeline_job(job_id)
    return _to_job_response(job)


@app.post(
    "/verification/jobs",
    response_model=PipelineJobResponse,
    status_code=202,
    dependencies=[Depends(require_api_token)],
)
def create_verification_job(
    request: VerificationJobRequest,
    background_tasks: BackgroundTasks,
    repo: SupplierRepository = Depends(get_repo),
) -> PipelineJobResponse:
    """Same async job/poll pattern as POST /collection/jobs, but for
    verification_ai.verification_service.VerificationService (AI
    cross-check, 0-100 ai_confidence_score, summary/strengths/risks) --
    the HTTP equivalent of `main.py verify-ai`."""
    if not request.supplier_id and not request.pending:
        raise HTTPException(status_code=422, detail="Specify either supplier_id or pending.")
    job_id = str(uuid.uuid4())
    options = request.model_dump()
    label = f"[verification] supplier #{request.supplier_id}" if request.supplier_id else "[verification] pending batch"
    repo.create_pipeline_job(job_id=job_id, query=label, options=options)
    background_tasks.add_task(run_verification_job, job_id, options)
    job = repo.get_pipeline_job(job_id)
    return _to_job_response(job)


@app.post(
    "/discovery/jobs",
    response_model=PipelineJobResponse,
    status_code=202,
    dependencies=[Depends(require_api_token)],
)
def create_discovery_job(
    request: DiscoveryJobRequest,
    background_tasks: BackgroundTasks,
    repo: SupplierRepository = Depends(get_repo),
) -> PipelineJobResponse:
    """Same async job/poll pattern as POST /collection/jobs, but for
    discovery.discovery_service.DiscoveryService -- AI-assisted supplier
    discovery, grounded in either real SerpAPI search results or an
    LLM's own knowledge per `request.source` (see
    discovery/discovery_service.py's module docstring for how both
    paths converge on the same real-fetch/content-match validation
    gate). The HTTP equivalent of `main.py discover`."""
    job_id = str(uuid.uuid4())
    options = request.model_dump()
    repo.create_pipeline_job(job_id=job_id, query=f"[discovery] {request.product}", options=options)
    background_tasks.add_task(run_discovery_job, job_id, options)
    job = repo.get_pipeline_job(job_id)
    return _to_job_response(job)


@app.post(
    "/discovery/backfill-product-keywords",
    dependencies=[Depends(require_api_token)],
)
def backfill_discovery_product_keywords(
    repo: SupplierRepository = Depends(get_repo),
) -> Dict[str, Any]:
    """One-off repair, safe to call more than once: fills product_keywords
    on suppliers discovery.DiscoveryService created before it started
    recording that field, using pipeline_jobs history to reconstruct the
    product term each was actually discovered for -- never guessed (see
    DiscoveryService.backfill_product_keywords's own docstring). Pure
    DB read/write with no external API calls, so unlike every other job
    endpoint here this runs synchronously instead of through the
    pipeline_jobs queue."""
    from discovery.discovery_service import DiscoveryService

    service = DiscoveryService(repo=repo)
    result = service.backfill_product_keywords()
    return {
        "updated_count": len(result["updated_supplier_ids"]),
        "updated_supplier_ids": result["updated_supplier_ids"],
        "already_had_keywords_count": len(result["already_had_keywords_supplier_ids"]),
        "missing_supplier_count": len(result["missing_supplier_ids"]),
    }


@app.post(
    "/sourcing/runs",
    response_model=PipelineJobResponse,
    status_code=202,
    dependencies=[Depends(require_api_token)],
)
def create_sourcing_run(
    request: SourcingRunRequest,
    background_tasks: BackgroundTasks,
    repo: SupplierRepository = Depends(get_repo),
) -> PipelineJobResponse:
    """One free-text sourcing brief drives the entire discover -> collect
    -> verify -> qualify loop (see sourcing/sourcing_agent.py). Same
    async job/poll pattern as every other job endpoint -- a run can
    easily take minutes. Poll GET /pipeline/jobs/{id} for live
    incremental progress (examined/qualified/target, via `progress`),
    then GET /sourcing/runs/{run_id} once `stats.run_id` appears in the
    completed job to fetch the qualified suppliers in full."""
    job_id = str(uuid.uuid4())
    options = request.model_dump()
    label = f"[sourcing] {request.brief_text[:60]}"
    repo.create_pipeline_job(job_id=job_id, query=label, options=options)
    background_tasks.add_task(run_sourcing_job, job_id, options)
    job = repo.get_pipeline_job(job_id)
    return _to_job_response(job)


@app.get(
    "/sourcing/runs/{run_id}",
    response_model=SourcingRunResponse,
    dependencies=[Depends(require_api_token)],
)
def get_sourcing_run(
    run_id: int, repo: SupplierRepository = Depends(get_repo),
) -> SourcingRunResponse:
    run = repo.get_sourcing_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Sourcing run not found")
    qualified_ids = run.get("qualified_supplier_ids_json") or []
    qualified_suppliers = [_to_search_result(s) for s in repo.list_suppliers(ids=qualified_ids, limit=len(qualified_ids) or 1)]
    return SourcingRunResponse(
        id=run["id"],
        brief_text=run["brief_text"],
        structured_brief=run.get("structured_brief_json"),
        target_count=run["target_count"],
        examined_count=run["examined_count"],
        status=run["status"],
        error_message=run.get("error_message"),
        created_at=_stringify(run.get("created_at")),
        completed_at=_stringify(run.get("completed_at")),
        qualified_suppliers=qualified_suppliers,
    )


@app.get("/sourcing/runs/{run_id}/export.csv", dependencies=[Depends(require_api_token)])
def export_sourcing_run_csv(
    run_id: int, repo: SupplierRepository = Depends(get_repo),
) -> PlainTextResponse:
    """Scoped to exactly the suppliers ONE sourcing run qualified, not
    the whole database -- see storage.repository.SupplierRepository.
    list_suppliers's `ids` filter and reports.generator.
    suppliers_to_sourcing_csv_string's own column set (matching the
    brief's requested output shape), both added specifically for this."""
    from reports.generator import suppliers_to_sourcing_csv_string

    run = repo.get_sourcing_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Sourcing run not found")
    qualified_ids = run.get("qualified_supplier_ids_json") or []
    suppliers = repo.list_suppliers(ids=qualified_ids, limit=len(qualified_ids) or 1)
    csv_text = suppliers_to_sourcing_csv_string(suppliers)
    return PlainTextResponse(
        csv_text, media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=sourcing_run_{run_id}.csv"},
    )


@app.post(
    "/suppliers/{supplier_id}/reverify",
    response_model=PipelineJobResponse,
    status_code=202,
    dependencies=[Depends(require_api_token)],
)
def create_reverify_job(
    supplier_id: int,
    background_tasks: BackgroundTasks,
    repo: SupplierRepository = Depends(get_repo),
) -> PipelineJobResponse:
    """Re-collect then re-verify one already-known supplier -- the HTTP
    equivalent of `main.py reverify --supplier-id`. Genuine changes are
    appended to the supplier's change log (see GET
    /suppliers/{id}/change-log), never silently overwritten."""
    if repo.get_supplier(supplier_id) is None:
        raise HTTPException(status_code=404, detail="Supplier not found")
    job_id = str(uuid.uuid4())
    label = f"[reverify] supplier #{supplier_id}"
    repo.create_pipeline_job(job_id=job_id, query=label, options={"supplier_id": supplier_id})
    background_tasks.add_task(run_reverify_job, job_id, supplier_id)
    job = repo.get_pipeline_job(job_id)
    return _to_job_response(job)


@app.get(
    "/suppliers/{supplier_id}/verification-history",
    dependencies=[Depends(require_api_token)],
)
def get_verification_history(
    supplier_id: int, repo: SupplierRepository = Depends(get_repo),
) -> List[Dict[str, Any]]:
    if repo.get_supplier(supplier_id) is None:
        raise HTTPException(status_code=404, detail="Supplier not found")
    return repo.get_verification_history(supplier_id)


@app.get(
    "/suppliers/{supplier_id}/change-log",
    dependencies=[Depends(require_api_token)],
)
def get_supplier_change_log(
    supplier_id: int, repo: SupplierRepository = Depends(get_repo),
) -> List[Dict[str, Any]]:
    if repo.get_supplier(supplier_id) is None:
        raise HTTPException(status_code=404, detail="Supplier not found")
    return repo.get_supplier_change_log(supplier_id)


@app.get(
    "/pipeline/jobs/{job_id}",
    response_model=PipelineJobResponse,
    dependencies=[Depends(require_api_token)],
)
def get_pipeline_job(job_id: str, repo: SupplierRepository = Depends(get_repo)) -> PipelineJobResponse:
    job = repo.get_pipeline_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _to_job_response(job)


@app.get("/pipeline/jobs", response_model=List[PipelineJobResponse], dependencies=[Depends(require_api_token)])
def list_pipeline_jobs(
    limit: int = 50, repo: SupplierRepository = Depends(get_repo)
) -> List[PipelineJobResponse]:
    return [_to_job_response(j) for j in repo.list_pipeline_jobs(limit=limit)]


@app.get("/export/csv", dependencies=[Depends(require_api_token)])
def export_csv(
    recommendation: Optional[str] = None,
    min_score: Optional[int] = None,
    limit: int = 1000,
    repo: SupplierRepository = Depends(get_repo),
) -> PlainTextResponse:
    from reports.generator import suppliers_to_csv_string

    suppliers = repo.list_suppliers(
        recommendation=recommendation, min_composite_score=min_score, limit=limit
    )
    csv_text = suppliers_to_csv_string(suppliers)
    return PlainTextResponse(
        csv_text, media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=suppliers.csv"},
    )


@app.get("/export/excel", dependencies=[Depends(require_api_token)])
def export_excel(
    recommendation: Optional[str] = None,
    min_score: Optional[int] = None,
    limit: int = 1000,
    repo: SupplierRepository = Depends(get_repo),
) -> Response:
    from reports.generator import suppliers_to_excel_bytes

    suppliers = repo.list_suppliers(
        recommendation=recommendation, min_composite_score=min_score, limit=limit
    )
    excel_bytes = suppliers_to_excel_bytes(suppliers)
    return Response(
        excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=suppliers.xlsx"},
    )


# ═════════════════════════════════════════════════════
# Buyer profiles + commercial compatibility search
# ═════════════════════════════════════════════════════


def _to_buyer_profile_response(profile: Dict[str, Any]) -> BuyerProfileResponse:
    return BuyerProfileResponse(
        id=profile["id"], name=profile["name"], destination_country=profile.get("destination_country"),
        required_capabilities=profile.get("required_capabilities") or [],
        preferred_incoterm=profile.get("preferred_incoterm"),
        preferred_payment_terms_days=profile.get("preferred_payment_terms_days"),
        min_company_size=profile.get("min_company_size"), target_market=profile.get("target_market"),
        min_export_experience_years=profile.get("min_export_experience_years"),
        manufacturers_only=bool(profile.get("manufacturers_only")),
        created_at=_stringify(profile.get("created_at")),
    )


@app.post(
    "/buyer-profiles", response_model=BuyerProfileResponse, status_code=201,
    dependencies=[Depends(require_api_token)],
)
def create_buyer_profile(
    request: BuyerProfileRequest, repo: SupplierRepository = Depends(get_repo)
) -> BuyerProfileResponse:
    fields = request.model_dump(exclude={"name"})
    try:
        profile_id = repo.create_buyer_profile(name=request.name, **fields)
    except Exception as e:
        raise HTTPException(status_code=409, detail=f"could not create profile: {e}") from e
    return _to_buyer_profile_response(repo.get_buyer_profile(profile_id))


@app.get(
    "/buyer-profiles", response_model=List[BuyerProfileResponse],
    dependencies=[Depends(require_api_token)],
)
def list_buyer_profiles(repo: SupplierRepository = Depends(get_repo)) -> List[BuyerProfileResponse]:
    return [_to_buyer_profile_response(p) for p in repo.list_buyer_profiles()]


@app.get(
    "/buyer-profiles/{profile_id}", response_model=BuyerProfileResponse,
    dependencies=[Depends(require_api_token)],
)
def get_buyer_profile(profile_id: int, repo: SupplierRepository = Depends(get_repo)) -> BuyerProfileResponse:
    profile = repo.get_buyer_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Buyer profile not found")
    return _to_buyer_profile_response(profile)


@app.delete("/buyer-profiles/{profile_id}", status_code=204, dependencies=[Depends(require_api_token)])
def delete_buyer_profile(profile_id: int, repo: SupplierRepository = Depends(get_repo)) -> None:
    if repo.get_buyer_profile(profile_id) is None:
        raise HTTPException(status_code=404, detail="Buyer profile not found")
    repo.delete_buyer_profile(profile_id)


@app.get(
    "/buyer-profiles/{profile_id}/search", response_model=List[CommercialSearchResult],
    dependencies=[Depends(require_api_token)],
)
def search_for_buyer_profile(
    profile_id: int, product: Optional[str] = None, limit: int = 50,
    repo: SupplierRepository = Depends(get_repo),
) -> List[CommercialSearchResult]:
    """The commercial-intelligence search: applies the profile's
    required fields as filters, scores every result against its
    preferred fields, and returns both the existing technical score
    and the new commercial-compatibility score, kept separate. See
    pipeline.buyer_profile_search's own docstring for the reasoning.
    """
    from pipeline.buyer_profile_search import search_suppliers_for_buyer_profile

    profile = repo.get_buyer_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Buyer profile not found")

    results = search_suppliers_for_buyer_profile(repo, profile, product_query=product, limit=limit)
    return [
        CommercialSearchResult(
            id=r["id"], canonical_name=r["canonical_name"], country=r.get("country"), domain=r.get("domain"),
            composite_score=r.get("composite_score"), evidence_coverage=r.get("evidence_coverage"),
            recommendation=r.get("recommendation"),
            is_manufacturer=bool(r["is_manufacturer"]) if r.get("is_manufacturer") is not None else None,
            commercial_compatibility_score=r.get("commercial_compatibility_score"),
            commercial_compatibility=r.get("commercial_compatibility") or {},
        )
        for r in results
    ]


# ═════════════════════════════════════════════════════
# Procurement outcomes (feedback loop) -- API only, no UI, per spec
# ═════════════════════════════════════════════════════


def _to_outcome_response(outcome: Dict[str, Any]) -> ProcurementOutcomeResponse:
    return ProcurementOutcomeResponse(
        id=outcome["id"], supplier_id=outcome["supplier_id"], buyer_profile_id=outcome.get("buyer_profile_id"),
        outcome=outcome["outcome"], notes=outcome.get("notes"), recorded_at=_stringify(outcome.get("recorded_at")),
    )


@app.post(
    "/suppliers/{supplier_id}/procurement-outcomes", response_model=ProcurementOutcomeResponse,
    status_code=201, dependencies=[Depends(require_api_token)],
)
def record_procurement_outcome(
    supplier_id: int, request: ProcurementOutcomeRequest, repo: SupplierRepository = Depends(get_repo),
) -> ProcurementOutcomeResponse:
    if repo.get_supplier(supplier_id) is None:
        raise HTTPException(status_code=404, detail="Supplier not found")
    outcome_id = repo.record_procurement_outcome(
        supplier_id=supplier_id, outcome=request.outcome,
        buyer_profile_id=request.buyer_profile_id, notes=request.notes,
    )
    outcome = repo.get_procurement_outcomes(supplier_id=supplier_id, limit=1000)
    return _to_outcome_response(next(o for o in outcome if o["id"] == outcome_id))


@app.get(
    "/suppliers/{supplier_id}/procurement-outcomes", response_model=List[ProcurementOutcomeResponse],
    dependencies=[Depends(require_api_token)],
)
def get_procurement_outcomes(
    supplier_id: int, repo: SupplierRepository = Depends(get_repo)
) -> List[ProcurementOutcomeResponse]:
    if repo.get_supplier(supplier_id) is None:
        raise HTTPException(status_code=404, detail="Supplier not found")
    return [_to_outcome_response(o) for o in repo.get_procurement_outcomes(supplier_id=supplier_id)]
