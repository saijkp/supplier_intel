"""
tests/test_contact_finder_service.py

Tests for verification/contact_finder_service.py -- the orchestrator
wiring ApolloContactFinder to storage.repository.SupplierRepository.
Uses a fake ApolloContactFinder (no real network/credits), same
DI-for-testability pattern as tests/test_collection_service.py's
FakeSiteCollector -- these are orchestration/repo-recording tests, not
Apollo-parsing-logic tests (that's tests/test_apollo_contact_finder.py).
"""

from __future__ import annotations

import pytest

from storage.database import initialise_schema
from storage.repository import SupplierRepository
from verification.apollo_contact_finder import ApolloContact, ApolloContactResult
from verification.contact_finder_service import ContactFinderService, _BATCH_SEMAPHORE


class FakeApolloContactFinder:
    """Returns a fixed ApolloContactResult (or raises) regardless of
    input, in call order if multiple are queued -- mirrors
    tests/test_collection_service.py's FakeSiteCollector convention."""

    def __init__(self, results=None, raise_error=None):
        self._results = list(results) if results is not None else [
            ApolloContactResult(
                contacts=[ApolloContact(
                    name="Jane Doe", title="Procurement Manager", email="jane@acme.example.com",
                    phone=None, linkedin_url="https://linkedin.com/in/janedoe", role_category="procurement",
                )],
                source="apollo", reason="found 1 contact(s) at acme.example.com",
            ),
        ]
        self._raise_error = raise_error
        self.calls = []

    def find_contacts(self, company_name, domain=None):
        self.calls.append((company_name, domain))
        if self._raise_error:
            raise self._raise_error
        if len(self._results) == 1:
            return self._results[0]
        return self._results.pop(0)


@pytest.fixture()
def repo(tmp_path):
    db_path = tmp_path / "test.db"
    initialise_schema(db_path)
    return SupplierRepository(db_path=db_path)


@pytest.fixture(autouse=True)
def _reset_semaphore():
    """The batch semaphore is process-wide (module-level) by design --
    reset it around every test so one test's acquire can't starve
    another's."""
    yield
    while _BATCH_SEMAPHORE._value < 1:
        _BATCH_SEMAPHORE.release()


