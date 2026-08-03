"""
tests/test_collection_service.py

Tests for collection/collection_service.py -- the orchestrator wiring
SiteCollector to storage.repository.SupplierRepository. Uses a fake
SiteCollector (no real browser/network), same DI-for-testability
pattern as tests/test_facility_and_linkedin_stages.py's
FakeAddressVerifier -- these are orchestration/repo-recording tests,
not extraction-logic tests (that's tests/test_site_collector.py, which
uses a real Playwright browser).
"""

from __future__ import annotations

import pytest

from collection.collection_service import CollectionService, _BATCH_SEMAPHORE
from collection.schemas import CollectedPage, CollectionResult
from storage.database import initialise_schema
from storage.repository import SupplierRepository


class FakeSiteCollector:
    """Returns a fixed CollectionResult (or raises) regardless of
    input, in call order if multiple are queued -- mirrors
    tests/test_pipeline.py's FakeScraper convention."""

    def __init__(self, results=None, raise_error=None):
        self._results = list(results) if results is not None else [
            CollectionResult(domain="acme.example.com", pages=[
                CollectedPage(url="https://acme.example.com", text="hi", has_contact_form=False),
            ], success=True, artifacts_dir="1/run1", proxy_provider="NoProxyProvider"),
        ]
        self._raise_error = raise_error
        self.calls = []

    def collect(self, supplier_id, domain):
        self.calls.append((supplier_id, domain))
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


