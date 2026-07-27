"""
tests/test_commercial_scoring.py

Tests for verification.commercial_scoring. The tiered-confidence-by-
source logic in assess_market_presence gets the most coverage, since
getting the priority order wrong (e.g. trusting a directory checkbox
over confirmed customs data) would silently produce a less reliable
answer while looking identical from the caller's side.
"""

from __future__ import annotations

import pytest

from verification.commercial_scoring import (
    SOURCE_DIRECTORY_LISTING,
    SOURCE_OWN_WEBSITE,
    SOURCE_SUPPLIER_LOCATION,
    SOURCE_TRADE_DATA,
    SOURCE_UNAVAILABLE,
    assess_certification,
    assess_company_scale,
    assess_export_maturity,
    assess_incoterm_capability,
    assess_market_presence,
    assess_oem_readiness,
    assess_oem_supplier_status,
)


def _capability(canonical_term, relationship="in_house", confidence=0.9, evidence="evidence text"):
    return {
        "canonical_term": canonical_term, "relationship": relationship,
        "confidence": confidence, "evidence": evidence, "source_url": "https://example.com",
    }


class TestAssessIncotermCapability:

    def test_found_capability_returns_true_with_evidence(self):
        result = assess_incoterm_capability([_capability("ddp shipping")], "DDP")
        assert result.value is True
        assert result.source == SOURCE_OWN_WEBSITE
        assert result.confidence == 0.9

    def test_missing_capability_returns_unknown_not_false(self):
        result = assess_incoterm_capability([], "DDP")
        assert result.value is None
        assert result.confidence == 0.0
        assert result.source == SOURCE_UNAVAILABLE

    def test_unrecognised_term_reports_unavailable_not_a_silent_miss(self):
        result = assess_incoterm_capability([_capability("ddp shipping")], "not a real incoterm")
        assert result.value is None
        assert "not a recognised logistics term" in result.reasoning

    def test_subcontracted_relationship_is_reflected_in_reasoning(self):
        result = assess_incoterm_capability(
            [_capability("customs expertise", relationship="subcontracted")], "customs brokerage"
        )
        assert result.value is True
        assert "logistics partner" in result.reasoning

    def test_best_of_multiple_matches_is_used(self):
        caps = [
            _capability("ddp shipping", confidence=0.4),
            _capability("ddp shipping", confidence=0.9),
        ]
        result = assess_incoterm_capability(caps, "DDP")
        assert result.confidence == 0.9


class TestAssessMarketPresenceTieredConfidence:
    """The core property: confirmed trade data > own-website assertion
    > self-reported directory checkbox, and only the strongest
    available tier is ever returned."""

    def test_confirmed_shipments_wins_over_everything_else(self):
        supplier = {"confirmed_shipments_uk": 3, "exports_to_uk": True}
        caps = [_capability("serves uk market", relationship="asserted", confidence=0.7)]
        result = assess_market_presence(supplier, caps, "uk")
        assert result.source == SOURCE_TRADE_DATA
        assert result.confidence == 0.95

    def test_own_website_assertion_wins_over_directory_flag_when_no_trade_data(self):
        supplier = {"confirmed_shipments_uk": 0, "exports_to_uk": True}
        caps = [_capability("serves uk market", relationship="asserted", confidence=0.7)]
        result = assess_market_presence(supplier, caps, "uk")
        assert result.source == SOURCE_OWN_WEBSITE
        assert result.confidence == pytest.approx(0.7 * 0.9)

    def test_directory_flag_used_only_when_nothing_stronger_exists(self):
        supplier = {"confirmed_shipments_uk": 0, "exports_to_uk": True}
        result = assess_market_presence(supplier, [], "uk")
        assert result.source == SOURCE_DIRECTORY_LISTING
        assert result.confidence == 0.5

    def test_no_evidence_anywhere_is_unknown(self):
        result = assess_market_presence({}, [], "uk")
        assert result.value is None
        assert result.confidence == 0.0
        assert result.source == SOURCE_UNAVAILABLE

    def test_australia_has_no_directory_or_trade_data_tier(self):
        """Only UK/EU/US have exports_to_*/confirmed_shipments_*
        columns -- Australia can only ever come from the own-website
        tier, and must not crash trying to read a nonexistent column."""
        result = assess_market_presence({}, [], "australia")
        assert result.value is None
        assert result.source == SOURCE_UNAVAILABLE

        caps = [_capability("serves australia market", relationship="asserted", confidence=0.6)]
        result2 = assess_market_presence({}, caps, "australia")
        assert result2.value is True
        assert result2.source == SOURCE_OWN_WEBSITE

    def test_zero_confirmed_shipments_is_treated_as_no_trade_data(self):
        supplier = {"confirmed_shipments_uk": 0}
        caps = [_capability("serves uk market", relationship="asserted", confidence=0.8)]
        result = assess_market_presence(supplier, caps, "uk")
        assert result.source == SOURCE_OWN_WEBSITE  # not trade_data, since count is 0


