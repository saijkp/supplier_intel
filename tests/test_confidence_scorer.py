"""
tests/test_confidence_scorer.py

Tests for verification_ai/confidence_scorer.py -- deterministic,
rule-based 0-100 rollup of a CrossCheckResult. No LLM call involved.
"""

from __future__ import annotations

from verification_ai.confidence_scorer import ConfidenceScorer
from verification_ai.cross_checker import CrossCheckResult, SubCheckResult


def _result(*sub_checks):
    return CrossCheckResult(sub_checks=list(sub_checks))


class TestConfidenceScorer:

    def test_no_sub_checks_returns_neutral_50(self):
        scorer = ConfidenceScorer()
        assert scorer.score(_result()) == 50

    def test_all_no_signal_stays_neutral(self):
        scorer = ConfidenceScorer()
        result = _result(
            SubCheckResult(name="manufacturer_assessment", verdict=None, detail="x"),
            SubCheckResult(name="facility_address", verdict=None, detail="x"),
        )
        assert scorer.score(result) == 50

    def test_all_confirmed_reaches_the_100_clamp(self):
        scorer = ConfidenceScorer()
        result = _result(
            SubCheckResult(name="manufacturer_assessment", verdict=True, detail="x"),
            SubCheckResult(name="facility_address", verdict=True, detail="x"),
            SubCheckResult(name="linkedin_presence", verdict=True, detail="x"),
            SubCheckResult(name="phone_format", verdict=True, detail="x"),
            SubCheckResult(name="own_site_name_match", verdict=True, detail="x"),
            SubCheckResult(name="certification_consistency", verdict=True, detail="x"),
        )
        assert scorer.score(result) == 100

    def test_all_contradicted_reaches_the_0_clamp(self):
        scorer = ConfidenceScorer()
        result = _result(
            SubCheckResult(name="manufacturer_assessment", verdict=False, detail="x"),
            SubCheckResult(name="facility_address", verdict=False, detail="x"),
            SubCheckResult(name="linkedin_presence", verdict=False, detail="x"),
            SubCheckResult(name="phone_format", verdict=False, detail="x"),
            SubCheckResult(name="own_site_name_match", verdict=False, detail="x"),
            SubCheckResult(name="certification_consistency", verdict=False, detail="x"),
        )
        assert scorer.score(result) == 0

    def test_one_confirmed_signal_moves_score_by_its_weight(self):
        scorer = ConfidenceScorer()
        result = _result(SubCheckResult(name="facility_address", verdict=True, detail="x"))
        assert scorer.score(result) == 50 + 25  # facility_address weight

    def test_one_contradicted_signal_moves_score_down_by_its_weight(self):
        scorer = ConfidenceScorer()
        result = _result(SubCheckResult(name="linkedin_presence", verdict=False, detail="x"))
        assert scorer.score(result) == 50 - 5  # linkedin_presence weight

    def test_unknown_sub_check_names_are_ignored(self):
        """A sub-check name that isn't in CHECK_WEIGHTS (e.g. a future
        addition not yet wired into scoring) must not crash or silently
        contribute an undefined weight."""
        scorer = ConfidenceScorer()
        result = _result(SubCheckResult(name="some_future_check", verdict=True, detail="x"))
        assert scorer.score(result) == 50

    def test_score_is_always_an_int(self):
        scorer = ConfidenceScorer()
        result = _result(SubCheckResult(name="facility_address", verdict=True, detail="x"))
        assert isinstance(scorer.score(result), int)

    def test_export_shipment_evidence_is_now_weighted(self):
        """Procurement Decision Engine foundation (Phase 1): real
        UK/EU/US customs evidence is real procurement-relevant
        evidence, previously computed but deliberately left unweighted
        -- now weighted on purpose. Regression guard against silently
        reverting."""
        from verification_ai.confidence_scorer import CHECK_WEIGHTS

        assert dict(CHECK_WEIGHTS)["export_shipment_evidence"] == 10

        scorer = ConfidenceScorer()
        result = _result(SubCheckResult(name="export_shipment_evidence", verdict=True, detail="x"))
        assert scorer.score(result) == 60

    def test_phone_format_is_deliberately_unweighted(self):
        """Regression guard for the other half of the same rebalance:
        phone-number format plausibility is weak, generic evidence --
        deliberately removed from CHECK_WEIGHTS (still computed by
        cross_checker.py and still shown in the AI narrative's evidence
        text, just no longer moving this number). A future accidental
        re-addition would silently start moving every already-scored
        supplier's ai_confidence_score; this catches that."""
        from verification_ai.confidence_scorer import CHECK_WEIGHTS

        assert "phone_format" not in dict(CHECK_WEIGHTS)

        scorer = ConfidenceScorer()
        result = _result(SubCheckResult(name="phone_format", verdict=True, detail="x"))
        assert scorer.score(result) == 50


class TestConfidenceScoreBreakdown:

    def test_breakdown_includes_every_weighted_check(self):
        from verification_ai.confidence_scorer import CHECK_WEIGHTS

        scorer = ConfidenceScorer()
        result = scorer.score_with_breakdown(_result())
        assert {entry.name for entry in result.breakdown} == {name for name, _ in CHECK_WEIGHTS}

    def test_breakdown_score_matches_bare_score(self):
        scorer = ConfidenceScorer()
        cross_result = _result(SubCheckResult(name="facility_address", verdict=True, detail="x"))
        assert scorer.score_with_breakdown(cross_result).score == scorer.score(cross_result)

    def test_breakdown_entry_shows_contribution_for_a_confirmed_check(self):
        scorer = ConfidenceScorer()
        result = scorer.score_with_breakdown(
            _result(SubCheckResult(name="oem_readiness_evidence", verdict=True, detail="x")),
        )
        entry = next(e for e in result.breakdown if e.name == "oem_readiness_evidence")
        assert entry.verdict is True
        assert entry.weight == 15
        assert entry.contribution == 15

    def test_breakdown_entry_shows_contribution_for_a_contradicted_check(self):
        scorer = ConfidenceScorer()
        result = scorer.score_with_breakdown(
            _result(SubCheckResult(name="facility_address", verdict=False, detail="x")),
        )
        entry = next(e for e in result.breakdown if e.name == "facility_address")
        assert entry.verdict is False
        assert entry.contribution == -25

    def test_breakdown_entry_shows_zero_contribution_for_no_signal(self):
        scorer = ConfidenceScorer()
        result = scorer.score_with_breakdown(_result())
        entry = next(e for e in result.breakdown if e.name == "manufacturer_assessment")
        assert entry.verdict is None
        assert entry.contribution == 0
