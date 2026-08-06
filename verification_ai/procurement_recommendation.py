"""
verification_ai/procurement_recommendation.py

Answers the question a procurement manager actually asks: "would I send
this company an RFQ, and why?" Replaces the generic AI summary's role
as the headline output of a supplier's AI verification -- the summary
is still generated (verification_ai/narrative_generator.py) and still
stored, but this deterministic category + evidence-grounded reason is
now the primary thing shown.

Deterministic, NOT LLM-judged -- same reasoning
sourcing/dossier_generator.py's own `_verification_status()` already
established: the *category* must be reproducible and auditable, not
something the model could get subtly wrong or drift on between runs.
The reason string is also built deterministically, directly from the
same cross-check evidence that decided the category -- no new LLM
call, so this adds zero new per-supplier cost on top of what
VerificationService.verify() already runs.

This is a NEW, independent field -- it does NOT overwrite or blend
with `recommendation` (verification/scorer.py's platform-track-record
score) or `sourcing_verification_status` (a specific sourcing brief's
own checklist status). Same "never blend independently-computed
scores" precedent as ai_confidence_score vs composite_score.

Thresholds below are a first-cut heuristic, kept as plain module
constants (like verification_ai.confidence_scorer.CHECK_WEIGHTS) so
they're easy to find and tune -- NOT pre-validated against real
supplier data. Expect to revisit these after seeing them against
production suppliers.
"""

from __future__ import annotations

import dataclasses
from typing import Dict, List, Optional, Tuple

from verification_ai.cross_checker import CrossCheckResult

CATEGORY_RECOMMENDED_FOR_RFQ = "recommended_for_rfq"
CATEGORY_RECOMMENDED_FOR_PROTOTYPE = "recommended_for_prototype"
CATEGORY_RECOMMENDED_FOR_MASS_PRODUCTION = "recommended_for_mass_production"
CATEGORY_REQUIRES_FACTORY_AUDIT = "requires_factory_audit"
CATEGORY_NEEDS_MORE_VERIFICATION = "needs_more_verification"
CATEGORY_HIGH_RISK = "high_risk"
CATEGORY_AVOID = "avoid"

VALID_CATEGORIES: Tuple[str, ...] = (
    CATEGORY_RECOMMENDED_FOR_RFQ,
    CATEGORY_RECOMMENDED_FOR_PROTOTYPE,
    CATEGORY_RECOMMENDED_FOR_MASS_PRODUCTION,
    CATEGORY_REQUIRES_FACTORY_AUDIT,
    CATEGORY_NEEDS_MORE_VERIFICATION,
    CATEGORY_HIGH_RISK,
    CATEGORY_AVOID,
)

# First-cut thresholds on ai_confidence_score -- tunable, see module docstring.
AVOID_MAX_SCORE = 20
HIGH_RISK_MAX_SCORE = 40
NEEDS_MORE_VERIFICATION_MAX_SCORE = 60
STRONG_EVIDENCE_MIN_SCORE = 76

# Plain-English fragments for the reason string, keyed by (check name, verdict).
# Only checks that matter for an RFQ decision are translated here -- deliberately
# excludes linkedin_presence/phone_format/own_site_name_match, the same weak
# signals this whole feature de-emphasised in confidence_scorer.py.
_POSITIVE_FRAGMENTS: Dict[str, str] = {
    "manufacturer_assessment": "confirmed as a manufacturer",
    "facility_address": "a verified facility address",
    "certification_consistency": "claimed certifications corroborated on their own site",
    "oem_readiness_evidence": "OEM-readiness evidence (PPAP/CAD/traceability) on their own site",
    "export_shipment_evidence": "real export shipment records on file",
    "contact_verification": "a verified named contact with email on file",
}
_NEGATIVE_FRAGMENTS: Dict[str, str] = {
    "manufacturer_assessment": "identified as a trader, not a manufacturer",
    "facility_address": "the claimed facility address could not be independently verified",
    "certification_consistency": "claimed certifications were not corroborated on their own site",
}


@dataclasses.dataclass(frozen=True)
class ProcurementRecommendationResult:
    category: str
    reason: str


def _by_name(cross_check_result: CrossCheckResult) -> Dict[str, Optional[bool]]:
    return {c.name: c.verdict for c in cross_check_result.sub_checks}


def _evidence_fragments(by_name: Dict[str, Optional[bool]]) -> Tuple[List[str], List[str]]:
    positives = [text for name, text in _POSITIVE_FRAGMENTS.items() if by_name.get(name) is True]
    negatives = [text for name, text in _NEGATIVE_FRAGMENTS.items() if by_name.get(name) is False]
    return positives, negatives