class TestAssessMarketPresenceSupplierLocation:
    """A supplier physically located in the destination market is
    itself strong evidence of at least domestic presence -- found and
    fixed after an end-to-end proof showed a UK-based supplier
    incorrectly returning 'unavailable' for a UK buyer profile."""

    def test_supplier_located_in_the_destination_market_is_detected(self):
        supplier = {"country": "United Kingdom"}
        result = assess_market_presence(supplier, [], "uk")
        assert result.value is True
        assert result.source == SOURCE_SUPPLIER_LOCATION
        assert result.confidence == 0.85

    def test_ranks_below_confirmed_trade_data(self):
        supplier = {"country": "United Kingdom", "confirmed_shipments_uk": 3}
        result = assess_market_presence(supplier, [], "uk")
        assert result.source == SOURCE_TRADE_DATA  # trade data still wins even though location also matches

    def test_ranks_above_own_website_assertion(self):
        supplier = {"country": "United Kingdom"}
        caps = [_capability("serves uk market", relationship="asserted", confidence=0.99)]
        result = assess_market_presence(supplier, caps, "uk")
        assert result.source == SOURCE_SUPPLIER_LOCATION  # location wins even over a highly-confident website claim

    def test_ranks_above_directory_flag(self):
        supplier = {"country": "United Kingdom", "exports_to_uk": True}
        result = assess_market_presence(supplier, [], "uk")
        assert result.source == SOURCE_SUPPLIER_LOCATION

    def test_supplier_in_a_different_market_does_not_falsely_match(self):
        supplier = {"country": "China"}
        result = assess_market_presence(supplier, [], "uk")
        assert result.value is None
        assert result.source == SOURCE_UNAVAILABLE

    def test_eu_member_state_matches_the_eu_market_key(self):
        supplier = {"country": "Germany"}
        result = assess_market_presence(supplier, [], "eu")
        assert result.value is True
        assert result.source == SOURCE_SUPPLIER_LOCATION

    def test_unrecognised_country_string_does_not_crash(self):
        supplier = {"country": "Some Made Up Place"}
        result = assess_market_presence(supplier, [], "uk")
        assert result.value is None


class TestAssessCertification:

    def test_found_certification_returns_evidence(self):
        result = assess_certification([_capability("iso 9001")], "ISO9001")
        assert result.value is True
        assert result.source == SOURCE_OWN_WEBSITE

    def test_missing_certification_is_unknown(self):
        result = assess_certification([], "ISO 9001")
        assert result.value is None

    def test_unrecognised_certification_reports_unavailable(self):
        result = assess_certification([_capability("iso 9001")], "not a real cert")
        assert result.value is None
        assert "not a recognised standard" in result.reasoning

    def test_ce_marking_is_recognised(self):
        result = assess_certification([_capability("ce marking")], "CE marked")
        assert result.value is True


class TestAssessOemReadiness:

    def test_ppap_found(self):
        result = assess_oem_readiness([_capability("ppap capability")], "PPAP")
        assert result.value is True

    def test_missing_is_unknown(self):
        result = assess_oem_readiness([], "PPAP")
        assert result.value is None


class TestAssessOemSupplierStatus:

    def test_found(self):
        result = assess_oem_supplier_status([_capability("oem supplier", relationship="asserted")])
        assert result.value is True

    def test_missing_is_unknown_not_false(self):
        result = assess_oem_supplier_status([])
        assert result.value is None


class TestAssessCompanyScale:

    def test_all_fields_present(self):
        supplier = {
            "registered_capital_rmb": 2_000_000.0, "year_established": 2010,
            "employee_count": "50-100", "factory_size_sqm": 5000, "annual_revenue_usd": "$1M-$5M",
        }
        results = assess_company_scale(supplier)
        by_name = {r.factor_name: r for r in results}
        assert by_name["registered_capital_rmb"].value == 2_000_000.0
        assert by_name["company_age_years"].value > 0
        assert by_name["employee_count"].value == "50-100"
        assert by_name["annual_revenue_usd"].value == "$1M-$5M"

    def test_no_fields_present_returns_all_unknown(self):
        results = assess_company_scale({})
        assert all(r.value is None for r in results)
        assert all(r.confidence == 0.0 for r in results)

    def test_never_fabricates_a_numeric_size_tier_from_text_ranges(self):
        """The specific discipline this function is built around: no
        attempt to parse '50-100 employees' into a single number."""
        supplier = {"employee_count": "50-100 employees"}
        results = assess_company_scale(supplier)
        employee_result = next(r for r in results if r.factor_name == "employee_count")
        assert employee_result.value == "50-100 employees"  # unchanged, not parsed into a number


class TestAssessExportMaturity:

    def test_counts_markets_with_any_evidence(self):
        supplier = {"confirmed_shipments_uk": 2, "exports_to_eu": True, "confirmed_shipments_us": 0}
        result = assess_export_maturity(supplier)
        assert result.value == 2
        assert "UK" in result.evidence
        assert "EU" in result.evidence

    def test_no_evidence_anywhere_is_unknown(self):
        result = assess_export_maturity({})
        assert result.value is None
        assert result.confidence == 0.0
