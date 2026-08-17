"""
tests/test_uk_company_verification_service.py

Tests for verification/uk_company_verification_service.py against a
real (temp-file) database -- same fixture pattern
tests/test_ai_platform_repository.py already uses -- with a fake
CompaniesHouseClient injected (no real network/API key). Covers the
three-outcome matching discipline (verified/inactive/no_clear_match,
never a binary accept/reject), provenance writes, and the batch
skip-already-checked/idempotency behaviour.
"""

from __future__ import annotations

import pytest

from storage.database import initialise_schema
from verification.companies_house_client import CompanyProfile, CompanySearchMatch
from verification.uk_company_verification_service import UKCompanyVerificationService


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    initialise_schema(path)
    return path


@pytest.fixture()
def repo(db_path):
    from storage.repository import SupplierRepository
    return SupplierRepository(db_path=db_path)


class FakeCompaniesHouseClient:
    def __init__(self, search_results=None, profiles=None, raise_on_search=None):
        self.search_results = search_results or {}
        self.profiles = profiles or {}
        self._raise_on_search = raise_on_search
        self.search_calls = []
        self.profile_calls = []

    def search_companies(self, name, max_results=5):
        self.search_calls.append(name)
        if self._raise_on_search:
            raise self._raise_on_search
        return self.search_results.get(name, [])

    def get_company_profile(self, company_number):
        self.profile_calls.append(company_number)
        return self.profiles.get(company_number)


def _make_service(repo, client):
    return UKCompanyVerificationService(repo=repo, companies_house_client=client)


ACTIVE_PROFILE = CompanyProfile(
    company_number="01234567", company_name="ACME MATERIAL HANDLING LTD",
    company_status="active", date_of_creation="2005-03-14", sic_codes=["28220"],
    registered_office_address="1 Main St, London, EC1A 1AA, United Kingdom",
    source_url="https://find-and-update.company-information.service.gov.uk/company/01234567",
)

DISSOLVED_PROFILE = CompanyProfile(
    company_number="07654321", company_name="ACME MATERIAL HANDLING LTD",
    company_status="dissolved", date_of_creation="1998-01-01", sic_codes=["28220"],
    registered_office_address="2 Old St, Leeds",
    source_url="https://find-and-update.company-information.service.gov.uk/company/07654321",
)


