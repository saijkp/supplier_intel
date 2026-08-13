"""
tests/test_batch_service.py

Tests for batch/batch_service.py -- routes each parsed CSV row through
the EXISTING single-company enrichment path (SupplierMatcher +
CollectionService), never a second extraction pipeline. Every
collaborator is faked (no DB, no network, no LLM) so these tests only
exercise BatchService's own row-classification/dedup/provenance logic.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from batch.batch_service import (
    BatchService,
    _placeholder_name_from_domain,
    _reject_reason_for_extracted_name,
)
from batch.csv_parser import ParsedRow


class FakeRepo:
    """In-memory stand-in for storage.repository.SupplierRepository --
    only the methods BatchService actually calls."""

    def __init__(self):
        self._next_supplier_id = 1
        self.suppliers: Dict[int, Dict[str, Any]] = {}   # id -> fields
        self._next_row_id = 1
        self.rows: Dict[int, Dict[str, Any]] = {}          # id -> fields
        self.provenance: List[Dict[str, Any]] = []
        self.history_calls: List[Dict[str, Any]] = []

    # -- batch_upload_rows --------------------------------------------
    def create_batch_upload_row(self, *, batch_job_id, row_index, original_columns,
                                 company_name, name_source="csv", website=None, status="pending"):
        row_id = self._next_row_id
        self._next_row_id += 1
        self.rows[row_id] = {
            "id": row_id, "batch_job_id": batch_job_id, "row_index": row_index,
            "original_columns": original_columns, "company_name": company_name,
            "name_source": name_source, "website": website, "status": status,
            "supplier_id": None, "error_message": None,
        }
        return row_id

    def update_batch_upload_row(self, row_id, fields):
        self.rows[row_id].update(fields)

    def get_batch_upload_rows(self, batch_job_id):
        return [r for r in self.rows.values() if r["batch_job_id"] == batch_job_id]

    # -- field_provenance ------------------------------------------------
    def save_field_provenance(self, *, supplier_id, field_name, value, source_url,
                               raw_snippet, extraction_method, source_tier, claim_type):
        entry = {
            "supplier_id": supplier_id, "field_name": field_name, "value": value,
            "source_url": source_url, "raw_snippet": raw_snippet,
            "extraction_method": extraction_method, "source_tier": source_tier,
            "claim_type": claim_type,
        }
        self.provenance.append(entry)
        return len(self.provenance)

    def get_field_provenance(self, supplier_id, field_name=None):
        return [p for p in self.provenance if p["supplier_id"] == supplier_id
                and (field_name is None or p["field_name"] == field_name)]

    # -- suppliers ------------------------------------------------------
    def get_supplier(self, supplier_id):
        supplier = self.suppliers.get(supplier_id)
        return dict(supplier) if supplier is not None else None

    def find_by_domain(self, domain):
        for s in self.suppliers.values():
            if s.get("domain") == domain:
                return dict(s)
        return None

    def merge_into_golden(self, supplier_id, supplier_data):
        """Mirrors storage.repository.SupplierRepository's real merge
        semantics for scalar fields: only fill in if the existing value
        is empty -- never let a lower-confidence source clobber an
        already-known fact. This distinction is load-bearing for the
        conflicting-name tests below, which depend on a pre-existing
        trusted name surviving _resolve_placeholder_row's merge call."""
        existing = self.suppliers[supplier_id]
        for field, new_value in supplier_data.items():
            if existing.get(field) in (None, "", 0) and new_value not in (None, ""):
                existing[field] = new_value

    def create_golden_record(self, supplier_data):
        supplier_id = self._next_supplier_id
        self._next_supplier_id += 1
        self.suppliers[supplier_id] = {"id": supplier_id, **supplier_data}
        return supplier_id

    def update_supplier_fields_with_history(self, supplier_id, fields, *, changed_by, change_reason=None):
        self.history_calls.append({"supplier_id": supplier_id, "fields": fields, "changed_by": changed_by})
        self.suppliers.setdefault(supplier_id, {"id": supplier_id}).update(fields)
        return []


class FakeMatcher:
    """Stand-in for deduplication.matcher.SupplierMatcher -- BatchService
    must go through this for named rows, and must NEVER call it for
    placeholder (name-less) rows."""

    def __init__(self, repo: FakeRepo):
        self.repo = repo
        self.calls: List[Dict[str, Any]] = []

    def resolve_and_store(self, candidate):
        self.calls.append(candidate)
        existing = self.repo.find_by_domain(candidate.get("domain"))
        if existing:
            self.repo.merge_into_golden(existing["id"], candidate)
            return {"supplier_id": existing["id"], "action": "merged"}
        supplier_id = self.repo.create_golden_record(dict(candidate))
        return {"new_supplier_id": supplier_id, "action": "created"}


