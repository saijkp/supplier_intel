"""
tests/test_supplier_correction.py

Tests for batch/supplier_correction.py's SupplierCorrectionService --
built to correct two real false matches confirmed live in production
(Ashpock -> shpock.com, IK Eng Ltd -> easydigitalfiling.com). Uses
fakes for the website finder and collection service -- no real
network/API key, same DI-for-testability pattern as every other
service in this codebase.
"""

from __future__ import annotations

import pytest

from batch.supplier_correction import SupplierCorrectionService
from scrapers.company_website_finder import WebsiteFindingResult
from storage.database import initialise_schema
from storage.repository import SupplierRepository


class FakeWebsiteFinder:
    def __init__(self, result=None):
        self._result = result
        self.calls = []

    def find_website(self, company_name, country=None):
        self.calls.append((company_name, country))
        return self._result


class FakeCollectionService:
    def __init__(self, outcome=None):
        self._outcome = outcome or {"status": "success", "pages_visited": 1}
        self.calls = []

    def collect(self, supplier_id, source_url=None):
        self.calls.append((supplier_id, source_url))
        return self._outcome


@pytest.fixture()
def repo(tmp_path):
    db_path = tmp_path / "test.db"
    initialise_schema(db_path)
    return SupplierRepository(db_path=db_path)


