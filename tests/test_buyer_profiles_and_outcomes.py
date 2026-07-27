"""
tests/test_buyer_profiles_and_outcomes.py

Tests for storage.repository's buyer_profile_* and
procurement_outcome_* methods.
"""

from __future__ import annotations

import sqlite3

import pytest

from storage.database import initialise_schema
from storage.repository import SupplierRepository


@pytest.fixture()
def repo(tmp_path):
    db_path = tmp_path / "test.db"
    initialise_schema(db_path)
    return SupplierRepository(db_path=db_path)


class TestCreateBuyerProfile:

    def test_minimal_profile_is_created(self, repo):
        profile_id = repo.create_buyer_profile(name="Basic Profile")
        profile = repo.get_buyer_profile(profile_id)
        assert profile["name"] == "Basic Profile"
        assert profile["required_capabilities"] == []

    def test_full_profile_round_trips_correctly(self, repo):
        profile_id = repo.create_buyer_profile(
            name="UK OEM Buyer", destination_country="United Kingdom",
            required_capabilities=["iso 9001", "iatf 16949"], preferred_incoterm="ddp shipping",
            preferred_payment_terms_days=60, min_company_size="medium+", target_market="oem",
            min_export_experience_years=5, manufacturers_only=True,
        )
        profile = repo.get_buyer_profile(profile_id)
        assert profile["destination_country"] == "United Kingdom"
        assert profile["required_capabilities"] == ["iso 9001", "iatf 16949"]
        assert profile["preferred_incoterm"] == "ddp shipping"
        assert profile["preferred_payment_terms_days"] == 60
        assert profile["min_company_size"] == "medium+"
        assert profile["target_market"] == "oem"
        assert profile["min_export_experience_years"] == 5

    def test_duplicate_name_raises_rather_than_silently_overwriting(self, repo):
        repo.create_buyer_profile(name="Dup Profile")
        with pytest.raises(sqlite3.IntegrityError):
            repo.create_buyer_profile(name="Dup Profile")

    def test_manufacturers_only_defaults_to_true(self, repo):
        profile_id = repo.create_buyer_profile(name="Default Test")
        profile = repo.get_buyer_profile(profile_id)
        assert profile["manufacturers_only"] == 1


class TestGetAndListBuyerProfiles:

    def test_get_by_id(self, repo):
        profile_id = repo.create_buyer_profile(name="Test Profile")
        assert repo.get_buyer_profile(profile_id)["name"] == "Test Profile"

    def test_get_nonexistent_id_returns_none(self, repo):
        assert repo.get_buyer_profile(999999) is None

    def test_get_by_name(self, repo):
        repo.create_buyer_profile(name="Named Profile", destination_country="China")
        profile = repo.get_buyer_profile_by_name("Named Profile")
        assert profile["destination_country"] == "China"

    def test_get_by_nonexistent_name_returns_none(self, repo):
        assert repo.get_buyer_profile_by_name("Does Not Exist") is None

    def test_list_returns_all_profiles_alphabetically(self, repo):
        repo.create_buyer_profile(name="Zebra Profile")
        repo.create_buyer_profile(name="Alpha Profile")
        profiles = repo.list_buyer_profiles()
        assert [p["name"] for p in profiles] == ["Alpha Profile", "Zebra Profile"]

    def test_list_on_empty_table_returns_empty_list(self, repo):
        assert repo.list_buyer_profiles() == []


class TestDeleteBuyerProfile:

    def test_delete_removes_the_profile(self, repo):
        profile_id = repo.create_buyer_profile(name="To Delete")
        repo.delete_buyer_profile(profile_id)
        assert repo.get_buyer_profile(profile_id) is None

    def test_deleting_a_profile_referenced_by_an_outcome_sets_it_null_not_orphaned(self, repo):
        """ON DELETE SET NULL, matching the schema's own foreign key
        clause -- an outcome must survive its profile being deleted,
        just losing the link, since the outcome itself (did this
        supplier accept these terms) remains a real historical fact."""
        profile_id = repo.create_buyer_profile(name="Temp Profile")
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        repo.record_procurement_outcome(supplier_id=supplier_id, outcome="quoted", buyer_profile_id=profile_id)

        repo.delete_buyer_profile(profile_id)

        outcomes = repo.get_procurement_outcomes(supplier_id=supplier_id)
        assert len(outcomes) == 1
        assert outcomes[0]["buyer_profile_id"] is None


