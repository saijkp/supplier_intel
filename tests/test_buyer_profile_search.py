"""
tests/test_buyer_profile_search.py

Tests for pipeline.buyer_profile_search. The property that matters
most: required fields filter and exclude, preferred fields score and
rank without ever excluding -- getting this backwards would either
wrongly hide good suppliers or wrongly include unqualified ones.
"""

from __future__ import annotations

import pytest

from pipeline.buyer_profile_search import search_suppliers_for_buyer_profile
from storage.database import initialise_schema
from storage.repository import SupplierRepository


@pytest.fixture()
def repo(tmp_path):
    db_path = tmp_path / "test.db"
    initialise_schema(db_path)
    return SupplierRepository(db_path=db_path)


def _make_supplier(repo, **overrides):
    name = overrides.get("canonical_name", "Acme Co")
    data = {
        "canonical_name": name, "country": "China",
        "domain": overrides.pop("domain", None) or f"{name.lower().replace(' ', '-')}.example.com",
        "product_keywords": ["wheel hub"],
    }
    data.update(overrides)
    supplier_id = repo.create_golden_record(data)
    followups = {k: v for k, v in overrides.items() if k in ("is_manufacturer",)}
    if followups:
        repo.update_supplier_fields(supplier_id, followups)
    if "composite_score" in overrides:
        repo.update_scores(supplier_id, {"composite_score": overrides["composite_score"]})
    return supplier_id


def _add_capability(repo, supplier_id, canonical_term, category, relationship="in_house", confidence=0.9):
    repo.add_capability_finding(supplier_id, {
        "reported_term": canonical_term, "canonical_term": canonical_term, "category": category,
        "relationship": relationship, "confidence": confidence,
        "evidence": f"evidence for {canonical_term}", "source_url": "https://example.com",
    })


class TestRequiredFieldsFilterAndExclude:

    def test_required_capability_excludes_suppliers_without_it(self, repo):
        with_cert = _make_supplier(repo, canonical_name="Certified Co")
        _add_capability(repo, with_cert, "iso 9001", "standard")
        without_cert = _make_supplier(repo, canonical_name="Uncertified Co")

        profile = {"required_capabilities": ["iso 9001"], "manufacturers_only": False}
        results = search_suppliers_for_buyer_profile(repo, profile)
        ids = {r["id"] for r in results}
        assert with_cert in ids
        assert without_cert not in ids

    def test_destination_country_excludes_wrong_country(self, repo):
        uk_supplier = _make_supplier(repo, canonical_name="UK Co", country="United Kingdom")
        cn_supplier = _make_supplier(repo, canonical_name="China Co", country="China")

        profile = {"destination_country": "United Kingdom", "manufacturers_only": False}
        results = search_suppliers_for_buyer_profile(repo, profile)
        ids = {r["id"] for r in results}
        assert uk_supplier in ids
        assert cn_supplier not in ids

    def test_manufacturers_only_excludes_traders(self, repo):
        manufacturer = _make_supplier(repo, canonical_name="Real Factory", is_manufacturer=True)
        trader = _make_supplier(repo, canonical_name="Trading Co", is_manufacturer=False)

        profile = {"manufacturers_only": True}
        results = search_suppliers_for_buyer_profile(repo, profile)
        ids = {r["id"] for r in results}
        assert manufacturer in ids
        assert trader not in ids


class TestPreferredFieldsScoreButNeverExclude:

    def test_supplier_with_no_incoterm_evidence_still_appears(self, repo):
        supplier = _make_supplier(repo, canonical_name="No Logistics Info Co")
        profile = {"preferred_incoterm": "ddp shipping", "manufacturers_only": False}
        results = search_suppliers_for_buyer_profile(repo, profile)
        assert any(r["id"] == supplier for r in results)

    def test_supplier_with_matching_incoterm_gets_a_higher_commercial_score(self, repo):
        with_ddp = _make_supplier(repo, canonical_name="DDP Co")
        _add_capability(repo, with_ddp, "ddp shipping", "logistics")
        without_ddp = _make_supplier(repo, canonical_name="No DDP Co")

        profile = {"preferred_incoterm": "ddp shipping", "manufacturers_only": False}
        results = search_suppliers_for_buyer_profile(repo, profile)
        by_id = {r["id"]: r for r in results}
        assert by_id[with_ddp]["commercial_compatibility_score"] is not None
        assert by_id[without_ddp]["commercial_compatibility_score"] is None

    def test_missing_preferred_data_is_none_not_zero(self, repo):
        """The specific discipline this module is built around: no
        available commercial evidence must report None, never a
        fabricated 0 that would look like confirmed poor fit."""
        supplier = _make_supplier(repo, canonical_name="Unknown Co")
        profile = {"preferred_incoterm": "ddp shipping", "manufacturers_only": False}
        results = search_suppliers_for_buyer_profile(repo, profile)
        result = next(r for r in results if r["id"] == supplier)
        assert result["commercial_compatibility_score"] is None


