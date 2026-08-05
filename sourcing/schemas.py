"""
sourcing/schemas.py

Output shapes shared across sourcing/brief_parser.py, sourcing/
sourcing_agent.py, and sourcing/dossier_generator.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class StructuredBrief:
    """What sourcing/brief_parser.py extracts from one free-text chat
    brief, and what sourcing_agent.SourcingAgentService.run() actually
    drives Discovery Service with."""
    product: str
    application: Optional[str] = None
    key_specifications: List[str] = field(default_factory=list)
    countries: List[str] = field(default_factory=list)  # priority order, as stated
    # Mapped through verification.capability_vocabulary.map_to_canonical --
    # an unrecognised term is DROPPED from filtering (never invented into
    # the vocabulary) but kept in unmapped_terms for display, so a filter
    # never silently rejects every candidate over one unrecognised phrase.
    required_capabilities: List[str] = field(default_factory=list)
    unmapped_terms: List[str] = field(default_factory=list)
    target_count: int = 10
    annual_volume: Optional[str] = None
    preferred_payment_terms: Optional[str] = None


@dataclass
class SourcingDossier:
    """The detailed per-supplier procurement checklist assessment --
    see sourcing/dossier_generator.py. Deliberately separate from
    verification_ai.narrative_generator.NarrativeResult: that answers
    a general "how much do independent sources corroborate this
    supplier," this answers "does this supplier satisfy THIS brief's
    checklist" -- never blended."""
    oem_odm_capability: str
    factory_manufacturing_processes: str
    engineering_testing_capability: str
    export_experience: str
    annual_volume_suitability: str
    payment_terms_assessment: str
    verification_status: str  # 'verified' | 'partially verified' | 'unverified'


@dataclass
class SourcingProgress:
    """One snapshot of a sourcing run's live status -- passed to
    sourcing_agent.SourcingAgentService.run()'s on_progress callback
    after each candidate is examined, and written onto
    pipeline_jobs.progress so GET /pipeline/jobs/{id} (already polled
    every few seconds by the frontend) shows real incremental status
    instead of silence until the whole run completes."""
    examined: int
    qualified: int
    target: int


@dataclass
class SourcingOutcome:
    """What sourcing_agent.SourcingAgentService.run() returns --
    dataclasses.asdict()'d as api/jobs.py's run_sourcing_job stats,
    same pattern as discovery.discovery_service.DiscoveryOutcome."""
    run_id: int
    brief: Optional[StructuredBrief]
    qualified_supplier_ids: List[int] = field(default_factory=list)
    examined_count: int = 0
    target_count: int = 0
    status: str = "completed"  # 'completed' | 'failed'
    error: Optional[str] = None
