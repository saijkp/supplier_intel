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

import threading
import time

import pytest

from collection.collection_service import CollectionService, _BATCH_SEMAPHORE
from collection.schemas import CertificateDocument, CollectedPage, CollectionResult
from storage.database import initialise_schema
from storage.repository import SupplierRepository


class FakeSiteCollector:
    """Returns a CollectionResult (or raises) keyed by domain -- a
    dict lookup rather than a positional queue, so which fake result
    comes back never depends on call order. This matters now that
    CollectionService.collect_pending() processes suppliers in
    concurrent waves: a positional `.pop(0)` queue would race across
    threads and silently pair the wrong result with the wrong
    supplier. `self.calls` is a plain list -- CPython's GIL makes
    `list.append` itself atomic, so concurrent appends are safe, but
    the resulting ORDER is not guaranteed; tests that care about call
    order construct CollectionService with parallel_workers=1.
    """

    def __init__(self, results_by_domain=None, default_result=None, raise_error=None, delay_seconds=0.0):
        self._results_by_domain = dict(results_by_domain) if results_by_domain else {}
        self._default_result = default_result or CollectionResult(
            domain="acme.example.com", pages=[
                CollectedPage(url="https://acme.example.com", text="hi", has_contact_form=False),
            ], success=True, artifacts_dir="1/run1", proxy_provider="NoProxyProvider",
        )
        self._raise_error = raise_error
        self._delay_seconds = delay_seconds
        self._lock = threading.Lock()
        self.calls = []
        # How many collect() calls are simultaneously in flight -- used to
        # directly prove real concurrency (max_concurrent >= 2) instead of
        # a wall-clock ratio, which proved flaky under real SQLite write
        # overhead from multiple threads (see
        # test_parallel_workers_process_concurrently's own comment).
        self.active = 0
        self.max_concurrent = 0

    def collect(self, supplier_id, domain, source_url=None):
        with self._lock:
            self.calls.append((supplier_id, domain, source_url))
            self.active += 1
            self.max_concurrent = max(self.max_concurrent, self.active)
        try:
            if self._delay_seconds:
                time.sleep(self._delay_seconds)
            if self._raise_error:
                raise self._raise_error
            return self._results_by_domain.get(domain, self._default_result)
        finally:
            with self._lock:
                self.active -= 1


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
        fake = FakeSiteCollector(results_by_domain={
            "acme.example.com": CollectionResult(domain="acme.example.com", success=False, error="could not load homepage"),
        })
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
        fake = FakeSiteCollector(results_by_domain={
            "acme.example.com": CollectionResult(domain="acme.example.com", success=True, artifacts_dir="1/run1", pages=[
                CollectedPage(
                    url="https://acme.example.com/contact",
                    text="Contact us: sales@acme.example.com or call +86 138 0000 0000.",
                    has_contact_form=False,
                ),
            ]),
        })
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
        fake = FakeSiteCollector(results_by_domain={
            "acme.example.com": CollectionResult(domain="acme.example.com", success=True, artifacts_dir="1/run1", pages=[
                CollectedPage(url="https://acme.example.com", text="Welcome to Acme.", has_contact_form=False),
            ]),
        })
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
        fake = FakeSiteCollector(results_by_domain={
            "acme.example.com": CollectionResult(domain="acme.example.com", success=True, artifacts_dir="1/run1", pages=[
                CollectedPage(
                    url="https://acme.example.com/contact",
                    text="Email us at othersales@acme.example.com",
                    has_contact_form=False,
                ),
            ]),
        })
        service = CollectionService(repo=repo, site_collector=fake)

        service.collect(supplier_id)

        supplier = repo.get_supplier(supplier_id)
        assert supplier["primary_email"] == "listing@acme-alibaba.example.com"

    def test_failed_collection_does_not_attempt_contact_extraction(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector(results_by_domain={
            "acme.example.com": CollectionResult(domain="acme.example.com", success=False, error="timeout"),
        })
        service = CollectionService(repo=repo, site_collector=fake)

        outcome = service.collect(supplier_id)

        assert outcome["status"] == "failed"
        assert outcome["contact_emails_added"] == 0
        assert outcome["contact_phones_added"] == 0

    def test_all_typed_phone_numbers_are_saved_not_just_the_first(self, repo):
        """The bug found via a real calibration run: a landline
        appearing first in page text must not cause a later mobile
        number to be lost -- primary_phone still only holds the first
        (unchanged), but supplier_phone_numbers keeps everything."""
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector(results_by_domain={
            "acme.example.com": CollectionResult(domain="acme.example.com", success=True, artifacts_dir="1/run1", pages=[
                CollectedPage(
                    url="https://acme.example.com/contact",
                    text="Tel: +862112345678, Mobile: +8613800001111",
                    has_contact_form=False,
                ),
            ]),
        })
        service = CollectionService(repo=repo, site_collector=fake)

        outcome = service.collect(supplier_id)

        assert outcome["contact_phone_types_saved"] == 2
        supplier = repo.get_supplier(supplier_id)
        assert supplier["primary_phone"] == "+862112345678"  # first found, unchanged behaviour

        phones = repo.get_phone_numbers(supplier_id)
        assert len(phones) == 2
        by_type = {p["phone_type"]: p["phone_number"] for p in phones}
        assert by_type == {"landline": "+862112345678", "mobile": "+8613800001111"}
        assert all(p["source_url"] == "https://acme.example.com/contact" for p in phones)

    def test_repeat_collection_does_not_duplicate_phone_rows(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector(results_by_domain={
            "acme.example.com": CollectionResult(domain="acme.example.com", success=True, artifacts_dir="1/run1", pages=[
                CollectedPage(url="https://acme.example.com/contact", text="Mobile: +8613800001111", has_contact_form=False),
            ]),
        })
        service = CollectionService(repo=repo, site_collector=fake)

        service.collect(supplier_id)
        second_outcome = service.collect(supplier_id)

        assert second_outcome["contact_phone_types_saved"] == 0  # already on file, nothing new inserted
        assert len(repo.get_phone_numbers(supplier_id)) == 1

    def test_whatsapp_and_wechat_gap_fill_from_context_labelled_numbers(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector(results_by_domain={
            "acme.example.com": CollectionResult(domain="acme.example.com", success=True, artifacts_dir="1/run1", pages=[
                CollectedPage(
                    url="https://acme.example.com/contact",
                    text="WhatsApp: +8613800001111, WeChat: +8613900002222",
                    has_contact_form=False,
                ),
            ]),
        })
        service = CollectionService(repo=repo, site_collector=fake)

        service.collect(supplier_id)

        supplier = repo.get_supplier(supplier_id)
        assert supplier["whatsapp"] == "+8613800001111"
        assert supplier["wechat_id"] == "+8613900002222"

    def test_whatsapp_gap_fill_never_overwrites_an_existing_value(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme", "domain": "acme.example.com", "whatsapp": "+10000000000",
        })
        fake = FakeSiteCollector(results_by_domain={
            "acme.example.com": CollectionResult(domain="acme.example.com", success=True, artifacts_dir="1/run1", pages=[
                CollectedPage(url="https://acme.example.com/contact", text="WhatsApp: +8613800001111", has_contact_form=False),
            ]),
        })
        service = CollectionService(repo=repo, site_collector=fake)

        service.collect(supplier_id)

        assert repo.get_supplier(supplier_id)["whatsapp"] == "+10000000000"

    def test_contact_source_pages_accumulates_distinct_urls(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector(results_by_domain={
            "acme.example.com": CollectionResult(domain="acme.example.com", success=True, artifacts_dir="1/run1", pages=[
                CollectedPage(url="https://acme.example.com/", text="Tel: +862112345678", has_contact_form=False),
                CollectedPage(url="https://acme.example.com/contact", text="Email: sales@acme.example.com", has_contact_form=False),
                CollectedPage(url="https://acme.example.com/about", text="Founded in 1998.", has_contact_form=False),
            ]),
        })
        service = CollectionService(repo=repo, site_collector=fake)

        service.collect(supplier_id)

        supplier = repo.get_supplier(supplier_id)
        assert set(supplier["contact_source_pages"]) == {
            "https://acme.example.com/", "https://acme.example.com/contact",
        }

    def test_obfuscated_email_is_found_through_the_full_collection_path(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector(results_by_domain={
            "acme.example.com": CollectionResult(domain="acme.example.com", success=True, artifacts_dir="1/run1", pages=[
                CollectedPage(
                    url="https://acme.example.com/contact",
                    text="Email us: info[at]acme.example.com for a quote.",
                    has_contact_form=False,
                ),
            ]),
        })
        service = CollectionService(repo=repo, site_collector=fake)

        service.collect(supplier_id)

        assert repo.get_supplier(supplier_id)["primary_email"] == "info@acme.example.com"


class TestDefaultRegionFallback:
    """default_region_fallback exists because collect() always runs
    BEFORE address/country extraction in the batch-upload flow (see
    batch/batch_service.py's module docstring) -- a freshly-created
    supplier's country is genuinely unknown at the moment contact
    extraction runs, so a national-format phone number (no +44 prefix)
    silently fails to parse without a region hint. Found via a real
    diagnostic run against truckmasters.co.uk/feeleruk.com."""

    def test_national_format_number_lost_without_a_fallback(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector(results_by_domain={
            "acme.example.com": CollectionResult(domain="acme.example.com", success=True, artifacts_dir="1/run1", pages=[
                CollectedPage(url="https://acme.example.com/contact", text="Tel: 01754 880481", has_contact_form=False),
            ]),
        })
        service = CollectionService(repo=repo, site_collector=fake)

        service.collect(supplier_id)

        assert repo.get_supplier(supplier_id)["primary_phone"] is None

    def test_national_format_number_recovered_with_fallback(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector(results_by_domain={
            "acme.example.com": CollectionResult(domain="acme.example.com", success=True, artifacts_dir="1/run1", pages=[
                CollectedPage(url="https://acme.example.com/contact", text="Tel: 01754 880481", has_contact_form=False),
            ]),
        })
        service = CollectionService(repo=repo, site_collector=fake, default_region_fallback="GB")

        service.collect(supplier_id)

        assert repo.get_supplier(supplier_id)["primary_phone"] == "+441754880481"

    def test_a_known_supplier_country_always_wins_over_the_fallback(self, repo):
        """The fallback must never override a real, known country --
        only fill in when country_name_to_region_code(country) itself
        resolves to nothing."""
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme", "domain": "acme.example.com", "country": "China",
        })
        fake = FakeSiteCollector(results_by_domain={
            "acme.example.com": CollectionResult(domain="acme.example.com", success=True, artifacts_dir="1/run1", pages=[
                CollectedPage(url="https://acme.example.com/contact", text="Tel: 021-1234 5678", has_contact_form=False),
            ]),
        })
        # A wrong fallback (GB) must not corrupt a real Chinese number --
        # proves the fallback only ever fires when country resolution fails.
        service = CollectionService(repo=repo, site_collector=fake, default_region_fallback="GB")

        service.collect(supplier_id)

        supplier = repo.get_supplier(supplier_id)
        assert supplier["primary_phone"] is not None
        assert supplier["primary_phone"].startswith("+86")

    def test_mailto_and_tel_hrefs_are_saved_through_the_full_collection_path(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector(results_by_domain={
            "acme.example.com": CollectionResult(domain="acme.example.com", success=True, artifacts_dir="1/run1", pages=[
                CollectedPage(
                    url="https://acme.example.com/contact",
                    text="Telephone: 01754 880481 Email: Click Here",
                    mailto_emails=["mail@acme.example.com"],
                    tel_phones=["01754880481"],
                    has_contact_form=False,
                ),
            ]),
        })
        service = CollectionService(repo=repo, site_collector=fake, default_region_fallback="GB")

        outcome = service.collect(supplier_id)

        assert outcome["contact_emails_added"] == 1
        assert outcome["contact_phones_added"] == 1
        supplier = repo.get_supplier(supplier_id)
        assert supplier["primary_email"] == "mail@acme.example.com"
        assert supplier["primary_phone"] == "+441754880481"


class TestPlaceholderEmailLogging:
    """abc@xyz.com-style template defaults must never be stored as real
    contact data (see verification.website_contact_extractor.
    find_placeholder_emails), but must be visible for review rather
    than silently vanishing -- recorded via field_provenance, the same
    table this codebase already uses for "disagreement, not applied"
    signals (batch_service.py's trusted-value guard)."""

    def test_placeholder_email_is_not_stored_but_is_logged(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector(results_by_domain={
            "acme.example.com": CollectionResult(domain="acme.example.com", success=True, artifacts_dir="1/run1", pages=[
                CollectedPage(url="https://acme.example.com/contact", text="Email us: abc@xyz.com", has_contact_form=False),
            ]),
        })
        service = CollectionService(repo=repo, site_collector=fake)

        outcome = service.collect(supplier_id)

        assert outcome["contact_emails_added"] == 0
        assert repo.get_supplier(supplier_id)["primary_email"] is None
        provenance = repo.get_field_provenance(supplier_id, "rejected_placeholder_email")
        assert len(provenance) == 1
        assert provenance[0]["value"] == "abc@xyz.com"
        assert provenance[0]["source_url"] == "https://acme.example.com/contact"
        assert provenance[0]["source_tier"] == "own_domain"
        assert provenance[0]["claim_type"] == "verifiable_fact"

    def test_a_real_email_alongside_a_placeholder_is_still_stored(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector(results_by_domain={
            "acme.example.com": CollectionResult(domain="acme.example.com", success=True, artifacts_dir="1/run1", pages=[
                CollectedPage(
                    url="https://acme.example.com/contact",
                    text="Sales: sales@acme.example.com. Template default: abc@xyz.com",
                    has_contact_form=False,
                ),
            ]),
        })
        service = CollectionService(repo=repo, site_collector=fake)

        service.collect(supplier_id)

        assert repo.get_supplier(supplier_id)["primary_email"] == "sales@acme.example.com"
        provenance = repo.get_field_provenance(supplier_id, "rejected_placeholder_email")
        assert [p["value"] for p in provenance] == ["abc@xyz.com"]

    def test_placeholder_found_via_mailto_href_is_also_logged(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector(results_by_domain={
            "acme.example.com": CollectionResult(domain="acme.example.com", success=True, artifacts_dir="1/run1", pages=[
                CollectedPage(
                    url="https://acme.example.com/contact", text="Email: Click Here",
                    mailto_emails=["abc@xyz.com"], has_contact_form=False,
                ),
            ]),
        })
        service = CollectionService(repo=repo, site_collector=fake)

        service.collect(supplier_id)

        provenance = repo.get_field_provenance(supplier_id, "rejected_placeholder_email")
        assert len(provenance) == 1
        assert provenance[0]["value"] == "abc@xyz.com"
        assert provenance[0]["extraction_method"] == "mailto_href"

    def test_no_placeholder_writes_nothing(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector(results_by_domain={
            "acme.example.com": CollectionResult(domain="acme.example.com", success=True, artifacts_dir="1/run1", pages=[
                CollectedPage(url="https://acme.example.com/contact", text="Sales: sales@acme.example.com", has_contact_form=False),
            ]),
        })
        service = CollectionService(repo=repo, site_collector=fake)

        service.collect(supplier_id)

        assert repo.get_field_provenance(supplier_id, "rejected_placeholder_email") == []


class TestCertificateDocuments:
    """SiteCollector already downloaded/saved certificate files during
    collect() -- CollectionService's job here is just to record what
    was found onto the supplier row (see collection_service.py's
    _save_certificate_documents, sibling to _extract_and_save_contact_details)."""

    def test_successful_collection_records_certificate_documents(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector(results_by_domain={
            "acme.example.com": CollectionResult(
                domain="acme.example.com", success=True, artifacts_dir="1/run1",
                pages=[CollectedPage(url="https://acme.example.com", text="hi")],
                certificate_documents=[
                    CertificateDocument(
                        url="https://acme.example.com/iso-9001.pdf", matched_keyword="iso",
                        filename="iso-9001.pdf", artifact_path="downloads/iso-9001.pdf",
                    ),
                ],
            ),
        })
        service = CollectionService(repo=repo, site_collector=fake)

        outcome = service.collect(supplier_id)

        assert outcome["certificates_saved"] == 1
        supplier = repo.get_supplier(supplier_id)
        assert len(supplier["certificate_document_urls"]) == 1
        assert supplier["certificate_document_urls"][0]["matched_keyword"] == "iso"

    def test_no_certificate_documents_found_records_nothing(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector()  # default result has no certificate_documents
        service = CollectionService(repo=repo, site_collector=fake)

        outcome = service.collect(supplier_id)

        assert outcome["certificates_saved"] == 0
        supplier = repo.get_supplier(supplier_id)
        assert supplier["certificate_document_urls"] is None

    def test_failed_collection_does_not_attempt_to_save_certificates(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        fake = FakeSiteCollector(results_by_domain={
            "acme.example.com": CollectionResult(domain="acme.example.com", success=False, error="timeout"),
        })
        service = CollectionService(repo=repo, site_collector=fake)

        outcome = service.collect(supplier_id)

        assert outcome["certificates_saved"] == 0


class TestCollectPending:

    def test_processes_every_eligible_supplier(self, repo):
        id_a = repo.create_golden_record({"canonical_name": "A Co", "domain": "a.example.com"})
        id_b = repo.create_golden_record({"canonical_name": "B Co", "domain": "b.example.com"})
        fake = FakeSiteCollector(results_by_domain={
            "a.example.com": CollectionResult(domain="a.example.com", success=True, pages=[CollectedPage(url="https://a.example.com", text="a")]),
            "b.example.com": CollectionResult(domain="b.example.com", success=True, pages=[CollectedPage(url="https://b.example.com", text="b")]),
        })
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
        fake = FakeSiteCollector(results_by_domain={
            "a.example.com": CollectionResult(domain="a.example.com", success=False, error="timeout"),
            "b.example.com": CollectionResult(domain="b.example.com", success=True, pages=[CollectedPage(url="https://b.example.com", text="b")]),
        })
        service = CollectionService(repo=repo, site_collector=fake)

        stats = service.collect_pending(limit=10)

        assert stats["attempted"] == 2
        assert stats["succeeded"] == 1
        assert stats["failed"] == 1

    def test_wall_clock_budget_stops_the_batch_early(self, repo):
        for i in range(3):
            repo.create_golden_record({"canonical_name": f"Co {i}", "domain": f"co{i}.example.com"})
        fake = FakeSiteCollector(results_by_domain={
            f"co{i}.example.com": CollectionResult(domain=f"co{i}.example.com", success=True, pages=[CollectedPage(url="u", text="t")])
            for i in range(3)
        })
        service = CollectionService(repo=repo, site_collector=fake, job_max_seconds=-1)  # already "over budget"

        stats = service.collect_pending(limit=10)

        assert stats["attempted"] == 0  # budget check happens before the first supplier
        assert stats["status"] == "partial"

    def test_parallel_workers_process_concurrently(self, repo):
        """The real point of Phase 0: waves of parallel_workers suppliers
        run CONCURRENTLY, not one at a time. Asserted directly via
        max_concurrent (how many collect() calls were simultaneously in
        flight) rather than a wall-clock ratio -- _collect_one makes
        several real SQLite writes per supplier, and WAL's
        single-writer semantics add real, environment-dependent
        serialization overhead on top of the artificial sleep, which
        made a wall-clock-ratio assertion flaky (see
        tests/test_sourcing_agent.py's own identical fix for the same
        reason)."""
        ids = [
            repo.create_golden_record({"canonical_name": f"Co {i}", "domain": f"co{i}.example.com"})
            for i in range(6)
        ]
        fake = FakeSiteCollector(delay_seconds=0.15)
        service = CollectionService(repo=repo, site_collector=fake, parallel_workers=3)

        stats = service.collect_pending(limit=10)

        assert stats["attempted"] == 6
        assert len(ids) == 6
        assert fake.max_concurrent >= 2  # real overlap, not one-at-a-time

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
