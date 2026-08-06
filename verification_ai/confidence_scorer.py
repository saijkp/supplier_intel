"""
verification_ai/confidence_scorer.py

Deterministic, rule-based 0-100 rollup of a CrossCheckResult -- NOT an
LLM call. Mirrors verification.manufacturer_verifier.ManufacturerVerifier
and verification.scorer.SupplierScorer's own weighted-checks style
exactly, so the *number* stays auditable/reproducible even though the
accompanying prose (verification_ai.narrative_generator) is
AI-generated. See the redesign plan for why ai_confidence_score is kept
deliberately separate from the existing composite_score: composite_score
answers "how strong is this supplier's platform track record,"
ai_confidence_score answers "how much do independent sources corroborate
this supplier's claimed identity and capabilities" -- different
questions, never blended into one number.
"""

from __future__ import annotations

import dataclasses
from typing import List, Optional, Tuple

from verification_ai.cross_checker import CrossCheckResult

# Rebalanced for the Procurement Decision Engine foundation: weighted
# toward real procurement evidence (can they manufacture, is the
# facility real, are certifications corroborated, is there OEM-readiness/
# export/contact evidence) and sharply away from weak, generic signals
# (LinkedIn presence, phone-number format, a bare name match on their
# own website) that don't actually answer "would I send this company an
# RFQ." Does NOT sum to 100 -- this is a starts-at-50-then-±weight model
# (see ConfidenceScorer.score below), not a weighted-sum-to-100 one, so
# there's no such constraint.
#
# phone_format is deliberately ABSENT here (previously weighted 10) --
# still computed by cross_checker.py and still visible in the AI
# narrative's evidence text, just no longer moving this number: a
# phone's format being locally plausible was never real evidence of
# manufacturing capability or trustworthiness.
#
# oem_readiness_evidence/export_shipment_evidence/contact_verification
# are new sub-checks (see cross_checker.py) added alongside this
# rebalance specifically because they ARE real procurement evidence
# that was previously either unweighted (export_shipment_evidence) or
# not computed at all.
CHECK_WEIGHTS: Tuple[Tuple[str, int], ...] = (
    ("manufacturer_assessment", 25),
    ("facility_address", 25),
    ("certification_consistency", 15),
    ("oem_readiness_evidence", 15),
    ("export_shipment_evidence", 10),
    ("contact_verification", 5),
    ("own_site_name_match", 5),
    ("linkedin_presence", 5),
)


@dataclasses.dataclass(frozen=True)
class ConfidenceScoreBreakdownEntry:
    name: str
    weight: int
    verdict: Optional[bool]  # None = no signal for this check
    contribution: int        # +weight / -weight / 0 (no signal)


@dataclasses.dataclass(frozen=True)
class ConfidenceScoreResult:
    score: int
    breakdown: List[ConfidenceScoreBreakdownEntry]


class ConfidenceScorer:

    def score(self, cross_check_result: CrossCheckResult) -> int:
        """Starts at a neutral 50 (same convention as ManufacturerVerifier)
        -- each sub-check with a real verdict (True/False) moves the
        score by its weight; a sub-check with no signal (None, or never
        run at all) leaves the score untouched. Clamped to [0, 100].
        Thin wrapper over score_with_breakdown -- kept so every existing
        caller/test asserting a bare int is unaffected by the breakdown
        addition."""
        return self.score_with_breakdown(cross_check_result).score

    def score_with_breakdown(self, cross_check_result: CrossCheckResult) -> ConfidenceScoreResult:
        """Same scoring logic as score() above, but also returns which
        checks ran, what they found, and exactly how many points each
        one contributed -- so a caller (VerificationService, then the
        UI) can show a complete, auditable breakdown of the number
        rather than just the number itself."""
        by_name = {c.name: c.verdict for c in cross_check_result.sub_checks}

        score = 50
        breakdown: List[ConfidenceScoreBreakdownEntry] = []
        for name, weight in CHECK_WEIGHTS:
            verdict = by_name.get(name)
            contribution = 0
            if verdict is not None:
                contribution = weight if verdict else -weight
                score += contribution
            breakdown.append(ConfidenceScoreBreakdownEntry(
                name=name, weight=weight, verdict=verdict, contribution=contribution,
            ))

        return ConfidenceScoreResult(score=max(0, min(100, score)), breakdown=breakdown)