class TestCorrectDomain:

    def test_clears_resolves_and_recollects_on_a_validated_match(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Ashpock", "domain": "shpock.com"})
        finder = FakeWebsiteFinder(WebsiteFindingResult(
            company_name="Ashpock", domain="aspoeck.com", validated=True,
            candidate_url="https://aspoeck.com", name_match_score=95.0,
            reason="company name matched candidate site text (score=95)",
        ))
        collection = FakeCollectionService(outcome={"status": "success", "pages_visited": 3})
        service = SupplierCorrectionService(repo=repo, website_finder=finder, collection_service=collection)

        result = service.correct_domain(supplier_id, reason="false match: shpock.com is an unrelated app")

        assert result["status"] == "resolved"
        assert result["old_domain"] == "shpock.com"
        assert result["new_domain"] == "aspoeck.com"
        assert result["cleared"] is True
        assert result["collection_status"] == "success"
        assert result["pages_visited"] == 3

        assert repo.get_supplier(supplier_id)["domain"] == "aspoeck.com"
        assert collection.calls == [(supplier_id, "aspoeck.com")]

        log = repo.get_supplier_change_log(supplier_id)
        assert len(log) == 2  # the clear, then the resolved write
        assert log[0]["field_name"] == "domain" and log[0]["new_value"] == "aspoeck.com"
        assert log[1]["old_value"] == "shpock.com" and log[1]["new_value"] is None

    def test_leaves_domain_cleared_when_nothing_validates(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "IK Eng Ltd", "domain": "easydigitalfiling.com"})
        finder = FakeWebsiteFinder(WebsiteFindingResult(
            company_name="IK Eng Ltd", domain=None, validated=False,
            candidate_url=None, name_match_score=None,
            reason="no non-platform, non-directory result found",
        ))
        collection = FakeCollectionService()
        service = SupplierCorrectionService(repo=repo, website_finder=finder, collection_service=collection)

        result = service.correct_domain(supplier_id)

        assert result["status"] == "needs_url"
        assert result["old_domain"] == "easydigitalfiling.com"
        assert result["new_domain"] is None
        assert repo.get_supplier(supplier_id)["domain"] is None
        assert collection.calls == []  # never re-collects without a validated domain

    def test_no_op_clear_when_domain_already_empty(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Some Co"})
        finder = FakeWebsiteFinder(WebsiteFindingResult(
            company_name="Some Co", domain=None, validated=False,
            candidate_url=None, name_match_score=None, reason="no result found",
        ))
        service = SupplierCorrectionService(repo=repo, website_finder=finder, collection_service=FakeCollectionService())

        result = service.correct_domain(supplier_id)

        assert result["cleared"] is False
        assert repo.get_supplier_change_log(supplier_id) == []

    def test_raises_on_missing_supplier(self, repo):
        service = SupplierCorrectionService(
            repo=repo, website_finder=FakeWebsiteFinder(), collection_service=FakeCollectionService(),
        )
        with pytest.raises(ValueError):
            service.correct_domain(999999)

    def test_default_clear_reason_names_the_wrong_value(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Ashpock", "domain": "shpock.com"})
        finder = FakeWebsiteFinder(WebsiteFindingResult(
            company_name="Ashpock", domain=None, validated=False,
            candidate_url=None, name_match_score=None, reason="no result found",
        ))
        service = SupplierCorrectionService(repo=repo, website_finder=finder, collection_service=FakeCollectionService())

        service.correct_domain(supplier_id)

        log = repo.get_supplier_change_log(supplier_id)
        assert "shpock.com" in log[0]["change_reason"]


class TestSetConfirmedDomain:
    """Real case: "Ashpock" was itself a misspelling of "Aspock"/
    "Aspoeck" -- correct_domain's search-based re-resolution kept
    failing because it was searching the WRONG name, not because the
    domain-matching logic was broken. set_confirmed_domain writes an
    already-human-verified answer directly, no search."""

    def test_sets_domain_and_name_directly_no_search(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Ashpock", "domain": "shpock.com"})
        finder = FakeWebsiteFinder()  # never called -- no search in this path
        collection = FakeCollectionService(outcome={"status": "success", "pages_visited": 2})
        service = SupplierCorrectionService(repo=repo, website_finder=finder, collection_service=collection)

        result = service.set_confirmed_domain(
            supplier_id, "aspoeck.com", canonical_name="Aspoeck Systems",
            reason="original name was a misspelling",
        )

        assert result["status"] == "set"
        assert result["old_domain"] == "shpock.com"
        assert result["new_domain"] == "aspoeck.com"
        assert result["old_canonical_name"] == "Ashpock"
        assert result["canonical_name"] == "Aspoeck Systems"
        assert finder.calls == []

        supplier = repo.get_supplier(supplier_id)
        assert supplier["domain"] == "aspoeck.com"
        assert supplier["canonical_name"] == "Aspoeck Systems"
        assert collection.calls == [(supplier_id, "aspoeck.com")]

    def test_name_is_optional(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "IK Eng Ltd", "domain": "easydigitalfiling.com"})
        service = SupplierCorrectionService(
            repo=repo, website_finder=FakeWebsiteFinder(), collection_service=FakeCollectionService(),
        )

        result = service.set_confirmed_domain(supplier_id, "ikeng.co.uk")

        assert result["canonical_name"] == "IK Eng Ltd"  # unchanged
        assert repo.get_supplier(supplier_id)["canonical_name"] == "IK Eng Ltd"
        assert repo.get_supplier(supplier_id)["domain"] == "ikeng.co.uk"

    def test_raises_on_missing_supplier(self, repo):
        service = SupplierCorrectionService(
            repo=repo, website_finder=FakeWebsiteFinder(), collection_service=FakeCollectionService(),
        )
        with pytest.raises(ValueError):
            service.set_confirmed_domain(999999, "example.com")

    def test_domain_already_claimed_by_another_supplier_is_reported_not_raised(self, repo):
        """Real case, found live: setting the bad "Ashpock" record's
        domain to aspoeck.com failed outright, because the REAL
        Aspoeck Systems already had its own supplier row under that
        exact domain (suppliers.domain is UNIQUE) -- proof the bad
        record is a duplicate, not a retry-differently error. Must
        surface this cleanly (naming the existing row), not let a raw
        sqlite3.IntegrityError bubble up as an opaque failure."""
        real_id = repo.create_golden_record({"canonical_name": "Aspoeck Systems", "domain": "aspoeck.com"})
        bad_id = repo.create_golden_record({"canonical_name": "Ashpock", "domain": "shpock.com"})
        service = SupplierCorrectionService(
            repo=repo, website_finder=FakeWebsiteFinder(), collection_service=FakeCollectionService(),
        )

        result = service.set_confirmed_domain(bad_id, "aspoeck.com")

        assert result["status"] == "domain_conflict"
        assert result["new_domain"] is None
        assert result["conflicting_supplier_id"] == real_id
        assert "Aspoeck Systems" in result["reason"]
        # the bad record's own domain is untouched -- still the old wrong value, not silently cleared
        assert repo.get_supplier(bad_id)["domain"] == "shpock.com"


class TestFlagDuplicate:

    def test_flags_the_record_and_logs_the_reason(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Ashpock"})
        service = SupplierCorrectionService(
            repo=repo, website_finder=FakeWebsiteFinder(), collection_service=FakeCollectionService(),
        )

        result = service.flag_duplicate(supplier_id, "duplicate of #123 (Aspoeck Systems, aspoeck.com)")

        assert result["status"] == "flagged"
        supplier = repo.get_supplier(supplier_id)
        assert supplier["flagged"] == 1
        assert supplier["flag_reason"] == "duplicate of #123 (Aspoeck Systems, aspoeck.com)"

        log = repo.get_supplier_change_log(supplier_id)
        assert any(entry["field_name"] == "flagged" for entry in log)

    def test_raises_on_missing_supplier(self, repo):
        service = SupplierCorrectionService(
            repo=repo, website_finder=FakeWebsiteFinder(), collection_service=FakeCollectionService(),
        )
        with pytest.raises(ValueError):
            service.flag_duplicate(999999, "x")