class FakePage:
    def __init__(self, url, text):
        self.url = url
        self.text = text


class FakeCollectionService:
    """Stand-in for collection.collection_service.CollectionService.
    Results are pre-scripted per supplier_id via `results`; default is a
    plain success with no pages, matching most rows' needs."""

    def __init__(self, results: Optional[Dict[int, Dict[str, Any]]] = None):
        self.results = results or {}
        self.calls: List[Dict[str, Any]] = []

    def collect(self, supplier_id, return_pages=False, source_url=None):
        self.calls.append({"supplier_id": supplier_id, "return_pages": return_pages, "source_url": source_url})
        outcome = dict(self.results.get(supplier_id, {"supplier_id": supplier_id, "status": "success", "pages_visited": 1, "error": None}))
        if return_pages and "pages" not in outcome:
            outcome["pages"] = []
        return outcome


class FakeLLMClient:
    """Stand-in for llm.client.LLMClient -- only complete_json is used,
    only for placeholder-name extraction."""

    def __init__(self, response: Any = None):
        self.response = response
        self.calls: List[Dict[str, Any]] = []

    def complete_json(self, system_prompt, user_prompt, **kwargs):
        self.calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt})
        return self.response


def _row(row_index=0, company_name=None, website=None, original_columns=None):
    return ParsedRow(
        row_index=row_index, original_columns=original_columns or {},
        company_name=company_name, website=website,
    )


def _make_service(repo=None, matcher=None, collection_service=None, llm_client=None):
    repo = repo or FakeRepo()
    return BatchService(
        repo=repo,
        matcher=matcher or FakeMatcher(repo),
        collection_service=collection_service or FakeCollectionService(),
        llm_client=llm_client or FakeLLMClient(),
    ), repo


class TestNeedsUrl:

    def test_name_only_no_website_marks_needs_url(self):
        service, repo = _make_service()
        outcome = service.run_batch([_row(company_name="Acme Co", website=None)], "job-1")
        assert outcome.needs_url == 1
        assert outcome.processed == 0
        row = next(iter(repo.rows.values()))
        assert row["status"] == "needs_url"

    def test_neither_name_nor_website_marks_needs_url(self):
        service, repo = _make_service()
        outcome = service.run_batch([_row()], "job-1")
        assert outcome.needs_url == 1
        row = next(iter(repo.rows.values()))
        assert row["status"] == "needs_url"

    def test_needs_url_row_never_touches_matcher_or_collection(self):
        matcher = FakeMatcher(FakeRepo())
        collection = FakeCollectionService()
        service, repo = _make_service(matcher=matcher, collection_service=collection)
        service.run_batch([_row(company_name="Acme Co")], "job-1")
        assert matcher.calls == []
        assert collection.calls == []

    def test_website_not_parseable_to_a_domain_marks_needs_url(self):
        service, repo = _make_service()
        outcome = service.run_batch([_row(company_name="Acme Co", website="   ")], "job-1")
        assert outcome.needs_url == 1


