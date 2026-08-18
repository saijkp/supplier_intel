"""
verification/uk_company_verification_service.py

Orchestrates CompaniesHouseClient against real suppliers and records
the outcome via storage.repository.SupplierRepository -- same
single/batch/semaphore/idempotency-marker shape as
collection/collection_service.py's CollectionService (its direct
template), CLI-only for now (main.py verify-uk-company): no async job,
no POST /uk-verification/jobs endpoint, no frontend button, given the
deadline this was built under -- add that apparatus later if it's
ever wanted, following the exact same recurring pattern
ContactFinderService/FactoryFactsService already established.

Used specifically as the UK-office validation gate for categories that
require it (currently: Material Handling). Deliberately has NO
category awareness of its own -- like batch-upload and the discovery
pipeline, it's invoked manually against whatever candidate list or
supplier ids the caller hands it (a CSV, or ids from a prior discovery
run). It doesn't need to know why it's being asked to check a given
supplier.

Matching discipline: searches Companies House by canonical_name, fuzzy-
matches every search result's registered name against it (rapidfuzz,
same tool discovery.candidate_validator.py already uses for the
analogous "is this really the company" question), and produces one of
three outcomes -- never a binary accept/reject:
  - "verified"        -- a high-confidence match whose company_status
                          is "active".
  - "inactive"         -- a high-confidence match, but NOT active
                          (dissolved/liquidation/administration/etc.)
                          -- a real flag, surfaced, never silently
                          dropped.
  - "no_clear_match"    -- nothing cleared the confidence bar (including
                          zero search results). Explicitly NOT treated
                          as a rejection: a trading name differing from
                          the registered legal name is common and not
                          itself suspicious. Surfaced as "check
                          manually," same evidence-not-verdict
                          discipline as every other stage this session.

_CLEAN_MATCH_THRESHOLD is deliberately higher than discovery.
candidate_validator's own name-match threshold (55.0): falsely
confirming the WRONG company's official government registration is a
worse failure mode than under-matching into "no_clear_match" -- the
buyer loses nothing by checking one more company by hand, but a wrong
auto-verify would be actively misleading in a compliance-adjacent
check like this one.

Company facts (company_status, registered_office_address,
date_of_creation, sic_codes) are stored on the suppliers row AND as
field_provenance entries with source_tier="other" (Companies House is
a third-party registry, not the supplier's own site, so never
"own_domain") and claim_type="verifiable_fact" (an official government
filing, not a self-reported claim) -- same provenance discipline every
other extracted field in this codebase already follows.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from rapidfuzz import fuzz

from config.settings import UK_VERIFICATION_JOB_MAX_SECONDS, UK_VERIFICATION_MAX_CONCURRENT_JOBS
from storage.repository import SupplierRepository
from verification.companies_house_client import CompaniesHouseClient

logger = logging.getLogger(__name__)

# Shared across UKCompanyVerificationService instances within one
# process -- see module docstring / CollectionService's own for why
# this needs to be process-wide, not per-instance.
_BATCH_SEMAPHORE = threading.Semaphore(UK_VERIFICATION_MAX_CONCURRENT_JOBS)

# See module docstring for why this is deliberately higher than
# discovery.candidate_validator's 55.0 name-match threshold.
_CLEAN_MATCH_THRESHOLD = 85.0

# Companies House ALWAYS returns the registered name with its legal
# suffix; a supplied/trading name almost never includes one -- found
# via a real gap-analysis run: Rutland Plastics/Inoplas Technology/
# Hymid Multi-Shot/Barkley Plastics all scored just under
# _CLEAN_MATCH_THRESHOLD purely because of the missing "Limited," not
# a real mismatch (confirmed by checking the actual matched titles --
# e.g. "RUTLAND PLASTICS LIMITED" vs supplied "Rutland Plastics").
# Stripped from BOTH sides before scoring, never from what's actually
# stored (companies_house_status etc. keep the verbatim registered
# name/title). Deliberately does NOT touch _CLEAN_MATCH_THRESHOLD
# itself -- that bar is doing real, necessary work against coincidental
# homonyms (e.g. "Sungplastic" vs the unrelated "NINGBO REIZ MOULD &
# PLASTIC CO., LTD," correctly still scores low after stripping "LTD,"
# since the actual company names have nothing else in common).
_UK_CORPORATE_SUFFIX_PATTERN = re.compile(
    r",?\s*\b(limited|ltd\.?|plc|llp|l\.l\.p\.?)\s*$", re.IGNORECASE,
)


def _strip_corporate_suffix(name: str) -> str:
    return _UK_CORPORATE_SUFFIX_PATTERN.sub("", name or "").strip()

# Fields written to field_provenance alongside the supplier row, in
# (field_name, value) pairs built fresh per call -- see _verify_one.
_PROVENANCE_FIELDS = (
    "companies_house_status", "companies_house_registered_office",
    "companies_house_incorporated_at", "companies_house_sic_codes",
)


def match_company_name_against_companies_house(
    company_name: str, client: CompaniesHouseClient, max_results: int = 5,
    min_confidence: float = _CLEAN_MATCH_THRESHOLD,
) -> Dict[str, Any]:
    """Stateless core of _verify_one's matching step -- search, fuzzy-
    match against `min_confidence`, resolve the profile -- with no
    supplier row and no DB writes. Factored out so a caller with no
    golden record yet (discovery.candidate_validator's pre-LLM UK
    registration gate, see that module's own docstring) can reuse the
    exact same matching discipline instead of a second, independently
    drifting implementation. Returns match_status
    ("verified"/"inactive"/"no_clear_match"), confidence (0-100 or None),
    profile (CompanyProfile or None), and matched_title (the best
    Companies House search hit's registered name, for logging/evidence
    even on a no_clear_match).

    `min_confidence` defaults to _CLEAN_MATCH_THRESHOLD (85, the bar
    calibrated for _verify_one's final compliance-adjacent verdict on a
    real supplier's canonical_name -- see that constant's own
    docstring for why false-CONFIRM is the failure mode to avoid
    there). A caller checking a much noisier, pre-extraction name guess
    (candidate_validator's gate 3.5) has the OPPOSITE risk profile: a
    false pass there just costs one already-budgeted LLM call, same as
    not having the gate at all, while a false reject silently discards
    a candidate that already passed every real signal available at
    that point -- so that caller passes its own, deliberately lower,
    threshold here rather than reusing 85."""
    company_name = (company_name or "").strip()
    if not company_name:
        return {"match_status": "no_clear_match", "confidence": None, "profile": None, "matched_title": None}

    try:
        matches = client.search_companies(company_name, max_results=max_results)
    except Exception as e:  # noqa: BLE001 -- CompaniesHouseClient already never raises; defence in depth
        logger.error("uk_company_verification: search failed for %r: %s", company_name, e)
        return {"match_status": "no_clear_match", "confidence": None, "profile": None, "matched_title": None}

    stripped_company_name = _strip_corporate_suffix(company_name).lower()
    best_match, best_score = None, 0.0
    for match in matches:
        stripped_title = _strip_corporate_suffix(match.title or "").lower()
        score = fuzz.ratio(stripped_company_name, stripped_title)
        if score > best_score:
            best_match, best_score = match, score

    if best_match is None:
        return {"match_status": "no_clear_match", "confidence": None, "profile": None, "matched_title": None}
    if best_score < min_confidence:
        return {
            "match_status": "no_clear_match", "confidence": round(best_score),
            "profile": None, "matched_title": best_match.title,
        }

    profile = client.get_company_profile(best_match.company_number)
    if profile is None:
        return {
            "match_status": "no_clear_match", "confidence": round(best_score),
            "profile": None, "matched_title": best_match.title,
        }

    match_status = "verified" if (profile.company_status or "").lower() == "active" else "inactive"
    return {
        "match_status": match_status, "confidence": round(best_score),
        "profile": profile, "matched_title": best_match.title,
    }


class UKCompanyVerificationService:

    def __init__(
        self,
        repo: Optional[SupplierRepository] = None,
        companies_house_client: Optional[CompaniesHouseClient] = None,
        job_max_seconds: int = UK_VERIFICATION_JOB_MAX_SECONDS,
    ):
        self.repo = repo or SupplierRepository()
        self.companies_house_client = companies_house_client or CompaniesHouseClient()
        self.job_max_seconds = job_max_seconds

    def verify_uk_company(self, supplier_id: int) -> Dict[str, Any]:
        """Verify one supplier by id -- always runs, not gated by
        companies_house_checked_at/force (a caller who names a specific
        supplier clearly wants it checked)."""
        supplier = self.repo.get_supplier(supplier_id)
        if supplier is None:
            raise ValueError(f"No supplier with id={supplier_id}")
        return self._verify_one(supplier)

    def _verify_one(self, supplier: Dict[str, Any]) -> Dict[str, Any]:
        supplier_id = supplier["id"]
        company_name = (supplier.get("canonical_name") or "").strip()
        checked_at = datetime.now(timezone.utc).isoformat()

        if not company_name:
            return self._record_no_clear_match(supplier_id, checked_at, None, "no company name on file to search")

        match = match_company_name_against_companies_house(company_name, self.companies_house_client)
        confidence = match["confidence"]
        profile = match["profile"]

        if match["match_status"] == "no_clear_match":
            if profile is None and match["matched_title"] is None and confidence is None:
                reason = "no Companies House search results at all"
            elif confidence is not None and profile is None and confidence < _CLEAN_MATCH_THRESHOLD:
                reason = (
                    f"best search match '{match['matched_title']}' scored {confidence:.0f}, "
                    f"below the {_CLEAN_MATCH_THRESHOLD:.0f} confidence bar"
                )
            else:
                reason = f"matched '{match['matched_title']}' (score={confidence:.0f}) but the company profile lookup failed"
            return self._record_no_clear_match(supplier_id, checked_at, confidence, reason)

        match_status = match["match_status"]

        fields = {
            "companies_house_number": profile.company_number,
            "companies_house_status": profile.company_status,
            "companies_house_registered_office": profile.registered_office_address,
            "companies_house_incorporated_at": profile.date_of_creation,
            "companies_house_sic_codes": profile.sic_codes,
            "companies_house_match_status": match_status,
            "companies_house_match_confidence": confidence,
            "companies_house_checked_at": checked_at,
        }
        self.repo.update_supplier_fields_with_history(
            supplier_id, fields, changed_by="uk_company_verification_service",
            change_reason=f"Companies House match (confidence={confidence}): status={profile.company_status}",
        )

        profile_values = {
            "companies_house_status": profile.company_status,
            "companies_house_registered_office": profile.registered_office_address,
            "companies_house_incorporated_at": profile.date_of_creation,
            "companies_house_sic_codes": profile.sic_codes,
        }
        for field_name in _PROVENANCE_FIELDS:
            value = profile_values[field_name]
            if not value:
                continue
            self.repo.save_field_provenance(
                supplier_id=supplier_id, field_name=field_name,
                value=value if isinstance(value, str) else json.dumps(value),
                source_url=profile.source_url, raw_snippet=None,
                extraction_method="companies_house_api", source_tier="other", claim_type="verifiable_fact",
            )

        return {
            "supplier_id": supplier_id, "match_status": match_status, "confidence": confidence,
            "company_number": profile.company_number, "company_status": profile.company_status,
        }

    def _record_no_clear_match(
        self, supplier_id: int, checked_at: str, confidence: Optional[int], reason: str,
    ) -> Dict[str, Any]:
        self.repo.update_supplier_fields_with_history(
            supplier_id,
            {
                "companies_house_match_status": "no_clear_match",
                "companies_house_match_confidence": confidence,
                "companies_house_checked_at": checked_at,
            },
            changed_by="uk_company_verification_service", change_reason=reason,
        )
        return {"supplier_id": supplier_id, "match_status": "no_clear_match", "confidence": confidence, "reason": reason}

    def verify_uk_company_batch(self, supplier_ids: List[int], force: bool = False) -> Dict[str, Any]:
        """Batch pass over an EXPLICIT list of supplier ids -- never a
        whole-database scan (see module docstring: this service has no
        category awareness, so it can't know which suppliers "need"
        checking on its own). Skips ids already checked
        (companies_house_checked_at IS NOT NULL) unless force=True."""
        acquired = _BATCH_SEMAPHORE.acquire(blocking=False)
        if not acquired:
            return {
                "attempted": 0, "verified": 0, "inactive": 0, "no_clear_match": 0,
                "total_given": len(supplier_ids), "status": "skipped",
                "reason": "another UK company verification batch is already running on this instance",
            }

        try:
            start_time = time.monotonic()
            attempted = verified = inactive = no_clear_match = 0
            stopped_early = False

            for supplier_id in supplier_ids:
                if time.monotonic() - start_time > self.job_max_seconds:
                    stopped_early = True
                    break

                supplier = self.repo.get_supplier(supplier_id)
                if supplier is None:
                    continue
                if not force and supplier.get("companies_house_checked_at"):
                    continue

                outcome = self._verify_one(supplier)
                attempted += 1
                if outcome["match_status"] == "verified":
                    verified += 1
                elif outcome["match_status"] == "inactive":
                    inactive += 1
                else:
                    no_clear_match += 1

            return {
                "attempted": attempted, "verified": verified, "inactive": inactive,
                "no_clear_match": no_clear_match, "total_given": len(supplier_ids),
                "status": "partial" if stopped_early else "completed",
            }
        finally:
            _BATCH_SEMAPHORE.release()
