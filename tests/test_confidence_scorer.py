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
        result = _result(SubCheckResult(name="phone_format", verdict=False, detail="x"))
        assert scorer.score(result) == 50 - 10  # phone_format weight

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