class TestFindContactsSingleSupplier:

    def test_successful_lookup_records_contacts_and_marker(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeApolloContactFinder()
        service = ContactFinderService(repo=repo, apollo_finder=fake)

        outcome = service.find_contacts(supplier_id)

        assert outcome["status"] == "apollo"
        assert outcome["contacts_found"] == 1
        supplier = repo.get_supplier(supplier_id)
        assert supplier["contacts_found_at"] is not None
        assert supplier["key_contacts"][0]["name"] == "Jane Doe"
        assert supplier["key_contacts"][0]["role_category"] == "procurement"
        assert fake.calls == [("Acme", "acme.example.com")]

    def test_raises_for_unknown_supplier(self, repo):
        service = ContactFinderService(repo=repo, apollo_finder=FakeApolloContactFinder())
        with pytest.raises(ValueError):
            service.find_contacts(999999)

    def test_unavailable_result_still_sets_marker_not_reattempted_forever(self, repo):
        """Same never-attempted-vs-attempted-and-found-nothing
        discipline as capability_extracted_at -- an 'unavailable'
        result (e.g. no API key, request failure) must still mark
        contacts_found_at so a batch run doesn't re-bill/re-attempt it
        every single pass forever."""
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeApolloContactFinder(results=[
            ApolloContactResult(contacts=[], source="unavailable", reason="APOLLO_API_KEY is not configured"),
        ])
        service = ContactFinderService(repo=repo, apollo_finder=fake)

        outcome = service.find_contacts(supplier_id)

        assert outcome["status"] == "unavailable"
        assert outcome["contacts_found"] == 0
        supplier = repo.get_supplier(supplier_id)
        assert supplier["contacts_found_at"] is not None

    def test_real_negative_result_is_recorded_as_empty_list_not_dropped(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeApolloContactFinder(results=[
            ApolloContactResult(contacts=[], source="apollo", reason="search completed, found nobody"),
        ])
        service = ContactFinderService(repo=repo, apollo_finder=fake)

        outcome = service.find_contacts(supplier_id)

        assert outcome["status"] == "apollo"
        assert outcome["contacts_found"] == 0
        supplier = repo.get_supplier(supplier_id)
        assert supplier["contacts_found_at"] is not None
        assert supplier["key_contacts"] == []

    def test_apollo_finder_raising_is_not_caught_by_service_itself(self, repo):
        """ApolloContactFinder never raises by contract -- this test
        documents that ContactFinderService relies on that contract
        rather than adding its own try/except, unlike CollectionService
        (which wraps SiteCollector defensively). Not a requirement, just
        documenting current behaviour."""
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeApolloContactFinder(raise_error=RuntimeError("should never happen"))
        service = ContactFinderService(repo=repo, apollo_finder=fake)

        with pytest.raises(RuntimeError):
            service.find_contacts(supplier_id)


class TestFindContactsPending:

    def test_processes_every_eligible_supplier(self, repo):
        id_a = repo.create_golden_record({"canonical_name": "A Co", "domain": "a.example.com"})
        id_b = repo.create_golden_record({"canonical_name": "B Co", "domain": "b.example.com"})
        fake = FakeApolloContactFinder(results=[
            ApolloContactResult(contacts=[], source="apollo", reason="r1"),
            ApolloContactResult(contacts=[], source="apollo", reason="r2"),
        ])
        service = ContactFinderService(repo=repo, apollo_finder=fake)

        stats = service.find_contacts_pending(limit=10)

        assert stats["attempted"] == 2
        assert stats["succeeded"] == 2
        assert stats["failed"] == 0
        assert stats["status"] == "completed"
        assert {c[1] for c in fake.calls} == {"a.example.com", "b.example.com"}

    def test_domain_less_suppliers_are_never_attempted(self, repo):
        repo.create_golden_record({"canonical_name": "No Domain Co"})
        fake = FakeApolloContactFinder()
        service = ContactFinderService(repo=repo, apollo_finder=fake)

        stats = service.find_contacts_pending(limit=10)

        assert stats["attempted"] == 0
        assert fake.calls == []

    def test_already_attempted_suppliers_are_skipped_without_force(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeApolloContactFinder()
        service = ContactFinderService(repo=repo, apollo_finder=fake)
        service.find_contacts(supplier_id)  # first pass -- sets contacts_found_at

        stats = service.find_contacts_pending(limit=10)  # second pass, no force

        assert stats["attempted"] == 0

    def test_force_reprocesses_already_attempted_suppliers(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeApolloContactFinder()
        service = ContactFinderService(repo=repo, apollo_finder=fake)
        service.find_contacts(supplier_id)

        stats = service.find_contacts_pending(limit=10, force=True)

        assert stats["attempted"] == 1

    def test_unavailable_results_count_as_failed_not_succeeded(self, repo):
        id_a = repo.create_golden_record({"canonical_name": "A Co", "domain": "a.example.com"})
        id_b = repo.create_golden_record({"canonical_name": "B Co", "domain": "b.example.com"})
        fake = FakeApolloContactFinder(results=[
            ApolloContactResult(contacts=[], source="unavailable", reason="no key"),
            ApolloContactResult(contacts=[], source="apollo", reason="found nobody"),
        ])
        service = ContactFinderService(repo=repo, apollo_finder=fake)

        stats = service.find_contacts_pending(limit=10)

        assert stats["attempted"] == 2
        assert stats["succeeded"] == 1
        assert stats["failed"] == 1

    def test_wall_clock_budget_stops_the_batch_early(self, repo):
        for i in range(3):
            repo.create_golden_record({"canonical_name": f"Co {i}", "domain": f"co{i}.example.com"})
        fake = FakeApolloContactFinder(results=[
            ApolloContactResult(contacts=[], source="apollo", reason="r")
            for _ in range(3)
        ])
        service = ContactFinderService(repo=repo, apollo_finder=fake, job_max_seconds=-1)  # already "over budget"

        stats = service.find_contacts_pending(limit=10)

        assert stats["attempted"] == 0  # budget check happens before the first supplier
        assert stats["status"] == "partial"

    def test_concurrent_batch_is_skipped_not_queued(self, repo):
        """A second find_contacts_pending() call while one is already
        running on this instance must not block/queue -- it returns
        immediately with status='skipped', matching CollectionService's
        documented behaviour."""
        repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeApolloContactFinder()
        service = ContactFinderService(repo=repo, apollo_finder=fake)

        acquired = _BATCH_SEMAPHORE.acquire(blocking=False)
        assert acquired  # simulate another batch already holding the semaphore
        try:
            stats = service.find_contacts_pending(limit=10)
            assert stats["status"] == "skipped"
            assert stats["attempted"] == 0
        finally:
            _BATCH_SEMAPHORE.release()
