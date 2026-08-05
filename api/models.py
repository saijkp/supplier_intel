"""
api/models.py

Pydantic request/response shapes for api/app.py. Deliberately built as
explicit models constructed field-by-field in app.py (see
`_to_search_result`/`_to_job_response` there), not `model_validate`'d
straight off a raw SQLite row -- a couple of fields need real
conversion (SQLite stores booleans as 0/1 integers; `Optional[bool]`
needs those coerced explicitly) and an explicit constructor keeps that
conversion visible and testable in one place rather than relying on
Pydantic's own coercion rules to happen to do the right thing.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SupplierSearchResult(BaseModel):
    id: int
    canonical_name: str
    country: Optional[str] = None
    domain: Optional[str] = None
    composite_score: Optional[int] = None
    recommendation: Optional[str] = None
    is_manufacturer: Optional[bool] = None
    primary_email: Optional[str] = None
    primary_phone: Optional[str] = None
    contact_form_url: Optional[str] = None
    linkedin_url: Optional[str] = None
    factory_photo_verdict: Optional[str] = None
    facility_address_verified: Optional[bool] = None
    year_established: Optional[int] = None
    alibaba_years: Optional[int] = None
    matched_capabilities: List[Dict[str, Any]] = Field(default_factory=list)
    # Additive fields from the AI Discovery/Collection/Verification
    # platform (v11) -- all Optional/absent-safe so existing frontend
    # consumers are unaffected until they choose to read them.
    ai_confidence_score: Optional[int] = None
    ai_summary: Optional[str] = None
    ai_strengths: List[str] = Field(default_factory=list)
    ai_risks: List[str] = Field(default_factory=list)
    ai_suitable_customer_types: List[str] = Field(default_factory=list)
    collection_status: Optional[str] = None
    # Sourcing Agent procurement dossier (v12) -- see
    # sourcing/dossier_generator.py. Empty/None for any supplier never
    # processed by a sourcing run, same additive-and-absent-safe
    # convention as the ai_* fields above.
    sourcing_oem_odm_notes: Optional[str] = None
    sourcing_factory_notes: Optional[str] = None
    sourcing_engineering_notes: Optional[str] = None
    sourcing_export_notes: Optional[str] = None
    sourcing_volume_suitability: Optional[str] = None
    sourcing_payment_terms_notes: Optional[str] = None
    sourcing_verification_status: Optional[str] = None


class PipelineJobRequest(BaseModel):
    query: str
    sources: Optional[List[str]] = None
    limit: Optional[int] = Field(
        default=None,
        description=(
            "Cap raw results kept PER SOURCE to this many -- mirrors main.py run's own "
            "--limit exactly (see pipeline.orchestrator.build_limit_scraper_kwargs, shared "
            "by both). No cap by default. Strongly recommended for alibaba/indiamart/"
            "china_1688/google: these are pay-per-event/metered, and without a limit each "
            "falls back to its own scraper default (50 for alibaba, 20 for china_1688) with "
            "no per-request ceiling."
        ),
    )
    run_verification: bool = True
    run_scoring: bool = True
    run_capability_extraction: bool = False
    run_website_discovery: bool = False
    run_facility_verification: bool = False
    run_linkedin_check: bool = False


class EnrichmentJobRequest(BaseModel):
    """Runs one of the standalone enrichment passes across every
    existing supplier that needs it -- NOT tied to a search query,
    unlike PipelineJobRequest. These are the same three commands
    main.py already exposes as `find-websites`/`extract-capabilities`/
    `verify-facilities`; this is the HTTP equivalent so a job can be
    triggered without shell access to wherever the API is deployed."""

    stage: str = Field(
        description='One of "find_websites", "extract_capabilities", "verify_facilities".'
    )
    force: bool = Field(
        default=False,
        description="Re-attempt suppliers already processed, not just never-attempted ones.",
    )
    limit: Optional[int] = Field(
        default=None,
        description=(
            "Stop after this many suppliers. Applies to find_websites (SerpAPI call per "
            "supplier), extract_capabilities (real HTTP + OpenAI call per supplier), and "
            "verify_facilities (Google Places/Amap call per supplier). No cap by default -- "
            "start small on a first real run."
        ),
    )
    assess_photos: bool = Field(
        default=False,
        description=(
            "extract_capabilities only. Off by default here (unlike the CLI's own default) "
            "-- factory photo assessment sends images to a vision model per supplier and is "
            "by far the most expensive part of this stage; opt in explicitly once text-only "
            "extraction quality has been checked."
        ),
    )


class CollectionJobRequest(BaseModel):
    """Triggers collection.collection_service.CollectionService against
    either one named supplier or a batch of every supplier needing it
    -- the HTTP equivalent of `main.py collect`. Reuses the same
    pipeline_jobs table/poll pattern as PipelineJobRequest/
    EnrichmentJobRequest (see api/jobs.py's run_collection_job)."""

    supplier_id: Optional[int] = Field(
        default=None,
        description="Collect against one specific supplier. Ignores pending/limit/force.",
    )
    pending: bool = Field(
        default=False,
        description="Batch mode: collect against every supplier needing it. Exactly one of "
                     "supplier_id or pending must be set.",
    )
    limit: int = Field(
        default=20,
        description="Stop after this many suppliers in pending mode. Each one launches a real "
                     "headless-browser session -- start small on a first run.",
    )
    force: bool = Field(
        default=False,
        description="In pending mode, re-collect every supplier with a domain, not just ones "
                     "never collected.",
    )


class VerificationJobRequest(BaseModel):
    """Triggers verification_ai.verification_service.VerificationService
    against either one named supplier or a batch of every supplier
    needing it -- the HTTP equivalent of `main.py verify-ai`. Same
    job/poll pattern as CollectionJobRequest."""

    supplier_id: Optional[int] = Field(
        default=None,
        description="Verify one specific supplier. Ignores pending/limit/force.",
    )
    pending: bool = Field(
        default=False,
        description="Batch mode: verify every supplier needing it. Exactly one of supplier_id "
                     "or pending must be set.",
    )
    limit: int = Field(
        default=20,
        description="Stop after this many suppliers in pending mode. Each one costs a real "
                     "OpenAI call -- start small on a first run.",
    )
    force: bool = Field(
        default=False,
        description="In pending mode, re-verify every supplier, not just ones never assessed.",
    )


class DiscoveryJobRequest(BaseModel):
    """Triggers discovery.discovery_service.DiscoveryService -- the HTTP
    equivalent of `main.py discover`. AI-assisted supplier discovery,
    grounded entirely in real SerpAPI search results (see
    discovery/discovery_service.py's module docstring)."""

    product: str = Field(description="Product/component search term, e.g. 'trailer axle'.")
    category: Optional[str] = Field(default=None, description="Optional product category, recorded on discovery_runs.")
    country: Optional[str] = Field(default=None, description="Optional country to qualify the search.")
    max_candidates: int = Field(
        default=20,
        description="Stop after this many candidate companies. Each one costs a paid SerpAPI "
                     "search plus, for candidates that pass initial filtering, a real HTTP "
                     "fetch and an OpenAI call -- start small on a first run.",
    )


class SourcingRunRequest(BaseModel):
    """Triggers sourcing.sourcing_agent.SourcingAgentService -- one
    free-text procurement brief drives the entire discover -> collect
    -> verify -> qualify loop (see sourcing/sourcing_agent.py's module
    docstring). The HTTP equivalent of a chat message on the frontend's
    Source tab."""

    brief_text: str = Field(
        description="Free-text sourcing brief, e.g. 'find 15 genuine winch manufacturers for "
                     "off-road trailer recovery, ISO 9001, prioritise China then India, annual "
                     "volume 5000pcs, 30 day payment terms'.",
    )
    max_multiplier: int = Field(
        default=5,
        description="Hard ceiling on candidates examined = target_count * max_multiplier, even if "
                     "the target count isn't reached -- cost governance, not a soft target.",
    )


class SourcingRunResponse(BaseModel):
    """GET /sourcing/runs/{id} -- the run itself plus its qualified
    suppliers resolved in full (not just ids), so the frontend can
    render results in one call. `qualified_suppliers` is empty while
    status is still 'running'; poll GET /pipeline/jobs/{job_id}
    (returned by POST /sourcing/runs) for live progress until then."""

    id: int
    brief_text: str
    structured_brief: Optional[Dict[str, Any]] = None
    target_count: int
    examined_count: int
    status: str
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    completed_at: Optional[str] = None
    qualified_suppliers: List[SupplierSearchResult] = Field(default_factory=list)


class PipelineJobResponse(BaseModel):
    id: str
    status: str
    query: str
    stats: Optional[Dict[str, Any]] = None
    progress: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Live incremental status for a long-running job (currently only written by "
                     "sourcing.SourcingAgentService's per-candidate progress callback), e.g. "
                     "{'examined': 7, 'qualified': 3, 'target': 10} -- polled the same way `stats` "
                     "already is, just before the job actually completes.",
    )
    error: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class BuyerProfileRequest(BaseModel):
    name: str
    destination_country: Optional[str] = None
    required_capabilities: List[str] = Field(default_factory=list)
    preferred_incoterm: Optional[str] = None
    preferred_payment_terms_days: Optional[int] = None
    min_company_size: Optional[str] = None
    target_market: Optional[str] = None
    min_export_experience_years: Optional[int] = None
    manufacturers_only: bool = True


class BuyerProfileResponse(BaseModel):
    id: int
    name: str
    destination_country: Optional[str] = None
    required_capabilities: List[str] = Field(default_factory=list)
    preferred_incoterm: Optional[str] = None
    preferred_payment_terms_days: Optional[int] = None
    min_company_size: Optional[str] = None
    target_market: Optional[str] = None
    min_export_experience_years: Optional[int] = None
    manufacturers_only: bool
    created_at: Optional[str] = None


class CommercialFactorResponse(BaseModel):
    factor_name: str
    value: Optional[Any] = None
    confidence: float
    evidence: Optional[str] = None
    source: str
    reasoning: str


class CommercialSearchResult(BaseModel):
    """Deliberately keeps composite_score (technical) and
    commercial_compatibility_score separate — see
    pipeline.buyer_profile_search's own docstring for why these are
    never silently blended into one number."""

    id: int
    canonical_name: str
    country: Optional[str] = None
    domain: Optional[str] = None
    composite_score: Optional[int] = None
    recommendation: Optional[str] = None
    is_manufacturer: Optional[bool] = None
    commercial_compatibility_score: Optional[float] = None
    commercial_compatibility: Dict[str, Any] = Field(default_factory=dict)


class ProcurementOutcomeRequest(BaseModel):
    outcome: str
    buyer_profile_id: Optional[int] = None
    notes: Optional[str] = None


class ProcurementOutcomeResponse(BaseModel):
    id: int
    supplier_id: int
    buyer_profile_id: Optional[int] = None
    outcome: str
    notes: Optional[str] = None
    recorded_at: Optional[str] = None