class TestDomesticPresenceIntegration:

    def test_uk_based_supplier_gets_market_presence_credit_for_uk_profile(self, repo):
        uk_supplier = _make_supplier(repo, canonical_name="UK Factory", country="United Kingdom")
        profile = {"destination_country": "United Kingdom", "manufacturers_only": False}
        results = search_suppliers_for_buyer_profile(repo, profile)
        result = next(r for r in results if r["id"] == uk_supplier)
        market_factors = [
            f for f in result["commercial_compatibility"]["factors"]
            if f["factor_name"] == "market_presence:uk"
        ]
        assert len(market_factors) == 1
        assert market_factors[0]["value"] is True
        assert market_factors[0]["source"] == "supplier_location"


class TestTwoScoresKeptSeparate:

    def test_composite_score_and_commercial_score_are_both_present_and_distinct(self, repo):
        supplier = _make_supplier(repo, canonical_name="Scored Co", composite_score=75)
        _add_capability(repo, supplier, "ddp shipping", "logistics")
        profile = {"preferred_incoterm": "ddp shipping", "manufacturers_only": False}
        results = search_suppliers_for_buyer_profile(repo, profile)
        result = results[0]
        assert result["composite_score"] == 75
        assert "commercial_compatibility_score" in result
        assert result["composite_score"] != result["commercial_compatibility_score"]


class TestPaymentTermsIntegration:

    def test_preferred_payment_terms_triggers_a_probability_estimate(self, repo):
        supplier = _make_supplier(repo, canonical_name="Payment Test Co", is_manufacturer=True)
        profile = {"preferred_payment_terms_days": 60, "manufacturers_only": False}
        results = search_suppliers_for_buyer_profile(repo, profile)
        result = next(r for r in results if r["id"] == supplier)
        payment_info = result["commercial_compatibility"]["payment_terms_probability"]
        assert payment_info is not None
        assert payment_info["outcome"] == "60-day payment terms"

    def test_no_preferred_payment_terms_means_no_estimate_computed(self, repo):
        supplier = _make_supplier(repo, canonical_name="No Payment Pref Co")
        profile = {"manufacturers_only": False}
        results = search_suppliers_for_buyer_profile(repo, profile)
        result = next(r for r in results if r["id"] == supplier)
        assert result["commercial_compatibility"]["payment_terms_probability"] is None


class TestOemTargetMarket:

    def test_oem_target_market_checks_oem_supplier_status(self, repo):
        oem_supplier = _make_supplier(repo, canonical_name="OEM Co")
        _add_capability(repo, oem_supplier, "oem supplier", "market_presence", relationship="asserted")
        profile = {"target_market": "oem", "manufacturers_only": False}
        results = search_suppliers_for_buyer_profile(repo, profile)
        result = next(r for r in results if r["id"] == oem_supplier)
        oem_factors = [f for f in result["commercial_compatibility"]["factors"] if f["factor_name"] == "oem_supplier_status"]
        assert len(oem_factors) == 1
        assert oem_factors[0]["value"] is True

    def test_non_oem_target_market_does_not_check_oem_status(self, repo):
        supplier = _make_supplier(repo, canonical_name="Regular Co")
        profile = {"manufacturers_only": False}  # no target_market at all
        results = search_suppliers_for_buyer_profile(repo, profile)
        result = next(r for r in results if r["id"] == supplier)
        oem_factors = [f for f in result["commercial_compatibility"]["factors"] if f["factor_name"] == "oem_supplier_status"]
        assert oem_factors == []
