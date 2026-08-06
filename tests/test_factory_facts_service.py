"""
tests/test_factory_facts_service.py

Tests for verification/factory_facts_service.py -- the orchestrator
wiring FactoryFactsExtractor + OwnWebsiteScraper to
storage.repository.SupplierRepository. Uses fakes for both (no real
network/OpenAI credits), same DI-for-testability pattern as
tests/test_contact_finder_service.py -- these are orchestration/
repo-recording tests, not extraction-logic tests (that's
tests/test_factory_facts_extractor.py).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from storage.database import initialise_schema
from storage.repository import SupplierRepository
from verification.factory_facts_extractor import FactoryFactsResult
from verification.factory_facts_service import FactoryFactsService, _BATCH_SEMAPHORE


class FakeOwnWebsiteScraper:
    def __init__(self, success=True, pages=None, raise_error=None):
        self._success = success
        self._pages = pages if pages is not None else [SimpleNamespace(url="https://acme.example.com", text="We operate three production lines.")]
        self._raise_error = raise_error
        self.calls = []

    def fetch(self, domain):
        self.calls.append(domain)
        if self._raise_error:
            raise self._raise_error
        return SimpleNamespace(success=self._success, pages=self._pages if self._success else [], error=None)


class FakeFactoryFactsExtractor:
    """Returns a fixed FactoryFactsResult (or None, or raises) --
    mirrors tests/test_contact_finder_service.py's FakeApolloContactFinder
    convention."""

    def __init__(self, result="default", raise_error=None):
        self._result = (
            FactoryFactsResult(
                production_lines_notes="Three production lines described.",
                machinery_notes="Named CNC machines.",
                factory_ownership="owned",
                model_used="gpt-4o-mini",
            ) if result == "default" else result
        )
        self._raise_error = raise_error
        self.calls = []

    def extract_from_pages(self, pages):
        self.calls.append(pages)
        if self._raise_error:
            raise self._raise_error
        return self._result


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


class TestFindFactsSingleSupplier:

    def test_successful_extraction_records_facts_and_marker(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        service = FactoryFactsService(repo=repo, extractor=FakeFactoryFactsExtractor(), own_website_scraper=FakeOwnWebsiteScraper())

        outcome = service.find_facts(supplier_id)

        assert outcome["status"] == "extracted"
        assert outcome["factory_ownership"] == "owned"
        supplier = repo.get_supplier(supplier_id)
        assert supplier["factory_facts_extracted_at"] is not None
        assert supplier["production_lines_notes"] == "Three production lines described."
        assert supplier["machinery_notes"] == "Named CNC machines."
        assert supplier["factory_ownership"] == "owned"

    def test_raises_for_unknown_supplier(self, repo):
        service = FactoryFactsService(repo=repo, extractor=FakeFactoryFactsExtractor(), own_website_scraper=FakeOwnWebsiteScraper())
        with pytest.raises(ValueError):
            service.find_facts(999999)

    def test_no_domain_marks_unavailable_and_sets_marker(self, repo):
        """Same never-attempted-vs-attempted-and-found-nothing
        discipline as contacts_found_at -- must still mark
        factory_facts_extracted_at so a batch run doesn't re-attempt it
        (and re-bill OpenAI) every single pass forever."""
        supplier_id = repo.create_golden_record({"canonical_name": "No Domain Co"})
        extractor = FakeFactoryFactsExtractor()
        service = FactoryFactsService(repo=repo, extractor=extractor, own_website_scraper=FakeOwnWebsiteScraper())

        outcome = service.find_facts(supplier_id)

        assert outcome["status"] == "unavailable"
        supplier = repo.get_supplier(supplier_id)
        assert supplier["factory_facts_extracted_at"] is not None
        assert extractor.calls == []  # never even tried

    def test_own_site_fetch_failure_marks_unavailable_not_fatal(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        service = FactoryFactsService(
            repo=repo, extractor=FakeFactoryFactsExtractor(),
            own_website_scraper=FakeOwnWebsiteScraper(success=False),
        )

        outcome = service.find_facts(supplier_id)  # must not raise

        assert outcome["status"] == "unavailable"
        supplier = repo.get_supplier(supplier_id)
        assert supplier["factory_facts_extracted_at"] is not None

    def test_own_site_fetch_raising_is_caught_not_propagated(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        service = FactoryFactsService(
            repo=repo, extractor=FakeFactoryFactsExtractor(),
            own_website_scraper=FakeOwnWebsiteScraper(raise_error=RuntimeError("network down")),
        )

        outcome = service.find_facts(supplier_id)  # must not raise

        assert outcome["status"] == "unavailable"
        supplier = repo.get_supplier(supplier_id)
        assert supplier["factory_facts_extracted_at"] is not None

    def test_extractor_returning_none_marks_no_evidence_not_fatal(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        service = FactoryFactsService(
            repo=repo, extractor=FakeFactoryFactsExtractor(result=None),
            own_website_scraper=FakeOwnWebsiteScraper(),
        )

        outcome = service.find_facts(supplier_id)

        assert outcome["status"] == "no_evidence"
        supplier = repo.get_supplier(supplier_id)
        assert supplier["factory_facts_extracted_at"] is not None
        assert supplier["production_lines_notes"] is None


class TestFindFactsPending:

    def test_processes_every_eligible_supplier(self, repo):
        id_a = repo.create_golden_record({"canonical_name": "A Co", "domain": "a.example.com"})
        id_b = repo.create_golden_record({"canonical_name": "B Co", "domain": "b.example.com"})
        service = FactoryFactsService(repo=repo, extractor=FakeFactoryFactsExtractor(), own_website_scraper=FakeOwnWebsiteScraper())

        stats = service.find_facts_pending(limit=10)

        assert stats["attempted"] == 2
        assert stats["succeeded"] == 2
        assert stats["failed"] == 0
        assert stats["status"] == "completed"

    def test_domain_less_suppliers_are_never_attempted(self, repo):
        repo.create_golden_record({"canonical_name": "No Domain Co"})
        scraper = FakeOwnWebsiteScraper()
        service = FactoryFactsService(repo=repo, extractor=FakeFactoryFactsExtractor(), own_website_scraper=scraper)

        stats = service.find_facts_pending(limit=10)

        assert stats["attempted"] == 0
        assert scraper.calls == []

    def test_already_attempted_suppliers_are_skipped_without_force(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        service = FactoryFactsService(repo=repo, extractor=FakeFactoryFactsExtractor(), own_website_scraper=FakeOwnWebsiteScraper())
        service.find_facts(supplier_id)  # first pass -- sets factory_facts_extracted_at

        stats = service.find_facts_pending(limit=10)  # second pass, no force

        assert stats["attempted"] == 0

    def test_force_reprocesses_already_attempted_suppliers(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        service = FactoryFactsService(repo=repo, extractor=FakeFactoryFactsExtractor(), own_website_scraper=FakeOwnWebsiteScraper())
        service.find_facts(supplier_id)

        stats = service.find_facts_pending(limit=10, force=True)

        assert stats["attempted"] == 1

    def test_wall_clock_budget_stops_the_batch_early(self, repo):
        for i in range(3):
            repo.create_golden_record({"canonical_name": f"Co {i}", "domain": f"co{i}.example.com"})
        service = FactoryFactsService(
            repo=repo, extractor=FakeFactoryFactsExtractor(), own_website_scraper=FakeOwnWebsiteScraper(),
            job_max_seconds=-1,  # already "over budget"
        )

        stats = service.find_facts_pending(limit=10)

        assert stats["attempted"] == 0  # budget check happens before the first supplier
        assert stats["status"] == "partial"

    def test_concurrent_batch_is_skipped_not_queued(self, repo):
        """A second find_facts_pending() call while one is already
        running on this instance must not block/queue -- it returns
        immediately with status='skipped', matching
        ContactFinderService's documented behaviour."""
        repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        service = FactoryFactsService(repo=repo, extractor=FakeFactoryFactsExtractor(), own_website_scraper=FakeOwnWebsiteScraper())

        acquired = _BATCH_SEMAPHORE.acquire(blocking=False)
        assert acquired  # simulate another batch already holding the semaphore
        try:
            stats = service.find_facts_pending(limit=10)
            assert stats["status"] == "skipped"
            assert stats["attempted"] == 0
        finally:
            _BATCH_SEMAPHORE.release()