class TestProcurementOutcomes:

    def test_outcome_is_recorded_and_retrievable(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        repo.record_procurement_outcome(supplier_id=supplier_id, outcome="nda_signed")
        outcomes = repo.get_procurement_outcomes(supplier_id=supplier_id)
        assert len(outcomes) == 1
        assert outcomes[0]["outcome"] == "nda_signed"

    def test_outcome_type_is_not_constrained_to_a_fixed_list(self, repo):
        """The specific discipline this schema is built around: an
        arbitrary, previously-unseen outcome string must be accepted,
        not rejected by a CHECK constraint."""
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        repo.record_procurement_outcome(supplier_id=supplier_id, outcome="some_entirely_new_outcome_type")
        outcomes = repo.get_procurement_outcomes(supplier_id=supplier_id)
        assert outcomes[0]["outcome"] == "some_entirely_new_outcome_type"

    def test_multiple_outcomes_for_one_supplier_are_all_retained(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        repo.record_procurement_outcome(supplier_id=supplier_id, outcome="nda_signed")
        repo.record_procurement_outcome(supplier_id=supplier_id, outcome="rfq_submitted")
        repo.record_procurement_outcome(supplier_id=supplier_id, outcome="quoted")
        outcomes = repo.get_procurement_outcomes(supplier_id=supplier_id)
        assert len(outcomes) == 3

    def test_most_recent_outcome_is_returned_first(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        repo.record_procurement_outcome(supplier_id=supplier_id, outcome="nda_signed")
        repo.record_procurement_outcome(supplier_id=supplier_id, outcome="quality_approved")
        outcomes = repo.get_procurement_outcomes(supplier_id=supplier_id)
        assert outcomes[0]["outcome"] == "quality_approved"

    def test_filter_by_buyer_profile(self, repo):
        profile_id = repo.create_buyer_profile(name="Filter Test Profile")
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        repo.record_procurement_outcome(supplier_id=supplier_id, outcome="quoted", buyer_profile_id=profile_id)
        repo.record_procurement_outcome(supplier_id=supplier_id, outcome="nda_signed")  # no profile

        filtered = repo.get_procurement_outcomes(buyer_profile_id=profile_id)
        assert len(filtered) == 1
        assert filtered[0]["outcome"] == "quoted"

    def test_notes_are_preserved(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        repo.record_procurement_outcome(
            supplier_id=supplier_id, outcome="supplier_rejected", notes="Lead time too long for our needs"
        )
        outcomes = repo.get_procurement_outcomes(supplier_id=supplier_id)
        assert outcomes[0]["notes"] == "Lead time too long for our needs"

    def test_deleting_the_supplier_cascades_to_its_outcomes(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        repo.record_procurement_outcome(supplier_id=supplier_id, outcome="nda_signed")

        from storage.database import connection_scope
        with connection_scope(repo.db_path) as conn:
            conn.execute("DELETE FROM suppliers WHERE id = ?", (supplier_id,))

        outcomes = repo.get_procurement_outcomes(supplier_id=supplier_id)
        assert outcomes == []

    def test_no_filters_returns_everything_up_to_limit(self, repo):
        s1 = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        s2 = repo.create_golden_record({"canonical_name": "Beta", "domain": "beta.example.com"})
        repo.record_procurement_outcome(supplier_id=s1, outcome="nda_signed")
        repo.record_procurement_outcome(supplier_id=s2, outcome="quoted")
        assert len(repo.get_procurement_outcomes()) == 2
