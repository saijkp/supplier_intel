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


class PipelineJobRequest(BaseModel):
    query: str
    sources: Optional[List[str]] = None
    run_verification: bool = True
    run_scoring: bool = True
    run_capability_extraction: bool = False
    run_website_discovery: bool = False
    run_facility_verification: bool = False
    run_linkedin_check: bool = False


class PipelineJobResponse(BaseModel):
    id: str
    status: str
    query: str
    stats: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