class TestVerifiedOutcome:

    def test_high_confidence_active_match_is_verified(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Material Handling Ltd", "domain": "acme.com"})
        client = FakeCompaniesHouseClient(
            search_results={"Acme Material Handling Ltd": [
                CompanySearchMatch(company_number="01234567", title="ACME MATERIAL HANDLING LTD", company_status="active"),
            ]},
            profiles={"01234567": ACTIVE_PROFILE},
        )
        service = _make_service(repo, client)

        outcome = service.verify_uk_company(supplier_id)

        assert outcome["match_status"] == "verified"
        supplier = repo.get_supplier(supplier_id)
        assert supplier["companies_house_number"] == "01234567"
        assert supplier["companies_house_status"] == "active"
        assert supplier["companies_house_registered_office"] == "1 Main St, London, EC1A 1AA, United Kingdom"
        assert supplier["companies_house_incorporated_at"] == "2005-03-14"
        assert supplier["companies_house_sic_codes"] == ["28220"]
        assert supplier["companies_house_match_status"] == "verified"
        assert supplier["companies_house_checked_at"] is not None

    def test_provenance_written_for_verifiable_facts(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Material Handling Ltd", "domain": "acme.com"})
        client = FakeCompaniesHouseClient(
            search_results={"Acme Material Handling Ltd": [
                CompanySearchMatch(company_number="01234567", title="ACME MATERIAL HANDLING LTD", company_status="active"),
            ]},
            profiles={"01234567": ACTIVE_PROFILE},
        )
        service = _make_service(repo, client)

        service.verify_uk_company(supplier_id)

        entries = repo.get_field_provenance(supplier_id)
        by_field = {e["field_name"]: e for e in entries}
        assert "companies_house_status" in by_field
        assert by_field["companies_house_status"]["source_tier"] == "other"
        assert by_field["companies_house_status"]["claim_type"] == "verifiable_fact"
        assert by_field["companies_house_status"]["extraction_method"] == "companies_house_api"
        assert by_field["companies_house_status"]["source_url"] == ACTIVE_PROFILE.source_url
        assert "companies_house_registered_office" in by_field
        assert "companies_house_incorporated_at" in by_field
        assert "companies_house_sic_codes" in by_field


class TestInactiveOutcome:

    def test_high_confidence_dissolved_match_is_inactive_not_rejected(self, repo):
        """A dissolved/inactive match is a real flag, surfaced -- never
        silently dropped, and never conflated with no_clear_match."""
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Material Handling Ltd", "domain": "acme.com"})
        client = FakeCompaniesHouseClient(
            search_results={"Acme Material Handling Ltd": [
                CompanySearchMatch(company_number="07654321", title="ACME MATERIAL HANDLING LTD", company_status="dissolved"),
            ]},
            profiles={"07654321": DISSOLVED_PROFILE},
        )
        service = _make_service(repo, client)

        outcome = service.verify_uk_company(supplier_id)

        assert outcome["match_status"] == "inactive"
        supplier = repo.get_supplier(supplier_id)
        assert supplier["companies_house_match_status"] == "inactive"
        assert supplier["companies_house_status"] == "dissolved"
        # The registration facts are still recorded -- this is evidence,
        # not a reason to withhold what was actually found.
        assert supplier["companies_house_number"] == "07654321"


class TestNoClearMatchOutcome:

    def test_no_search_results_is_no_clear_match_not_a_rejection(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Totally Obscure Trading Name Ltd", "domain": "obscure.com"})
        client = FakeCompaniesHouseClient(search_results={})
        service = _make_service(repo, client)

        outcome = service.verify_uk_company(supplier_id)

        assert outcome["match_status"] == "no_clear_match"
        supplier = repo.get_supplier(supplier_id)
        assert supplier["companies_house_match_status"] == "no_clear_match"
        # Nothing about this should look like a verdict -- no number,
        # no status, no office address ever got attached to a guess.
        assert supplier["companies_house_number"] is None

    def test_low_confidence_match_is_no_clear_match_with_the_score_recorded(self, repo):
        """A trading name differing from the registered legal name is
        common and not itself suspicious -- see module docstring. This
        must fall to "check manually," not silently pick the best-
        available (but still weak) guess."""
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Handling Co", "domain": "acme.com"})
        client = FakeCompaniesHouseClient(
            search_results={"Acme Handling Co": [
                CompanySearchMatch(company_number="99999999", title="COMPLETELY UNRELATED WIDGETS PLC", company_status="active"),
            ]},
        )
        service = _make_service(repo, client)

        outcome = service.verify_uk_company(supplier_id)

        assert outcome["match_status"] == "no_clear_match"
        assert outcome["confidence"] is not None
        assert outcome["confidence"] < 85

    def test_profile_lookup_failure_after_a_good_name_match_is_no_clear_match(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Material Handling Ltd", "domain": "acme.com"})
        client = FakeCompaniesHouseClient(
            search_results={"Acme Material Handling Ltd": [
                CompanySearchMatch(company_number="01234567", title="ACME MATERIAL HANDLING LTD", company_status="active"),
            ]},
            profiles={},  # profile lookup returns None
        )
        service = _make_service(repo, client)

        outcome = service.verify_uk_company(supplier_id)

        assert outcome["match_status"] == "no_clear_match"

    def test_no_canonical_name_is_no_clear_match_without_ever_searching(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Placeholder", "domain": "acme.com"})
        repo.update_supplier_fields(supplier_id, {"canonical_name": ""})
        client = FakeCompaniesHouseClient()
        service = _make_service(repo, client)

        outcome = service.verify_uk_company(supplier_id)

        assert outcome["match_status"] == "no_clear_match"
        assert client.search_calls == []

    def test_search_exception_is_no_clear_match_not_raised(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Material Handling Ltd", "domain": "acme.com"})
        client = FakeCompaniesHouseClient(raise_on_search=RuntimeError("network down"))
        service = _make_service(repo, client)

        outcome = service.verify_uk_company(supplier_id)

        assert outcome["match_status"] == "no_clear_match"


class TestVerifySingleSupplier:

    def test_unknown_supplier_id_raises(self, repo):
        client = FakeCompaniesHouseClient()
        service = _make_service(repo, client)
        with pytest.raises(ValueError):
            service.verify_uk_company(999999)


class TestVerifyBatch:

    def test_skips_already_checked_suppliers_unless_forced(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Material Handling Ltd", "domain": "acme.com"})
        repo.update_supplier_fields(supplier_id, {"companies_house_checked_at": "2026-01-01T00:00:00+00:00"})
        client = FakeCompaniesHouseClient(
            search_results={"Acme Material Handling Ltd": [
                CompanySearchMatch(company_number="01234567", title="ACME MATERIAL HANDLING LTD", company_status="active"),
            ]},
            profiles={"01234567": ACTIVE_PROFILE},
        )
        service = _make_service(repo, client)

        outcome = service.verify_uk_company_batch([supplier_id])

        assert outcome["attempted"] == 0
        assert client.search_calls == []

    def test_force_re_checks_already_checked_suppliers(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Material Handling Ltd", "domain": "acme.com"})
        repo.update_supplier_fields(supplier_id, {"companies_house_checked_at": "2026-01-01T00:00:00+00:00"})
        client = FakeCompaniesHouseClient(
            search_results={"Acme Material Handling Ltd": [
                CompanySearchMatch(company_number="01234567", title="ACME MATERIAL HANDLING LTD", company_status="active"),
            ]},
            profiles={"01234567": ACTIVE_PROFILE},
        )
        service = _make_service(repo, client)

        outcome = service.verify_uk_company_batch([supplier_id], force=True)

        assert outcome["attempted"] == 1
        assert outcome["verified"] == 1

    def test_batch_totals_across_mixed_outcomes(self, repo):
        verified_id = repo.create_golden_record({"canonical_name": "Acme Material Handling Ltd", "domain": "acme.com"})
        inactive_id = repo.create_golden_record({"canonical_name": "Dissolved Handling Ltd", "domain": "dissolved.com"})
        no_match_id = repo.create_golden_record({"canonical_name": "Totally Obscure Ltd", "domain": "obscure.com"})

        client = FakeCompaniesHouseClient(
            search_results={
                "Acme Material Handling Ltd": [
                    CompanySearchMatch(company_number="01234567", title="ACME MATERIAL HANDLING LTD", company_status="active"),
                ],
                "Dissolved Handling Ltd": [
                    CompanySearchMatch(company_number="07654321", title="DISSOLVED HANDLING LTD", company_status="dissolved"),
                ],
                "Totally Obscure Ltd": [],
            },
            profiles={
                "01234567": ACTIVE_PROFILE,
                "07654321": CompanyProfile(
                    company_number="07654321", company_name="DISSOLVED HANDLING LTD", company_status="dissolved",
                    date_of_creation="1998-01-01", sic_codes=[], registered_office_address=None,
                    source_url="https://find-and-update.company-information.service.gov.uk/company/07654321",
                ),
            },
        )
        service = _make_service(repo, client)

        outcome = service.verify_uk_company_batch([verified_id, inactive_id, no_match_id])

        assert outcome["attempted"] == 3
        assert outcome["verified"] == 1
        assert outcome["inactive"] == 1
        assert outcome["no_clear_match"] == 1
        assert outcome["status"] == "completed"

    def test_unknown_supplier_id_in_batch_is_skipped_not_raised(self, repo):
        client = FakeCompaniesHouseClient()
        service = _make_service(repo, client)
        outcome = service.verify_uk_company_batch([999999])
        assert outcome["attempted"] == 0

    def test_job_max_seconds_of_zero_stops_before_the_first_item(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Material Handling Ltd", "domain": "acme.com"})
        client = FakeCompaniesHouseClient(
            search_results={"Acme Material Handling Ltd": [
                CompanySearchMatch(company_number="01234567", title="ACME MATERIAL HANDLING LTD", company_status="active"),
            ]},
            profiles={"01234567": ACTIVE_PROFILE},
        )
        service = UKCompanyVerificationService(repo=repo, companies_house_client=client, job_max_seconds=0)

        outcome = service.verify_uk_company_batch([supplier_id])

        assert outcome["status"] == "partial"
        assert outcome["attempted"] == 0
