"""
tests/test_procurement_recommendation.py

Tests for verification_ai/procurement_recommendation.py -- the
deterministic 7-category RFQ-readiness categoriser. No LLM call
involved; every test constructs evidence directly and asserts the
category (and that the reason string reflects real evidence, never
fabricated).
"""

from __future__ import annotations

from verification_ai.cross_checker import CrossCheckResult, SubCheckResult
from verification_ai.procurement_recommendation import (
    CATEGORY_AVOID,
    CATEGORY_HIGH_RISK,
    CATEGORY_NEEDS_MORE_VERIFICATION,
    CATEGORY_RECOMMENDED_FOR_MASS_PRODUCTION,
    CATEGORY_RECOMMENDED_FOR_PROTOTYPE,
    CATEGORY_RECOMMENDED_FOR_RFQ,
    CATEGORY_REQUIRES_FACTORY_AUDIT,
    VALID_CATEGORIES,
    categorise,
)


def _cross_result(*sub_checks, inconsistencies=None):
    return CrossCheckResult(sub_checks=list(sub_checks), inconsistencies=inconsistencies or [])


def _check(name, verdict):
    return SubCheckResult(name=name, verdict=verdict, detail="x")


class TestAvoidCategory:

    def test_confirmed_trader_is_avoid_regardless_of_score(self):
        result = categorise(95, is_manufacturer=False, cross_check_result=_cross_result())
        assert result.category == CATEGORY_AVOID

    def test_manufacturer_assessment_contradicted_is_avoid_regardless_of_score(self):
        result = categorise(
            95, is_manufacturer=None,
            cross_check_result=_cross_result(_check("manufacturer_assessment", False)),
        )
        assert result.category == CATEGORY_AVOID

    def test_very_low_score_is_avoid(self):
        result = categorise(10, is_manufacturer=None, cross_check_result=_cross_result())
        assert result.category == CATEGORY_AVOID


class TestHighRiskCategory:

    def test_low_score_is_high_risk(self):
        result = categorise(30, is_manufacturer=True, cross_check_result=_cross_result())
        assert result.category == CATEGORY_HIGH_RISK

    def test_contradicted_facility_address_is_high_risk_even_with_a_decent_score(self):
        result = categorise(
            65, is_manufacturer=True,
            cross_check_result=_cross_result(_check("facility_address", False)),
        )
        assert result.category == CATEGORY_HIGH_RISK


class TestNeedsMoreVerificationCategory:

    def test_mid_range_score_with_no_strong_evidence_needs_more_verification(self):
        result = categorise(50, is_manufacturer=None, cross_check_result=_cross_result())
        assert result.category == CATEGORY_NEEDS_MORE_VERIFICATION


class TestRequiresFactoryAuditCategory:

    def test_good_score_but_facility_never_verified(self):
        result = categorise(
            65, is_manufacturer=True,
            cross_check_result=_cross_result(_check("manufacturer_assessment", True)),
        )
        assert result.category == CATEGORY_REQUIRES_FACTORY_AUDIT


class TestRecommendedForRfqCategory:

    def test_good_score_with_verified_facility_but_not_strong_enough_for_mass_production(self):
        result = categorise(
            70, is_manufacturer=True,
            cross_check_result=_cross_result(_check("facility_address", True)),
        )
        assert result.category == CATEGORY_RECOMMENDED_FOR_RFQ


class TestRecommendedForPrototypeCategory:

    def test_strong_score_with_verified_facility_but_no_full_oem_chain(self):
        result = categorise(
            80, is_manufacturer=True,
            cross_check_result=_cross_result(_check("facility_address", True)),
        )
        assert result.category == CATEGORY_RECOMMENDED_FOR_PROTOTYPE


class TestRecommendedForMassProductionCategory:

    def test_strong_score_with_full_evidence_chain(self):
        result = categorise(
            85, is_manufacturer=True,
            cross_check_result=_cross_result(
                _check("facility_address", True),
                _check("certification_consistency", True),
                _check("oem_readiness_evidence", True),
            ),
        )
        assert result.category == CATEGORY_RECOMMENDED_FOR_MASS_PRODUCTION


class TestAllCategoriesAreValid:

    def test_every_reachable_category_is_in_valid_categories(self):
        assert CATEGORY_AVOID in VALID_CATEGORIES
        assert CATEGORY_HIGH_RISK in VALID_CATEGORIES
        assert CATEGORY_NEEDS_MORE_VERIFICATION in VALID_CATEGORIES
        assert CATEGORY_REQUIRES_FACTORY_AUDIT in VALID_CATEGORIES
        assert CATEGORY_RECOMMENDED_FOR_RFQ in VALID_CATEGORIES
        assert CATEGORY_RECOMMENDED_FOR_PROTOTYPE in VALID_CATEGORIES
        assert CATEGORY_RECOMMENDED_FOR_MASS_PRODUCTION in VALID_CATEGORIES
        assert len(VALID_CATEGORIES) == 7

    def test_categorise_always_returns_a_valid_category(self):
        for score in (0, 20, 21, 40, 41, 60, 61, 75, 76, 100):
            result = categorise(score, is_manufacturer=None, cross_check_result=_cross_result())
            assert result.category in VALID_CATEGORIES


class TestReasonIsGroundedInEvidence:

    def test_reason_mentions_confirmed_manufacturer_when_true(self):
        result = categorise(
            85, is_manufacturer=True,
            cross_check_result=_cross_result(
                _check("manufacturer_assessment", True),
                _check("facility_address", True),
                _check("certification_consistency", True),
                _check("oem_readiness_evidence", True),
            ),
        )
        assert "manufacturer" in result.reason.lower()
        assert str(85) in result.reason

    def test_reason_never_fabricates_evidence_not_given(self):
        """No sub-checks at all -- the reason must not claim any
        specific corroboration that was never actually computed."""
        result = categorise(50, is_manufacturer=None, cross_check_result=_cross_result())
        assert "verified facility" not in result.reason.lower()
        assert "certifications corroborated" not in result.reason.lower()

    def test_reason_notes_unverified_facility_for_audit_category(self):
        result = categorise(
            65, is_manufacturer=True,
            cross_check_result=_cross_result(_check("manufacturer_assessment", True)),
        )
        assert "audit" in result.reason.lower()

    def test_avoid_reason_notes_trader_status(self):
        result = categorise(90, is_manufacturer=False, cross_check_result=_cross_result())
        assert "trader" in result.reason.lower() or "avoid" in result.reason.lower() or "against" in result.reason.lower()


class TestNeverRaises:

    def test_missing_cross_check_result_fields_degrade_gracefully(self):
        """Defence in depth -- a malformed/unexpected CrossCheckResult
        must never crash verify(), just fall back to a safe default
        category."""
        result = categorise(70, is_manufacturer=True, cross_check_result=CrossCheckResult())
        assert result.category in VALID_CATEGORIES