class TestNamedRowSuccessPath:

    def test_company_name_and_website_resolves_via_matcher_and_collects(self):
        repo = FakeRepo()
        matcher = FakeMatcher(repo)
        collection = FakeCollectionService()
        service, _ = _make_service(repo=repo, matcher=matcher, collection_service=collection)
        outcome = service.run_batch(
            [_row(company_name="Acme Co", website="https://acme.com")], "job-1",
        )
        assert outcome.succeeded == 1
        assert len(matcher.calls) == 1
        assert matcher.calls[0]["canonical_name"] == "Acme Co"
        assert matcher.calls[0]["domain"] == "acme.com"
        assert len(collection.calls) == 1
        row = next(iter(repo.rows.values()))
        assert row["status"] == "success"
        assert row["name_source"] == "csv"
        assert row["supplier_id"] is not None

    def test_collect_failure_marks_row_failed_with_error_message(self):
        repo = FakeRepo()
        matcher = FakeMatcher(repo)
        collection = FakeCollectionService()
        service, _ = _make_service(repo=repo, matcher=matcher, collection_service=collection)
        # supplier id 1 will be the one created by the matcher below
        collection.results[1] = {"status": "failed", "error": "site unreachable", "pages_visited": 0}
        outcome = service.run_batch(
            [_row(company_name="Acme Co", website="https://acme.com")], "job-1",
        )
        assert outcome.failed == 1
        assert outcome.succeeded == 0
        row = next(iter(repo.rows.values()))
        assert row["status"] == "failed"
        assert row["error_message"] == "site unreachable"

    def test_resolve_exception_marks_row_failed_and_does_not_abort_batch(self):
        class ExplodingMatcher(FakeMatcher):
            def resolve_and_store(self, candidate):
                raise RuntimeError("dedup blew up")

        repo = FakeRepo()
        service, _ = _make_service(repo=repo, matcher=ExplodingMatcher(repo))
        outcome = service.run_batch(
            [
                _row(row_index=0, company_name="Acme Co", website="https://acme.com"),
                _row(row_index=1, company_name="Beta Co", website="https://beta.com"),
            ],
            "job-1",
        )
        assert outcome.failed == 2
        assert outcome.total_rows == 2
        for row in repo.rows.values():
            assert row["status"] == "failed"
            assert "dedup blew up" in row["error_message"]


class TestPlaceholderRows:

    def test_website_only_uses_domain_lookup_not_matcher(self):
        repo = FakeRepo()
        matcher = FakeMatcher(repo)
        service, _ = _make_service(repo=repo, matcher=matcher)
        outcome = service.run_batch([_row(website="https://acmetrailer.com")], "job-1")
        assert matcher.calls == []
        assert outcome.placeholder_names_used == 1
        row = next(iter(repo.rows.values()))
        assert row["name_source"] == "inferred_from_domain"
        supplier = repo.suppliers[row["supplier_id"]]
        assert supplier["canonical_name"] == _placeholder_name_from_domain("acmetrailer.com")

    def test_placeholder_row_merges_into_supplier_already_on_that_domain(self):
        repo = FakeRepo()
        existing_id = repo.create_golden_record({"canonical_name": "Existing Co", "domain": "acmetrailer.com"})
        service, _ = _make_service(repo=repo)
        service.run_batch([_row(website="https://acmetrailer.com")], "job-1")
        row = next(iter(repo.rows.values()))
        assert row["supplier_id"] == existing_id

    def test_placeholder_row_creates_new_supplier_when_domain_unseen(self):
        repo = FakeRepo()
        service, _ = _make_service(repo=repo)
        service.run_batch([_row(website="https://brandnew.com")], "job-1")
        assert len(repo.suppliers) == 1


