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

from batch.batch_service import BatchService, _placeholder_name_from_domain
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
    def find_by_domain(self, domain):
        for s in self.suppliers.values():
            if s.get("domain") == domain:
                return dict(s)
        return None

    def merge_into_golden(self, supplier_id, supplier_data):
        self.suppliers[supplier_id].update(supplier_data)

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

    def collect(self, supplier_id, return_pages=False):
        self.calls.append({"supplier_id": supplier_id, "return_pages": return_pages})
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
            def collect(self, supplier_id, return_pages=False):
                self.calls.append({"supplier_id": supplier_id, "return_pages": return_pages})
                return {
                    "status": "success", "pages_visited": 1, "error": None,
                    "pages": [FakePage("https://acmetrailer.com/about",
                                        "Acme Trailer Manufacturing Ltd is a factory in Ohio.")],
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
            def collect(self, supplier_id, return_pages=False):
                self.calls.append({"supplier_id": supplier_id, "return_pages": return_pages})
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
            def collect(self, supplier_id, return_pages=False):
                self.calls.append({"supplier_id": supplier_id, "return_pages": return_pages})
                return {"status": "success", "pages_visited": 1, "error": None, "pages": [FakePage("https://x.com", "text")]}

        llm = FakeLLMClient(response={"company_name": "Irrelevant"})
        service, _ = _make_service(repo=repo, collection_service=CollectionWithPages(), llm_client=llm)
        service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")
        assert llm.calls == []


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