_CATEGORY_CLOSING_CLAUSE: Dict[str, str] = {
    CATEGORY_RECOMMENDED_FOR_MASS_PRODUCTION: (
        "strong enough evidence across manufacturing, quality, and OEM-readiness signals "
        "to consider for a mass-production RFQ"
    ),
    CATEGORY_RECOMMENDED_FOR_PROTOTYPE: (
        "solid overall evidence, but not yet enough certification/OEM-readiness corroboration "
        "for a mass-production commitment -- suitable for a prototype-stage engagement"
    ),
    CATEGORY_RECOMMENDED_FOR_RFQ: "enough corroborated evidence to be worth sending an RFQ to",
    CATEGORY_REQUIRES_FACTORY_AUDIT: (
        "a reasonable overall score, but the facility itself is not independently verified -- "
        "a factory audit would resolve that gap before committing"
    ),
    CATEGORY_NEEDS_MORE_VERIFICATION: "too little corroborating evidence yet to make a confident RFQ decision either way",
    CATEGORY_HIGH_RISK: "significant unresolved evidence gaps or contradictions -- proceed only with caution",
    CATEGORY_AVOID: "evidence that actively weighs against engaging this supplier",
}


def _build_reason(category: str, confidence_score: int, by_name: Dict[str, Optional[bool]]) -> str:
    positives, negatives = _evidence_fragments(by_name)
    parts: List[str] = []
    if positives:
        parts.append(", ".join(positives).capitalize())
    if negatives:
        parts.append("but " + ", ".join(negatives) if positives else ", ".join(negatives).capitalize())
    evidence_sentence = " ".join(parts) if parts else "Little independent evidence is available either way"
    closing = _CATEGORY_CLOSING_CLAUSE[category]
    return f"{evidence_sentence} (AI confidence {confidence_score}/100) -- {closing}."


def categorise(
    confidence_score: int,
    is_manufacturer: Optional[bool],
    cross_check_result: CrossCheckResult,
) -> ProcurementRecommendationResult:
    """Deterministic mapping from (score, manufacturer status, cross-check
    evidence) to exactly one of the 7 VALID_CATEGORIES, plus a reason
    built from the same evidence. Never raises -- same contract as every
    other verifier/scorer in this codebase; callers should still treat
    this as pure/side-effect-free rather than needing a try/except, but
    it degrades to CATEGORY_NEEDS_MORE_VERIFICATION on any unexpected
    input shape rather than crashing a verify() call over a headline
    categorisation.
    """
    try:
        by_name = _by_name(cross_check_result)
        manufacturer_verdict = by_name.get("manufacturer_assessment")
        facility_verdict = by_name.get("facility_address")
        certs_verdict = by_name.get("certification_consistency")
        oem_verdict = by_name.get("oem_readiness_evidence")

        # Hard exclusion: a confirmed trader is never a manufacturing RFQ
        # target, regardless of how well everything else scores.
        if is_manufacturer is False or manufacturer_verdict is False:
            category = CATEGORY_AVOID
        elif confidence_score <= AVOID_MAX_SCORE:
            category = CATEGORY_AVOID
        elif confidence_score <= HIGH_RISK_MAX_SCORE or facility_verdict is False:
            category = CATEGORY_HIGH_RISK
        elif confidence_score <= NEEDS_MORE_VERIFICATION_MAX_SCORE:
            category = CATEGORY_NEEDS_MORE_VERIFICATION
        elif facility_verdict is not True:
            # Score alone looks corroborated, but the one check that most
            # directly answers "is this a real place" has no positive
            # signal -- an audit resolves that specific gap.
            category = CATEGORY_REQUIRES_FACTORY_AUDIT
        elif confidence_score >= STRONG_EVIDENCE_MIN_SCORE and certs_verdict is True and oem_verdict is True:
            category = CATEGORY_RECOMMENDED_FOR_MASS_PRODUCTION
        elif confidence_score >= STRONG_EVIDENCE_MIN_SCORE:
            category = CATEGORY_RECOMMENDED_FOR_PROTOTYPE
        else:
            category = CATEGORY_RECOMMENDED_FOR_RFQ

        reason = _build_reason(category, confidence_score, by_name)
        return ProcurementRecommendationResult(category=category, reason=reason)
    except Exception:  # noqa: BLE001 -- a headline categorisation bug must never abort verify()
        return ProcurementRecommendationResult(
            category=CATEGORY_NEEDS_MORE_VERIFICATION,
            reason="Could not be categorised due to an unexpected evidence shape -- treat as needing more verification.",
        )
