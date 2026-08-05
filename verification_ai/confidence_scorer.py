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

from typing import Tuple

from verification_ai.cross_checker import CrossCheckResult

# Sums to 100, same convention as ManufacturerVerifier.ASSESSOR_WEIGHTS.
# manufacturer_assessment carries less weight here than in
# ManufacturerVerifier's own scoring (where it IS the whole score) since
# it's one signal among several independent corroborations here, not the
# entire question being asked.
#
# cross_checker.py's "export_shipment_evidence" sub-check (added
# alongside the Sourcing Agent's trade-data cross-check) is DELIBERATELY
# NOT weighted here -- same precedent verification/capability_extractor.py's
# own docstring already sets for a new signal: rebalancing these weights
# would change ai_confidence_score for every supplier already scored
# under the current weights, and that's a decision worth making on
# purpose, not as a side effect of adding one more signal. It's still
# visible in cross-check evidence and the sourcing dossier narrative,
# just not moving this number yet.
CHECK_WEIGHTS: Tuple[Tuple[str, int], ...] = (
    ("manufacturer_assessment", 25),
    ("facility_address", 25),
    ("linkedin_presence", 15),
    ("phone_format", 10),
    ("own_site_name_match", 15),
    ("certification_consistency", 10),
)


class ConfidenceScorer:

    def score(self, cross_check_result: CrossCheckResult) -> int:
        """Starts at a neutral 50 (same convention as ManufacturerVerifier)
        -- each sub-check with a real verdict (True/False) moves the
        score by its weight; a sub-check with no signal (None, or never
        run at all) leaves the score untouched. Clamped to [0, 100]."""
        by_name = {c.name: c.verdict for c in cross_check_result.sub_checks}

        score = 50
        for name, weight in CHECK_WEIGHTS:
            verdict = by_name.get(name)
            if verdict is None:
                continue
            score += weight if verdict else -weight

        return max(0, min(100, score))
