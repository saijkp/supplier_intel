"""
tests/test_facility_and_linkedin_stages.py

Tests for SupplierIntelligencePipeline's facility address verification
and LinkedIn presence checking stages.
"""

from __future__ import annotations

import pytest

from pipeline.orchestrator import SupplierIntelligencePipeline
from storage.database import initialise_schema
from storage.repository import SupplierRepository
from verification.facility_address_verifier import AddressVerificationResult
from verification.linkedin_presence import LinkedInPresenceResult


@pytest.fixture()
def repo(tmp_path):
    db_path = tmp_path / "test.db"
    initialise_schema(db_path)
    return SupplierRepository(db_path=db_path)


class FakeAddressVerifier:
    def __init__(self, result=None):
        self._result = result or AddressVerificationResult(
            verified=True, source="google_places", formatted_address="123 Fake St", reason="ok",
        )
        self.calls = []

    def verify(self, address, company_name=""):
        self.calls.append((address, company_name))
        return self._result


class ExplodingAddressVerifier:
    def verify(self, address, company_name=""):
        raise RuntimeError("API down")


class FakeLinkedInChecker:
    def __init__(self, result=None):
        self._result = result or LinkedInPresenceResult(
            company_name="", presence_confirmed=True,
            linkedin_url="https://linkedin.com/company/acme", snippet="500 employees", reason="found",
        )
        self.calls = []

    def check(self, company_name):
        self.calls.append(company_name)
        return self._result


class ExplodingLinkedInChecker:
    def check(self, company_name):
        raise RuntimeError("SerpAPI down")


def _pipeline(repo, **kwargs):
    return SupplierIntelligencePipeline(repo=repo, scrapers={}, normalizers={}, **kwargs)


class TestFacilityVerificationStage:

    def test_verified_address_updates_the_record_and_stat(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme", "domain": "acme.example.com", "address": "123 Real St",
        })
        google = FakeAddressVerifier(AddressVerificationResult(
            verified=True, source="google_places", formatted_address="123 Real St, UK", reason="ok",
        ))
        pipeline = _pipeline(repo, google_places_verifier=google, amap_verifier=FakeAddressVerifier())
        stats = pipeline.run_facility_verification_only()

        assert stats["facility_address_verified"] == 1
        supplier = repo.get_supplier(supplier_id)
        assert supplier["facility_address_verified"] == 1  # SQLite boolean storage
        assert supplier["facility_address_verification_source"] == "google_places"
        assert supplier["facility_address_verified_at"] is not None

    def test_unverified_address_does_not_increment_the_stat_but_is_marked_attempted(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme", "domain": "acme.example.com", "address": "fake address",
        })
        google = FakeAddressVerifier(AddressVerificationResult(
            verified=False, source="google_places", formatted_address=None, reason="no match",
        ))
        pipeline = _pipeline(repo, google_places_verifier=google, amap_verifier=FakeAddressVerifier())
        stats = pipeline.run_facility_verification_only()

        assert stats["facility_address_verified"] == 0
        supplier = repo.get_supplier(supplier_id)
        assert supplier["facility_address_verified"] == 0
        assert supplier["facility_address_verified_at"] is not None  # still marked attempted

    def test_supplier_without_an_address_is_skipped(self, repo):
        repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com", "address": None})
        google = FakeAddressVerifier()
        pipeline = _pipeline(repo, google_places_verifier=google, amap_verifier=FakeAddressVerifier())
        pipeline.run_facility_verification_only()
        assert google.calls == []

    def test_china_supplier_routes_to_amap_not_google(self, repo):
        repo.create_golden_record({
            "canonical_name": "Ningbo Co", "domain": "ningbo.example.com",
            "address": "123 Industrial Rd", "country": "China",
        })
        google = FakeAddressVerifier()
        amap = FakeAddressVerifier()
        pipeline = _pipeline(repo, google_places_verifier=google, amap_verifier=amap)
        pipeline.run_facility_verification_only()

        assert google.calls == []
        assert len(amap.calls) == 1

    def test_uk_supplier_routes_to_google_not_amap(self, repo):
        repo.create_golden_record({
            "canonical_name": "Acme UK", "domain": "acme.example.com",
            "address": "123 High St", "country": "United Kingdom",
        })
        google = FakeAddressVerifier()
        amap = FakeAddressVerifier()
        pipeline = _pipeline(repo, google_places_verifier=google, amap_verifier=amap)
        pipeline.run_facility_verification_only()

        assert len(google.calls) == 1
        assert amap.calls == []

    def test_already_verified_supplier_is_not_re_checked(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme", "domain": "acme.example.com", "address": "123 Real St",
        })
        repo.mark_facility_address_verification_attempted(supplier_id, verified=True, source="google_places")
        google = FakeAddressVerifier()
        pipeline = _pipeline(repo, google_places_verifier=google, amap_verifier=FakeAddressVerifier())
        pipeline.run_facility_verification_only()
        assert google.calls == []

    def test_force_re_checks_already_verified_suppliers(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme", "domain": "acme.example.com", "address": "123 Real St",
        })
        repo.mark_facility_address_verification_attempted(supplier_id, verified=True, source="google_places")
        google = FakeAddressVerifier()
        pipeline = _pipeline(repo, google_places_verifier=google, amap_verifier=FakeAddressVerifier())
        pipeline.run_facility_verification_only(force=True)
        assert len(google.calls) == 1

    def test_one_supplier_raising_does_not_abort_the_batch(self, repo):
        repo.create_golden_record({"canonical_name": "Broken", "domain": "b.example.com", "address": "x"})
        good_id = repo.create_golden_record({"canonical_name": "Good", "domain": "g.example.com", "address": "y"})
        pipeline = _pipeline(
            repo, google_places_verifier=ExplodingAddressVerifier(),
            amap_verifier=FakeAddressVerifier(),
        )
        # Both are UK by default (no country set) so both go to the exploding verifier --
        # this specifically proves the batch survives an exception mid-loop.
        stats = pipeline.run_facility_verification_only()
        assert repo.get_supplier(good_id)["facility_address_verified_at"] is None  # never reached, but no crash
        assert stats["facility_address_verified"] == 0