class TestNameExtraction:

    def test_successful_extraction_replaces_placeholder_and_records_provenance(self):
        repo = FakeRepo()

        class CollectionWithPages(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                self.calls.append({"supplier_id": supplier_id, "return_pages": return_pages, "source_url": source_url})
                return {
                    "status": "success", "pages_visited": 1, "error": None,
                    "pages": [FakePage(
                        "https://acmetrailer.com/about",
                        "Acme Trailer Manufacturing Ltd is a factory in Ohio, manufacturing "
                        "trailer axles and chassis components since 1998 for customers across "
                        "North America.",
                    )],
                }

        llm = FakeLLMClient(response={"company_name": "Acme Trailer Manufacturing Ltd"})
        service, _ = _make_service(repo=repo, collection_service=CollectionWithPages(), llm_client=llm)

        outcome = service.run_batch([_row(website="https://acmetrailer.com")], "job-1")

        assert outcome.placeholder_names_replaced == 1
        row = next(iter(repo.rows.values()))
        assert row["company_name"] == "Acme Trailer Manufacturing Ltd"
        assert repo.suppliers[row["supplier_id"]]["canonical_name"] == "Acme Trailer Manufacturing Ltd"

        assert len(repo.provenance) == 1
        prov = repo.provenance[0]
        assert prov["field_name"] == "canonical_name"
        assert prov["value"] == "Acme Trailer Manufacturing Ltd"
        assert prov["source_url"] == "https://acmetrailer.com/about"
        assert prov["source_tier"] == "own_domain"
        assert prov["claim_type"] == "verifiable_fact"
        assert prov["extraction_method"] == "llm_grounded_extraction"

    def test_no_pages_returned_skips_extraction(self):
        repo = FakeRepo()
        collection = FakeCollectionService()
        llm = FakeLLMClient(response={"company_name": "Should Not Be Used"})
        service, _ = _make_service(repo=repo, collection_service=collection, llm_client=llm)
        outcome = service.run_batch([_row(website="https://acmetrailer.com")], "job-1")
        assert outcome.placeholder_names_replaced == 0
        assert llm.calls == []
        row = next(iter(repo.rows.values()))
        supplier = repo.suppliers[row["supplier_id"]]
        assert supplier["canonical_name"] != "Should Not Be Used"

    def test_llm_returns_no_name_leaves_placeholder_in_place(self):
        repo = FakeRepo()

        class CollectionWithPages(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                self.calls.append({"supplier_id": supplier_id, "return_pages": return_pages, "source_url": source_url})
                return {"status": "success", "pages_visited": 1, "error": None,
                        "pages": [FakePage("https://x.com", "not much here")]}

        llm = FakeLLMClient(response={"company_name": None})
        service, _ = _make_service(repo=repo, collection_service=CollectionWithPages(), llm_client=llm)
        outcome = service.run_batch([_row(website="https://acmetrailer.com")], "job-1")
        assert outcome.placeholder_names_replaced == 0
        row = next(iter(repo.rows.values()))
        assert row["company_name"] is None  # batch row's own snapshot untouched

    def test_named_row_never_attempts_name_extraction(self):
        repo = FakeRepo()

        class CollectionWithPages(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                self.calls.append({"supplier_id": supplier_id, "return_pages": return_pages, "source_url": source_url})
                return {"status": "success", "pages_visited": 1, "error": None, "pages": [FakePage("https://x.com", "text")]}

        llm = FakeLLMClient(response={"company_name": "Irrelevant"})
        service, _ = _make_service(repo=repo, collection_service=CollectionWithPages(), llm_client=llm)
        service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")
        assert llm.calls == []

    def test_junk_extracted_name_is_rejected_not_stored(self):
        """The exact bug found via a real calibration run: a bare nginx
        default page confidently 'extracts' the company name 'nginx'."""
        repo = FakeRepo()

        class NginxDefaultPage(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                self.calls.append({"supplier_id": supplier_id, "return_pages": return_pages, "source_url": source_url})
                return {
                    "status": "success", "pages_visited": 1, "error": None,
                    "pages": [FakePage(
                        "http://www.cgpsealing.com",
                        "Welcome to nginx!\nWelcome to nginx!\nIf you see this page, the nginx "
                        "web server is successfully installed and\nworking. Further "
                        "configuration is required.\nFor online documentation and support "
                        "please refer to\nnginx.org\n.\nCommercial support is available at\n"
                        "nginx.com\n.\nThank you for using nginx.",
                    )],
                }

        llm = FakeLLMClient(response={"company_name": "nginx"})
        service, _ = _make_service(repo=repo, collection_service=NginxDefaultPage(), llm_client=llm)

        outcome = service.run_batch([_row(website="http://www.cgpsealing.com")], "job-1")

        assert outcome.placeholder_names_replaced == 0
        assert outcome.placeholder_names_rejected == 1
        row = next(iter(repo.rows.values()))
        supplier = repo.suppliers[row["supplier_id"]]
        assert supplier["canonical_name"] != "nginx"  # placeholder untouched
        assert row["company_name"] is None  # batch row's own snapshot untouched
        assert "rejected" in row["name_extraction_note"]
        assert "nginx" in row["name_extraction_note"]
        assert repo.provenance == []  # nothing recorded for a rejected name

    def test_short_page_text_is_rejected_regardless_of_name(self):
        """A real name-shaped string extracted from an almost-blank page
        is still suspicious -- the page-shape check catches what the
        exact-name blocklist can't enumerate in advance."""
        repo = FakeRepo()

        class AlmostBlankPage(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                self.calls.append({"supplier_id": supplier_id, "return_pages": return_pages, "source_url": source_url})
                return {"status": "success", "pages_visited": 1, "error": None,
                        "pages": [FakePage("https://x.com", "Site Under Maintenance")]}

        llm = FakeLLMClient(response={"company_name": "Acme Trading"})
        service, _ = _make_service(repo=repo, collection_service=AlmostBlankPage(), llm_client=llm)

        outcome = service.run_batch([_row(website="https://x.com")], "job-1")

        assert outcome.placeholder_names_rejected == 1
        row = next(iter(repo.rows.values()))
        supplier = repo.suppliers[row["supplier_id"]]
        assert supplier["canonical_name"] != "Acme Trading"

    def test_extraction_conflicting_with_a_trusted_existing_name_is_not_applied(self):
        """cgpsealing.com's real failure mode: the domain already had a
        trusted name (e.g. from a bulk import) before this batch row
        touched it -- extraction must never overwrite it, but the
        disagreement is still worth recording."""
        repo = FakeRepo()
        supplier_id = repo.create_golden_record({
            "canonical_name": "CGP (Wuhu) Sealing Co., Ltd.", "domain": "cgpsealing.com",
        })

        class CollectionWithPages(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                self.calls.append({"supplier_id": supplier_id, "return_pages": return_pages, "source_url": source_url})
                return {
                    "status": "success", "pages_visited": 1, "error": None,
                    "pages": [FakePage(
                        "http://www.cgpsealing.com",
                        "Some other confusingly-worded page content about sealing products "
                        "and industrial gaskets manufactured for automotive customers.",
                    )],
                }

        llm = FakeLLMClient(response={"company_name": "Wuhu Sealing Group"})
        service, _ = _make_service(repo=repo, collection_service=CollectionWithPages(), llm_client=llm)

        outcome = service.run_batch([_row(website="http://www.cgpsealing.com")], "job-1")

        assert outcome.placeholder_names_replaced == 0
        assert outcome.placeholder_names_conflicting == 1
        assert repo.suppliers[supplier_id]["canonical_name"] == "CGP (Wuhu) Sealing Co., Ltd."  # never overwritten

        row = next(iter(repo.rows.values()))
        assert row["company_name"] is None  # batch row's own snapshot untouched
        assert "CGP (Wuhu) Sealing Co., Ltd." in row["name_extraction_note"]
        assert "Wuhu Sealing Group" in row["name_extraction_note"]

        assert len(repo.provenance) == 1
        prov = repo.provenance[0]
        assert prov["field_name"] == "canonical_name_candidate"  # never "canonical_name"
        assert prov["value"] == "Wuhu Sealing Group"
        assert prov["source_tier"] == "own_domain"
        assert prov["claim_type"] == "verifiable_fact"

    def test_conflicting_name_that_is_also_junk_is_rejected_first(self):
        """Rejection is checked before the trusted-name comparison --
        a junk extraction should never even reach field_provenance as a
        'candidate', trusted-name or not."""
        repo = FakeRepo()
        repo.create_golden_record({"canonical_name": "CGP (Wuhu) Sealing Co., Ltd.", "domain": "cgpsealing.com"})

        class NginxDefaultPage(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                self.calls.append({"supplier_id": supplier_id, "return_pages": return_pages, "source_url": source_url})
                return {"status": "success", "pages_visited": 1, "error": None,
                        "pages": [FakePage("http://www.cgpsealing.com", "Welcome to nginx! " * 5)]}

        llm = FakeLLMClient(response={"company_name": "nginx"})
        service, _ = _make_service(repo=repo, collection_service=NginxDefaultPage(), llm_client=llm)

        outcome = service.run_batch([_row(website="http://www.cgpsealing.com")], "job-1")

        assert outcome.placeholder_names_rejected == 1
        assert outcome.placeholder_names_conflicting == 0
        assert repo.provenance == []


class TestRejectReasonForExtractedName:

    def test_exact_blocklist_match_is_rejected(self):
        assert _reject_reason_for_extracted_name("nginx", "some long page text here " * 5) is not None

    def test_blocklist_match_is_case_and_punctuation_insensitive(self):
        assert _reject_reason_for_extracted_name("Nginx!", "some long page text here " * 5) is not None
        assert _reject_reason_for_extracted_name("  NGINX  ", "some long page text here " * 5) is not None

    def test_name_containing_a_blocklist_phrase_is_not_rejected_on_that_basis(self):
        """The exact bug the user flagged: whole-name match only, never
        substring -- a real company shouldn't be rejected just because
        its name happens to contain 'welcome to'."""
        long_text = "Chiming Auto Lighting is a manufacturer based in Foshan, China. " * 3
        assert _reject_reason_for_extracted_name("Welcome to Chiming", long_text) is None

    def test_real_names_are_never_falsely_rejected(self):
        long_text = "A real company page with plenty of descriptive marketing text. " * 3
        for real_name in (
            "JOST", "BPW Bergische Achsen KG", "Cartek International Co.,Ltd",
            "CGP (Wuhu) Sealing Co., Ltd.", "3G Winnard Ltd.", "Adriauto S.r.l.",
        ):
            assert _reject_reason_for_extracted_name(real_name, long_text) is None, real_name

    def test_parking_page_text_signature_is_rejected_even_with_a_plausible_name(self):
        page_text = (
            "Acme Trading Co\nThis domain is parked free, courtesy of GoDaddy.com. "
            "Would you like to buy this domain?"
        )
        assert _reject_reason_for_extracted_name("Acme Trading Co", page_text) is not None

    def test_short_page_text_is_rejected(self):
        assert _reject_reason_for_extracted_name("Acme Trading Co", "Hello world") is not None

    def test_page_text_over_the_length_floor_with_no_signature_is_not_rejected(self):
        long_text = "This is a perfectly ordinary company homepage with real content. " * 2
        assert _reject_reason_for_extracted_name("Acme Trading Co", long_text) is None


class TestWithinBatchDedup:

    def test_second_row_same_domain_reuses_supplier_and_skips_second_collect(self):
        repo = FakeRepo()
        matcher = FakeMatcher(repo)
        collection = FakeCollectionService()
        service, _ = _make_service(repo=repo, matcher=matcher, collection_service=collection)

        outcome = service.run_batch(
            [
                _row(row_index=0, company_name="Acme Co", website="https://acme.com"),
                _row(row_index=1, company_name="Acme Corp", website="https://acme.com/"),
            ],
            "job-1",
        )
        assert len(matcher.calls) == 1  # resolve_and_store only called once
        assert len(collection.calls) == 1  # no redundant real fetch for the repeat
        assert outcome.succeeded == 2
        rows = list(repo.rows.values())
        assert rows[0]["supplier_id"] == rows[1]["supplier_id"]
        assert rows[1]["status"] == "success"

    def test_repeat_domain_after_failed_first_collect_marks_second_row_failed_too(self):
        repo = FakeRepo()
        matcher = FakeMatcher(repo)
        collection = FakeCollectionService()
        service, _ = _make_service(repo=repo, matcher=matcher, collection_service=collection)
        # We don't know the supplier_id ahead of time (matcher assigns
        # it), so script failure for supplier id 1 -- the first (and
        # only, given create_golden_record starts at 1) id this batch
        # will create.
        collection.results[1] = {"status": "failed", "error": "timeout", "pages_visited": 0}

        outcome = service.run_batch(
            [
                _row(row_index=0, company_name="Acme Co", website="https://acme.com"),
                _row(row_index=1, company_name="Acme Corp", website="https://acme.com"),
            ],
            "job-1",
        )
        assert len(collection.calls) == 1
        assert outcome.failed == 2
        rows = list(repo.rows.values())
        assert rows[1]["status"] == "failed"
        assert rows[1]["error_message"] == "timeout"

    def test_placeholder_repeat_domain_skips_second_domain_lookup_via_cache(self):
        repo = FakeRepo()
        collection = FakeCollectionService()
        service, _ = _make_service(repo=repo, collection_service=collection)
        outcome = service.run_batch(
            [
                _row(row_index=0, website="https://acmetrailer.com"),
                _row(row_index=1, website="https://acmetrailer.com"),
            ],
            "job-1",
        )
        assert len(repo.suppliers) == 1
        assert len(collection.calls) == 1
        assert outcome.placeholder_names_used == 2


class TestProgressCallback:

    def test_progress_callback_invoked_once_per_row(self):
        service, _ = _make_service()
        seen = []
        service.run_batch(
            [_row(row_index=0, company_name="A"), _row(row_index=1)],
            "job-1", progress_callback=lambda o: seen.append(o.processed + o.needs_url),
        )
        assert len(seen) == 2

    def test_progress_callback_exception_does_not_abort_batch(self):
        service, _ = _make_service()

        def exploding_callback(outcome):
            raise RuntimeError("ui glitch")

        outcome = service.run_batch(
            [_row(row_index=0, company_name="A"), _row(row_index=1, company_name="B")],
            "job-1", progress_callback=exploding_callback,
        )
        assert outcome.total_rows == 2
        assert outcome.needs_url == 2


class TestPlaceholderNameFromDomain:

    def test_strips_tld_and_titlecases(self):
        assert _placeholder_name_from_domain("acmetrailer.com") == "Acmetrailer"

    def test_hyphens_and_underscores_become_spaces(self):
        assert _placeholder_name_from_domain("acme-trailer_parts.co.uk") == "Acme Trailer Parts"

    def test_empty_domain_does_not_raise(self):
        assert _placeholder_name_from_domain("") == ""