class TestCollectSingleSupplier:

    def test_successful_collection_records_a_run_and_updates_supplier(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector()
        service = CollectionService(repo=repo, site_collector=fake)

        outcome = service.collect(supplier_id)

        assert outcome["status"] == "success"
        assert outcome["pages_visited"] == 1
        supplier = repo.get_supplier(supplier_id)
        assert supplier["collection_status"] == "success"
        assert supplier["collection_last_run_at"] is not None
        runs = repo.get_collection_runs(supplier_id)
        assert len(runs) == 1
        assert runs[0]["pages_visited"] == 1

    def test_raises_for_unknown_supplier(self, repo):
        service = CollectionService(repo=repo, site_collector=FakeSiteCollector())
        with pytest.raises(ValueError):
            service.collect(999999)

    def test_supplier_without_domain_fails_without_calling_collector(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "No Domain Co"})
        fake = FakeSiteCollector()
        service = CollectionService(repo=repo, site_collector=fake)

        outcome = service.collect(supplier_id)

        assert outcome["status"] == "failed"
        assert fake.calls == []  # never even tried
        supplier = repo.get_supplier(supplier_id)
        assert supplier["collection_status"] == "failed"

    def test_failed_collection_is_recorded_but_does_not_raise(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector(results=[
            CollectionResult(domain="acme.example.com", success=False, error="could not load homepage"),
        ])
        service = CollectionService(repo=repo, site_collector=fake)

        outcome = service.collect(supplier_id)  # must not raise

        assert outcome["status"] == "failed"
        assert outcome["error"] == "could not load homepage"

    def test_site_collector_raising_is_caught_not_propagated(self, repo):
        """SiteCollector itself never raises by contract, but
        CollectionService adds defence-in-depth anyway, matching every
        other pipeline stage's per-supplier fault isolation."""
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector(raise_error=RuntimeError("browser crashed"))
        service = CollectionService(repo=repo, site_collector=fake)

        outcome = service.collect(supplier_id)  # must not raise

        assert outcome["status"] == "failed"
        assert "browser crashed" in outcome["error"]


class TestContactExtraction:
    """A successful collection must also populate primary_email/
    primary_phone from the fetched pages -- this was a real gap (see
    collection_service.py's module docstring) where a freshly
    discovered supplier could be collected and verified repeatedly and
    never gain a contact detail. These tests use real page text so the
    real regex/phonenumbers extraction in
    verification/website_contact_extractor.py actually runs, not a
    mocked extractor."""

    def test_successful_collection_populates_email_and_phone(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector(results=[
            CollectionResult(domain="acme.example.com", success=True, artifacts_dir="1/run1", pages=[
                CollectedPage(
                    url="https://acme.example.com/contact",
                    text="Contact us: sales@acme.example.com or call +86 138 0000 0000.",
                    has_contact_form=False,
                ),
            ]),
        ])
        service = CollectionService(repo=repo, site_collector=fake)

        outcome = service.collect(supplier_id)

        assert outcome["status"] == "success"
        assert outcome["contact_emails_added"] == 1
        assert outcome["contact_phones_added"] == 1
        supplier = repo.get_supplier(supplier_id)
        assert supplier["primary_email"] == "sales@acme.example.com"
        assert supplier["primary_phone"] == "+8613800000000"

    def test_page_with_no_extractable_contact_details_adds_nothing(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector(results=[
            CollectionResult(domain="acme.example.com", success=True, artifacts_dir="1/run1", pages=[
                CollectedPage(url="https://acme.example.com", text="Welcome to Acme.", has_contact_form=False),
            ]),
        ])
        service = CollectionService(repo=repo, site_collector=fake)

        outcome = service.collect(supplier_id)

        assert outcome["status"] == "success"
        assert outcome["contact_emails_added"] == 0
        assert outcome["contact_phones_added"] == 0
        supplier = repo.get_supplier(supplier_id)
        assert supplier["primary_email"] is None
        assert supplier["primary_phone"] is None

    def test_existing_contact_details_are_not_overwritten(self, repo):
        """enrich_contact_details fills gaps only -- a supplier that
        already has a (possibly better-sourced, e.g. Alibaba listing)
        email on file must keep it, not have it replaced by whatever
        the own-website crawl happens to find."""
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme", "domain": "acme.example.com",
            "primary_email": "listing@acme-alibaba.example.com",
        })
        fake = FakeSiteCollector(results=[
            CollectionResult(domain="acme.example.com", success=True, artifacts_dir="1/run1", pages=[
                CollectedPage(
                    url="https://acme.example.com/contact",
                    text="Email us at othersales@acme.example.com",
                    has_contact_form=False,
                ),
            ]),
        ])
        service = CollectionService(repo=repo, site_collector=fake)

        service.collect(supplier_id)

        supplier = repo.get_supplier(supplier_id)
        assert supplier["primary_email"] == "listing@acme-alibaba.example.com"

    def test_failed_collection_does_not_attempt_contact_extraction(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector(results=[
            CollectionResult(domain="acme.example.com", success=False, error="timeout"),
        ])
        service = CollectionService(repo=repo, site_collector=fake)

        outcome = service.collect(supplier_id)

        assert outcome["status"] == "failed"
        assert outcome["contact_emails_added"] == 0
        assert outcome["contact_phones_added"] == 0


class TestCollectPending:

    def test_processes_every_eligible_supplier(self, repo):
        id_a = repo.create_golden_record({"canonical_name": "A Co", "domain": "a.example.com"})
        id_b = repo.create_golden_record({"canonical_name": "B Co", "domain": "b.example.com"})
        fake = FakeSiteCollector(results=[
            CollectionResult(domain="a.example.com", success=True, pages=[CollectedPage(url="https://a.example.com", text="a")]),
            CollectionResult(domain="b.example.com", success=True, pages=[CollectedPage(url="https://b.example.com", text="b")]),
        ])
        service = CollectionService(repo=repo, site_collector=fake)

        stats = service.collect_pending(limit=10)

        assert stats["attempted"] == 2
        assert stats["succeeded"] == 2
        assert stats["failed"] == 0
        assert stats["status"] == "completed"
        assert {c[0] for c in fake.calls} == {id_a, id_b}

    def test_domain_less_suppliers_are_never_attempted(self, repo):
        repo.create_golden_record({"canonical_name": "No Domain Co"})
        fake = FakeSiteCollector()
        service = CollectionService(repo=repo, site_collector=fake)

        stats = service.collect_pending(limit=10)

        assert stats["attempted"] == 0
        assert fake.calls == []

    def test_already_collected_suppliers_are_skipped_without_force(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector()
        service = CollectionService(repo=repo, site_collector=fake)
        service.collect(supplier_id)  # first pass -- sets collection_status

        stats = service.collect_pending(limit=10)  # second pass, no force

        assert stats["attempted"] == 0

    def test_force_reprocesses_already_collected_suppliers(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector()
        service = CollectionService(repo=repo, site_collector=fake)
        service.collect(supplier_id)

        stats = service.collect_pending(limit=10, force=True)

        assert stats["attempted"] == 1

    def test_partial_failure_does_not_abort_the_batch(self, repo):
        id_a = repo.create_golden_record({"canonical_name": "A Co", "domain": "a.example.com"})
        id_b = repo.create_golden_record({"canonical_name": "B Co", "domain": "b.example.com"})
        fake = FakeSiteCollector(results=[
            CollectionResult(domain="a.example.com", success=False, error="timeout"),
            CollectionResult(domain="b.example.com", success=True, pages=[CollectedPage(url="https://b.example.com", text="b")]),
        ])
        service = CollectionService(repo=repo, site_collector=fake)

        stats = service.collect_pending(limit=10)

        assert stats["attempted"] == 2
        assert stats["succeeded"] == 1
        assert stats["failed"] == 1

    def test_wall_clock_budget_stops_the_batch_early(self, repo):
        for i in range(3):
            repo.create_golden_record({"canonical_name": f"Co {i}", "domain": f"co{i}.example.com"})
        fake = FakeSiteCollector(results=[
            CollectionResult(domain=f"co{i}.example.com", success=True, pages=[CollectedPage(url="u", text="t")])
            for i in range(3)
        ])
        service = CollectionService(repo=repo, site_collector=fake, job_max_seconds=-1)  # already "over budget"

        stats = service.collect_pending(limit=10)

        assert stats["attempted"] == 0  # budget check happens before the first supplier
        assert stats["status"] == "partial"

    def test_concurrent_batch_is_skipped_not_queued(self, repo):
        """A second collect_pending() call while one is already running
        on this instance must not block/queue -- it returns
        immediately with status='skipped', matching the module
        docstring's documented behaviour."""
        repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector()
        service = CollectionService(repo=repo, site_collector=fake)

        acquired = _BATCH_SEMAPHORE.acquire(blocking=False)
        assert acquired  # simulate another batch already holding the semaphore
        try:
            stats = service.collect_pending(limit=10)
            assert stats["status"] == "skipped"
            assert stats["attempted"] == 0
        finally:
            _BATCH_SEMAPHORE.release()