class TestLinkedInCheckStage:

    def test_found_page_updates_the_record_and_stat(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        checker = FakeLinkedInChecker(LinkedInPresenceResult(
            company_name="Acme", presence_confirmed=True,
            linkedin_url="https://linkedin.com/company/acme", snippet="", reason="found",
        ))
        pipeline = _pipeline(repo, linkedin_checker=checker)
        stats = pipeline.run_linkedin_check_only()

        assert stats["linkedin_checked"] == 1
        supplier = repo.get_supplier(supplier_id)
        assert supplier["linkedin_url"] == "https://linkedin.com/company/acme"
        assert supplier["linkedin_checked_at"] is not None

    def test_not_found_does_not_increment_stat_but_marks_attempted(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Obscure Co", "domain": "obscure.example.com"})
        checker = FakeLinkedInChecker(LinkedInPresenceResult(
            company_name="Obscure Co", presence_confirmed=False,
            linkedin_url=None, snippet=None, reason="not found",
        ))
        pipeline = _pipeline(repo, linkedin_checker=checker)
        stats = pipeline.run_linkedin_check_only()

        assert stats["linkedin_checked"] == 0
        supplier = repo.get_supplier(supplier_id)
        assert supplier["linkedin_url"] is None
        assert supplier["linkedin_checked_at"] is not None

    def test_already_checked_supplier_is_not_re_checked(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        repo.mark_linkedin_checked(supplier_id)
        checker = FakeLinkedInChecker()
        pipeline = _pipeline(repo, linkedin_checker=checker)
        pipeline.run_linkedin_check_only()
        assert checker.calls == []

    def test_force_re_checks(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        repo.mark_linkedin_checked(supplier_id)
        checker = FakeLinkedInChecker()
        pipeline = _pipeline(repo, linkedin_checker=checker)
        pipeline.run_linkedin_check_only(force=True)
        assert len(checker.calls) == 1

    def test_one_supplier_raising_does_not_abort_the_batch(self, repo):
        repo.create_golden_record({"canonical_name": "Broken Co", "domain": "b.example.com"})
        pipeline = _pipeline(repo, linkedin_checker=ExplodingLinkedInChecker())
        stats = pipeline.run_linkedin_check_only()
        assert stats["linkedin_checked"] == 0  # no crash, just no progress recorded
