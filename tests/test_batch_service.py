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
from discovery.candidate_extractor import Candidate
from discovery.candidate_validator import ValidationResult
from scrapers.base_scraper import ScraperResult


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
        self.reputation_snippets: Dict[int, List[Dict[str, Any]]] = {}

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

    # -- supplier_reputation_snippets ------------------------------------
    def save_reputation_snippets(self, supplier_id, snippets):
        if not snippets:
            return 0
        self.reputation_snippets.setdefault(supplier_id, [])
        inserted = 0
        for s in snippets:
            key = (s["query_type"], s.get("link"))
            existing_keys = {(row["query_type"], row.get("link")) for row in self.reputation_snippets[supplier_id]}
            if key in existing_keys:
                continue
            self.reputation_snippets[supplier_id].append(dict(s))
            inserted += 1
        return inserted

    def get_reputation_snippets(self, supplier_id, query_type=None):
        rows = self.reputation_snippets.get(supplier_id, [])
        if query_type:
            return [r for r in rows if r["query_type"] == query_type]
        return list(rows)

    def mark_reputation_search_attempted(self, supplier_id):
        self.suppliers.setdefault(supplier_id, {"id": supplier_id})["reputation_search_attempted_at"] = "2024-01-01T00:00:00+00:00"


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
    def __init__(self, url, text, footer_text="", facility_photo_urls=None):
        self.url = url
        self.text = text
        self.footer_text = footer_text
        self.facility_photo_urls = facility_photo_urls or []


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
    for placeholder-name, address, and factory-location extraction.

    Calls are routed by system_prompt: anything whose prompt asks for
    "factory_location" (batch_service.FACTORY_LOCATION_EXTRACTION_SYSTEM_PROMPT
    is the only such prompt) is answered from factory_location_response /
    factory_location_responses; everything else (name + address
    extraction) is answered from response / responses -- exactly
    mirroring how run_batch fires both an address AND a factory-location
    pass over the same candidate pages for every collected row, using
    two independently-scripted response tracks so a test for one
    extraction doesn't have to account for the other's calls.

    `response` (or `factory_location_response`) is returned for every
    call on its track unless the corresponding `responses`/
    `factory_location_responses` list is given, in which case each call
    on that track consumes the next entry in order -- for tests
    exercising a track's multi-tier fallback (one LLM call per tier)."""

    def __init__(
        self, response: Any = None, responses: Optional[List[Any]] = None,
        factory_location_response: Any = None, factory_location_responses: Optional[List[Any]] = None,
    ):
        self.response = response
        self._responses = iter(responses) if responses is not None else None
        self.factory_location_response = (
            {"factory_location": None} if factory_location_response is None else factory_location_response
        )
        self._factory_location_responses = (
            iter(factory_location_responses) if factory_location_responses is not None else None
        )
        self.calls: List[Dict[str, Any]] = []

    def complete_json(self, system_prompt, user_prompt, **kwargs):
        self.calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt})
        if "factory_location" in system_prompt:
            if self._factory_location_responses is not None:
                return next(self._factory_location_responses)
            return self.factory_location_response
        if self._responses is not None:
            return next(self._responses)
        return self.response


class FakeGoogleScraper:
    """Stand-in for scrapers.google_search_scraper.GoogleSearchScraper --
    only scrape() is used, for reputation search. `results_by_query`
    maps the exact query text BatchService will construct (e.g.
    "Acme Co scam") to a list of ScraperResult; a query with no entry
    returns an empty list (mirrors a real zero-result search, not an
    error)."""

    def __init__(self, results_by_query: Optional[Dict[str, List[ScraperResult]]] = None):
        self.results_by_query = results_by_query or {}
        self.calls: List[Dict[str, Any]] = []

    def scrape(self, query, max_results=20, site_filter=None, **kwargs):
        self.calls.append({"query": query, "max_results": max_results})
        return self.results_by_query.get(query, [])


class FakeCandidateValidator:
    """Stand-in for discovery.candidate_validator.CandidateValidator --
    only recover() is used by BatchService. `recovery_result` (a
    ValidationResult or None) is what recover() returns unconditionally;
    these tests don't need a real search+extract_candidates+validate
    round trip (that's covered by test_discovery_candidate_validator.py),
    just a fixed outcome to assert BatchService's own wiring around it."""

    def __init__(self, recovery_result=None, raises=False):
        self._recovery_result = recovery_result
        self._raises = raises
        self.recover_calls: List[Dict[str, Any]] = []

    def recover(self, company_name, product_term, google_scraper, country=None, max_candidates=2,
                existing_country=None):
        self.recover_calls.append({
            "company_name": company_name, "product_term": product_term, "max_candidates": max_candidates,
            "existing_country": existing_country,
        })
        if self._raises:
            raise RuntimeError("recovery exploded")
        return self._recovery_result


def _recovered_result(domain, title="Acme Trailer Co"):
    candidate = Candidate(title=title, link=f"https://{domain}/", snippet="", domain=domain)
    return ValidationResult(candidate, True, title, None, 90.0, "validated: name corroborated (score=90)")


def _reputation_result(title, link, snippet):
    return ScraperResult(
        source="google", source_id=link,
        raw_data={"title": title, "link": link, "snippet": snippet, "displayed_link": link},
        success=True,
    )


def _row(row_index=0, company_name=None, website=None, original_columns=None, country=None, product_keywords=None):
    return ParsedRow(
        row_index=row_index, original_columns=original_columns or {},
        company_name=company_name, website=website, country=country,
        product_keywords=product_keywords,
    )


def _make_service(repo=None, matcher=None, collection_service=None, llm_client=None, google_scraper=None,
                   candidate_validator=None):
    repo = repo or FakeRepo()
    return BatchService(
        repo=repo,
        matcher=matcher or FakeMatcher(repo),
        collection_service=collection_service or FakeCollectionService(),
        llm_client=llm_client or FakeLLMClient(),
        google_scraper=google_scraper or FakeGoogleScraper(),
        candidate_validator=candidate_validator,
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

    def test_csv_country_column_is_trusted_directly_onto_a_new_supplier(self):
        """The CSV's own Country column is the uploader's own data, not
        a scraped extraction -- same trust status as Company Name
        already has for canonical_name. Threaded into the matcher
        candidate so a brand-new supplier gets it set directly."""
        repo = FakeRepo()
        matcher = FakeMatcher(repo)
        service, _ = _make_service(repo=repo, matcher=matcher)
        service.run_batch(
            [_row(company_name="Acme Co", website="https://acme.com", country="United Kingdom")], "job-1",
        )
        assert matcher.calls[0]["country"] == "United Kingdom"
        supplier = next(iter(repo.suppliers.values()))
        assert supplier["country"] == "United Kingdom"

    def test_csv_country_does_not_overwrite_an_existing_suppliers_country(self):
        """Real case this guards against: a batch upload for a company
        already in the DB (matched by domain) must not let a CSV's
        Country column silently clobber an already-known, possibly
        more specific fact (e.g. an existing "China" from an earlier
        scrape overwritten by a CSV's coarser "Asia") -- same
        trusted-value-guard discipline as every other merge field."""
        repo = FakeRepo()
        repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com", "country": "China"})
        matcher = FakeMatcher(repo)
        service, _ = _make_service(repo=repo, matcher=matcher)
        service.run_batch(
            [_row(company_name="Acme Co", website="https://acme.com", country="Asia")], "job-1",
        )
        supplier = next(iter(repo.suppliers.values()))
        assert supplier["country"] == "China"

    def test_no_country_column_leaves_behaviour_unchanged(self):
        repo = FakeRepo()
        matcher = FakeMatcher(repo)
        service, _ = _make_service(repo=repo, matcher=matcher)
        service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")
        assert matcher.calls[0]["country"] is None
        supplier = next(iter(repo.suppliers.values()))
        assert supplier.get("country") is None

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
        assert outcome.processed == outcome.succeeded + outcome.failed
        for row in repo.rows.values():
            assert row["status"] == "failed"
            assert "dedup blew up" in row["error_message"]


class TestProgressReconciliation:
    """`processed` must always equal `succeeded + failed` so the UI's
    "X/total (Y succeeded, Z failed)" progress line reconciles -- a row
    that never reaches processed (needs_url) must never count toward
    succeeded/failed either, and every row that does count toward
    succeeded/failed must have incremented processed first, regardless
    of which of the several failure branches it exits through."""

    def test_processed_reconciles_across_every_outcome_branch(self):
        class ExplodingMatcher(FakeMatcher):
            def resolve_and_store(self, candidate):
                if candidate.get("canonical_name") == "Explode Co":
                    raise RuntimeError("dedup blew up")
                return super().resolve_and_store(candidate)

        repo = FakeRepo()
        matcher = ExplodingMatcher(repo)
        service, _ = _make_service(repo=repo, matcher=matcher)

        outcome = service.run_batch(
            [
                _row(row_index=0, company_name="No Website Co", website=None),  # needs_url
                _row(row_index=1, company_name="Marketplace Co", website="https://www.alibaba.com"),  # failed, pre-resolve
                _row(row_index=2, company_name="Explode Co", website="https://explode.com"),  # failed, resolve exception
                _row(row_index=3, company_name="Real Co", website="https://real.com"),  # succeeded
            ],
            "job-1",
        )

        assert outcome.needs_url == 1
        assert outcome.processed == outcome.succeeded + outcome.failed
        assert outcome.processed == 3  # marketplace + explode + real, not the needs_url row
        assert outcome.succeeded == 1
        assert outcome.failed == 2


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

    def test_placeholder_row_csv_country_is_trusted_onto_a_new_supplier(self):
        repo = FakeRepo()
        service, _ = _make_service(repo=repo)
        service.run_batch([_row(website="https://brandnew.com", country="France")], "job-1")
        supplier = next(iter(repo.suppliers.values()))
        assert supplier["country"] == "France"

    def test_placeholder_row_csv_country_does_not_overwrite_existing(self):
        repo = FakeRepo()
        existing_id = repo.create_golden_record({
            "canonical_name": "Existing Co", "domain": "acmetrailer.com", "country": "Germany",
        })
        service, _ = _make_service(repo=repo)
        service.run_batch([_row(website="https://acmetrailer.com", country="France")], "job-1")
        assert repo.suppliers[existing_id]["country"] == "Germany"


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


class TestAddressExtraction:

    def test_applied_when_supplier_has_no_existing_address(self):
        repo = FakeRepo()
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})

        class ContactPageCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                self.calls.append({"supplier_id": supplier_id})
                return {
                    "status": "success", "pages_visited": 1, "error": None,
                    "pages": [FakePage(
                        "https://acme.com/contact",
                        "Contact us at Acme Co, 1 Main St, Springfield, IL 62704, USA. "
                        "We'd love to hear about your project.",
                    )],
                }

        llm = FakeLLMClient(response={"address": "1 Main St, Springfield, IL 62704, USA"})
        service, _ = _make_service(repo=repo, collection_service=ContactPageCollection(), llm_client=llm)

        outcome = service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")

        assert outcome.addresses_found == 1
        assert outcome.addresses_conflicting == 0
        assert repo.suppliers[supplier_id]["address"] == "1 Main St, Springfield, IL 62704, USA"

        prov = [p for p in repo.provenance if p["field_name"] == "address"]
        assert len(prov) == 1
        assert prov[0]["value"] == "1 Main St, Springfield, IL 62704, USA"
        assert prov[0]["source_url"] == "https://acme.com/contact"
        assert prov[0]["source_tier"] == "own_domain"
        assert prov[0]["claim_type"] == "verifiable_fact"

    def test_partial_address_is_stored_exactly_as_extracted_not_completed(self):
        """Explicit requirement: a city-only extraction must be stored
        as-is, never padded out with an invented street/postcode."""
        repo = FakeRepo()
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})

        class ContactPageCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                return {"status": "success", "pages_visited": 1, "error": None,
                        "pages": [FakePage(
                            "https://acme.com/contact",
                            "Get in touch with our sales team for a quote. We are based in "
                            "Foshan, China, and ship worldwide. Reach out any time.",
                        )]}

        llm = FakeLLMClient(response={"address": "Foshan, China"})
        service, _ = _make_service(repo=repo, collection_service=ContactPageCollection(), llm_client=llm)

        service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")

        assert repo.suppliers[supplier_id]["address"] == "Foshan, China"

    def test_conflicting_when_supplier_already_has_an_address(self):
        repo = FakeRepo()
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme Co", "domain": "acme.com",
            "address": "Existing Trusted Address, Springfield, IL",
        })

        class ContactPageCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                return {"status": "success", "pages_visited": 1, "error": None,
                        "pages": [FakePage(
                            "https://acme.com/contact",
                            "Get in touch with our team. Contact: 99 Other St, Nowhere, USA. "
                            "We respond to all enquiries within one business day.",
                        )]}

        llm = FakeLLMClient(response={"address": "99 Other St, Nowhere, USA"})
        service, _ = _make_service(repo=repo, collection_service=ContactPageCollection(), llm_client=llm)

        outcome = service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")

        assert outcome.addresses_found == 0
        assert outcome.addresses_conflicting == 1
        assert repo.suppliers[supplier_id]["address"] == "Existing Trusted Address, Springfield, IL"  # untouched

        prov = [p for p in repo.provenance if p["field_name"] == "address_candidate"]
        assert len(prov) == 1
        assert prov[0]["value"] == "99 Other St, Nowhere, USA"

    def test_falls_through_tiers_when_contact_page_has_no_address(self):
        repo = FakeRepo()
        repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})

        class MultiTierCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                return {
                    "status": "success", "pages_visited": 2, "error": None,
                    "pages": [
                        FakePage(
                            "https://acme.com/contact",
                            "Send us a message using the form below and a member of our team "
                            "will get back to you as soon as possible.",
                        ),
                        FakePage(
                            "https://acme.com/imprint",
                            "Impressum: Acme Co GmbH, 5 Impressum Str, Berlin, Germany. "
                            "Registered at the local court, VAT ID as shown on invoices.",
                        ),
                    ],
                }

        # The LLM is asked twice (once per tier); no address on the
        # contact page, a real one on the impressum page.
        llm = FakeLLMClient(responses=[{"address": None}, {"address": "5 Impressum Str, Berlin, Germany"}])

        service, _ = _make_service(repo=repo, collection_service=MultiTierCollection(), llm_client=llm)
        outcome = service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")

        assert outcome.addresses_found == 1
        address_calls = [c for c in llm.calls if "factory_location" not in c["system_prompt"]]
        assert len(address_calls) == 2
        supplier_id = next(iter(repo.suppliers.keys()))
        assert repo.suppliers[supplier_id]["address"] == "5 Impressum Str, Berlin, Germany"

    def test_parking_page_candidate_is_skipped_not_extracted_from(self):
        repo = FakeRepo()
        repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})

        class ParkingPageCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                return {"status": "success", "pages_visited": 1, "error": None,
                        "pages": [FakePage("https://acme.com/contact",
                                            "This domain is parked. Buy this domain from GoDaddy.com.")]}

        llm = FakeLLMClient(response={"address": "Should Not Be Used 123"})
        service, _ = _make_service(repo=repo, collection_service=ParkingPageCollection(), llm_client=llm)

        outcome = service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")

        assert outcome.addresses_found == 0
        assert llm.calls == []  # never even asked -- filtered before the LLM call

    def test_no_candidate_pages_at_all_is_skipped(self):
        repo = FakeRepo()
        repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})

        class NoRelevantPagesCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                return {"status": "success", "pages_visited": 1, "error": None,
                        "pages": [FakePage("https://acme.com/products", "our great products")]}

        llm = FakeLLMClient(response={"address": "Should Not Be Used"})
        service, _ = _make_service(repo=repo, collection_service=NoRelevantPagesCollection(), llm_client=llm)

        outcome = service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")

        assert outcome.addresses_found == 0
        assert llm.calls == []

    def test_address_extraction_runs_for_placeholder_rows_too(self):
        """Not gated by name_source -- a URL-only row needs an address
        extracted just as much as a named one."""
        repo = FakeRepo()

        class ContactPageCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                return {
                    "status": "success", "pages_visited": 1, "error": None,
                    "pages": [
                        FakePage("https://acmetrailer.com/", "not much on the homepage, just a hero banner"),
                        FakePage(
                            "https://acmetrailer.com/contact",
                            "Give us a call or stop by our facility. Acme Trailer, 1 Depot Rd, "
                            "Ohio, USA. Open Monday through Friday.",
                        ),
                    ],
                }

        llm = FakeLLMClient(response={"address": "1 Depot Rd, Ohio, USA"})
        service, _ = _make_service(repo=repo, collection_service=ContactPageCollection(), llm_client=llm)

        outcome = service.run_batch([_row(website="https://acmetrailer.com")], "job-1")

        assert outcome.addresses_found == 1
        supplier_id = next(iter(repo.suppliers.keys()))
        assert repo.suppliers[supplier_id]["address"] == "1 Depot Rd, Ohio, USA"

    def test_falls_through_to_about_page_when_no_other_tier_has_an_address(self):
        """The 4th, lowest-priority tier -- see verification.
        address_extractor.address_candidate_sources' own docstring for
        why it's tried last."""
        repo = FakeRepo()
        repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})

        class AboutPageCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                return {
                    "status": "success", "pages_visited": 1, "error": None,
                    "pages": [FakePage(
                        "https://acme.com/about-us",
                        "Acme Co was founded in 1990 and has grown into a leading manufacturer. "
                        "Our head office is located at 1 Main St, Berlin, Germany.",
                    )],
                }

        llm = FakeLLMClient(response={"address": "1 Main St, Berlin, Germany"})
        service, _ = _make_service(repo=repo, collection_service=AboutPageCollection(), llm_client=llm)

        outcome = service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")

        assert outcome.addresses_found == 1
        supplier_id = next(iter(repo.suppliers.keys()))
        assert repo.suppliers[supplier_id]["address"] == "1 Main St, Berlin, Germany"
        prov = [p for p in repo.provenance if p["field_name"] == "address"]
        assert prov[0]["source_url"] == "https://acme.com/about-us"


class TestFactoryLocationExtraction:
    """Mirrors TestAddressExtraction's cases exactly -- same tiered
    candidate sources, same trusted-value guard -- but on the
    factory_location field, using FakeLLMClient's separate
    factory_location_response track so these tests don't have to
    script an address response too."""

    def test_applied_when_supplier_has_no_existing_factory_location(self):
        repo = FakeRepo()
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})

        class ContactPageCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                return {
                    "status": "success", "pages_visited": 1, "error": None,
                    "pages": [FakePage(
                        "https://acme.com/contact",
                        "Our 50,000 sq ft manufacturing facility is located in Foshan, China. "
                        "Contact us any time for a quote.",
                    )],
                }

        llm = FakeLLMClient(factory_location_response={"factory_location": "Foshan, China"})
        service, _ = _make_service(repo=repo, collection_service=ContactPageCollection(), llm_client=llm)

        outcome = service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")

        assert outcome.factory_locations_found == 1
        assert outcome.factory_locations_conflicting == 0
        assert repo.suppliers[supplier_id]["factory_location"] == "Foshan, China"

        prov = [p for p in repo.provenance if p["field_name"] == "factory_location"]
        assert len(prov) == 1
        assert prov[0]["value"] == "Foshan, China"
        assert prov[0]["source_url"] == "https://acme.com/contact"
        assert prov[0]["source_tier"] == "own_domain"
        assert prov[0]["claim_type"] == "verifiable_fact"

    def test_partial_factory_location_is_stored_exactly_as_extracted_not_completed(self):
        repo = FakeRepo()
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})

        class ContactPageCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                return {"status": "success", "pages_visited": 1, "error": None,
                        "pages": [FakePage(
                            "https://acme.com/contact",
                            "Production takes place at our plant in Shenzhen. Reach out any time "
                            "for a quote on your project.",
                        )]}

        llm = FakeLLMClient(factory_location_response={"factory_location": "Shenzhen"})
        service, _ = _make_service(repo=repo, collection_service=ContactPageCollection(), llm_client=llm)

        service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")

        assert repo.suppliers[supplier_id]["factory_location"] == "Shenzhen"

    def test_conflicting_when_supplier_already_has_a_factory_location(self):
        repo = FakeRepo()
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme Co", "domain": "acme.com",
            "factory_location": "Existing Trusted Factory Location, Springfield, IL",
        })

        class ContactPageCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                return {"status": "success", "pages_visited": 1, "error": None,
                        "pages": [FakePage(
                            "https://acme.com/contact",
                            "Our factory is located in Nowhere, USA. We respond to all enquiries "
                            "within one business day.",
                        )]}

        llm = FakeLLMClient(factory_location_response={"factory_location": "Nowhere, USA"})
        service, _ = _make_service(repo=repo, collection_service=ContactPageCollection(), llm_client=llm)

        outcome = service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")

        assert outcome.factory_locations_found == 0
        assert outcome.factory_locations_conflicting == 1
        assert repo.suppliers[supplier_id]["factory_location"] == "Existing Trusted Factory Location, Springfield, IL"

        prov = [p for p in repo.provenance if p["field_name"] == "factory_location_candidate"]
        assert len(prov) == 1
        assert prov[0]["value"] == "Nowhere, USA"

    def test_falls_through_tiers_when_contact_page_has_no_factory_location(self):
        repo = FakeRepo()
        repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})

        class MultiTierCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                return {
                    "status": "success", "pages_visited": 2, "error": None,
                    "pages": [
                        FakePage(
                            "https://acme.com/contact",
                            "Send us a message using the form below and a member of our team "
                            "will get back to you as soon as possible.",
                        ),
                        FakePage(
                            "https://acme.com/imprint",
                            "Impressum: Acme Co GmbH. Our production facility spans 20,000 sqm "
                            "in Berlin, Germany. Registered at the local court.",
                        ),
                    ],
                }

        llm = FakeLLMClient(
            factory_location_responses=[{"factory_location": None}, {"factory_location": "Berlin, Germany"}],
        )
        service, _ = _make_service(repo=repo, collection_service=MultiTierCollection(), llm_client=llm)
        outcome = service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")

        assert outcome.factory_locations_found == 1
        factory_location_calls = [c for c in llm.calls if "factory_location" in c["system_prompt"]]
        assert len(factory_location_calls) == 2
        supplier_id = next(iter(repo.suppliers.keys()))
        assert repo.suppliers[supplier_id]["factory_location"] == "Berlin, Germany"

    def test_parking_page_candidate_is_skipped_not_extracted_from(self):
        repo = FakeRepo()
        repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})

        class ParkingPageCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                return {"status": "success", "pages_visited": 1, "error": None,
                        "pages": [FakePage("https://acme.com/contact",
                                            "This domain is parked. Buy this domain from GoDaddy.com.")]}

        llm = FakeLLMClient(factory_location_response={"factory_location": "Should Not Be Used"})
        service, _ = _make_service(repo=repo, collection_service=ParkingPageCollection(), llm_client=llm)

        outcome = service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")

        assert outcome.factory_locations_found == 0
        factory_location_calls = [c for c in llm.calls if "factory_location" in c["system_prompt"]]
        assert factory_location_calls == []  # never even asked -- filtered before the LLM call

    def test_no_candidate_pages_at_all_is_skipped(self):
        repo = FakeRepo()
        repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})

        class NoRelevantPagesCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                return {"status": "success", "pages_visited": 1, "error": None,
                        "pages": [FakePage("https://acme.com/products", "our great products")]}

        llm = FakeLLMClient(factory_location_response={"factory_location": "Should Not Be Used"})
        service, _ = _make_service(repo=repo, collection_service=NoRelevantPagesCollection(), llm_client=llm)

        outcome = service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")

        assert outcome.factory_locations_found == 0
        factory_location_calls = [c for c in llm.calls if "factory_location" in c["system_prompt"]]
        assert factory_location_calls == []

    def test_factory_location_extraction_runs_for_placeholder_rows_too(self):
        repo = FakeRepo()

        class ContactPageCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                return {
                    "status": "success", "pages_visited": 1, "error": None,
                    "pages": [
                        FakePage("https://acmetrailer.com/", "not much on the homepage, just a hero banner"),
                        FakePage(
                            "https://acmetrailer.com/contact",
                            "Our factory is located at 1 Depot Rd, Ohio, USA. Open Monday "
                            "through Friday.",
                        ),
                    ],
                }

        llm = FakeLLMClient(factory_location_response={"factory_location": "1 Depot Rd, Ohio, USA"})
        service, _ = _make_service(repo=repo, collection_service=ContactPageCollection(), llm_client=llm)

        outcome = service.run_batch([_row(website="https://acmetrailer.com")], "job-1")

        assert outcome.factory_locations_found == 1
        supplier_id = next(iter(repo.suppliers.keys()))
        assert repo.suppliers[supplier_id]["factory_location"] == "1 Depot Rd, Ohio, USA"

    def test_address_and_factory_location_are_independently_extracted_from_the_same_page(self):
        """The two extractions run over the same candidate pages but
        are entirely separate fields -- a page can yield both, one, or
        neither, and one must never be used to fill in the other."""
        repo = FakeRepo()
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})

        class ContactPageCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                return {"status": "success", "pages_visited": 1, "error": None,
                        "pages": [FakePage(
                            "https://acme.com/contact",
                            "Registered office: 1 Main St, Springfield, IL, USA. Our production "
                            "facility is located in Foshan, China.",
                        )]}

        llm = FakeLLMClient(
            response={"address": "1 Main St, Springfield, IL, USA"},
            factory_location_response={"factory_location": "Foshan, China"},
        )
        service, _ = _make_service(repo=repo, collection_service=ContactPageCollection(), llm_client=llm)

        outcome = service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")

        assert outcome.addresses_found == 1
        assert outcome.factory_locations_found == 1
        assert repo.suppliers[supplier_id]["address"] == "1 Main St, Springfield, IL, USA"
        assert repo.suppliers[supplier_id]["factory_location"] == "Foshan, China"

    def test_falls_through_to_about_page_when_no_other_tier_has_a_factory_location(self):
        repo = FakeRepo()
        repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})

        class AboutPageCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                return {
                    "status": "success", "pages_visited": 1, "error": None,
                    "pages": [FakePage(
                        "https://acme.com/about-us",
                        "Acme Co was founded in 1990. Our factory is located in Foshan, China, "
                        "where we manufacture all our products in-house.",
                    )],
                }

        llm = FakeLLMClient(factory_location_response={"factory_location": "Foshan, China"})
        service, _ = _make_service(repo=repo, collection_service=AboutPageCollection(), llm_client=llm)

        outcome = service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")

        assert outcome.factory_locations_found == 1
        supplier_id = next(iter(repo.suppliers.keys()))
        assert repo.suppliers[supplier_id]["factory_location"] == "Foshan, China"
        prov = [p for p in repo.provenance if p["field_name"] == "factory_location"]
        assert prov[0]["source_url"] == "https://acme.com/about-us"


class TestFacilityPhotoAggregation:

    def test_new_urls_are_written_to_candidate_facility_photo_urls(self):
        repo = FakeRepo()
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})

        class PhotoCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                return {
                    "status": "success", "pages_visited": 1, "error": None,
                    "pages": [FakePage(
                        "https://acme.com/about", "our factory floor",
                        facility_photo_urls=["https://acme.com/img/factory1.jpg", "https://acme.com/img/factory2.jpg"],
                    )],
                }

        service, _ = _make_service(repo=repo, collection_service=PhotoCollection())
        outcome = service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")

        assert outcome.facility_photos_found == 1
        assert repo.suppliers[supplier_id]["candidate_facility_photo_urls"] == [
            "https://acme.com/img/factory1.jpg", "https://acme.com/img/factory2.jpg",
        ]

    def test_urls_are_unioned_across_pages_and_deduplicated(self):
        repo = FakeRepo()
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})

        class PhotoCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                return {
                    "status": "success", "pages_visited": 2, "error": None,
                    "pages": [
                        FakePage("https://acme.com/about", "text",
                                 facility_photo_urls=["https://acme.com/img/factory1.jpg"]),
                        FakePage("https://acme.com/facility", "text",
                                 facility_photo_urls=["https://acme.com/img/factory1.jpg", "https://acme.com/img/factory2.jpg"]),
                    ],
                }

        service, _ = _make_service(repo=repo, collection_service=PhotoCollection())
        service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")

        assert repo.suppliers[supplier_id]["candidate_facility_photo_urls"] == [
            "https://acme.com/img/factory1.jpg", "https://acme.com/img/factory2.jpg",
        ]

    def test_accumulates_onto_existing_candidates_rather_than_overwriting(self):
        """Unlike address/factory_location, this is additive, not
        gap-fill-once -- a supplier that already has candidate photos
        from an earlier run keeps them, and new ones found this run are
        appended, not blocked by a trusted-value guard."""
        repo = FakeRepo()
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme Co", "domain": "acme.com",
            "candidate_facility_photo_urls": ["https://acme.com/img/old.jpg"],
        })

        class PhotoCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                return {"status": "success", "pages_visited": 1, "error": None,
                        "pages": [FakePage("https://acme.com/about", "text",
                                            facility_photo_urls=["https://acme.com/img/new.jpg"])]}

        service, _ = _make_service(repo=repo, collection_service=PhotoCollection())
        outcome = service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")

        assert outcome.facility_photos_found == 1
        assert repo.suppliers[supplier_id]["candidate_facility_photo_urls"] == [
            "https://acme.com/img/old.jpg", "https://acme.com/img/new.jpg",
        ]

    def test_no_new_urls_does_not_count_as_found_and_does_not_write(self):
        repo = FakeRepo()
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme Co", "domain": "acme.com",
            "candidate_facility_photo_urls": ["https://acme.com/img/old.jpg"],
        })

        class PhotoCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                return {"status": "success", "pages_visited": 1, "error": None,
                        "pages": [FakePage("https://acme.com/about", "text",
                                            facility_photo_urls=["https://acme.com/img/old.jpg"])]}

        service, _ = _make_service(repo=repo, collection_service=PhotoCollection())
        outcome = service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")

        assert outcome.facility_photos_found == 0
        history_calls = [c for c in repo.history_calls if "candidate_facility_photo_urls" in c["fields"]]
        assert history_calls == []

    def test_no_pages_with_facility_photos_is_skipped(self):
        repo = FakeRepo()
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})

        class NoPhotoCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                return {"status": "success", "pages_visited": 1, "error": None,
                        "pages": [FakePage("https://acme.com/about", "no photos here")]}

        service, _ = _make_service(repo=repo, collection_service=NoPhotoCollection())
        outcome = service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")

        assert outcome.facility_photos_found == 0
        assert "candidate_facility_photo_urls" not in repo.suppliers[supplier_id]

    def test_capped_at_the_per_supplier_maximum(self):
        repo = FakeRepo()
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})
        many_urls = [f"https://acme.com/img/{i}.jpg" for i in range(15)]

        class PhotoCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                return {"status": "success", "pages_visited": 1, "error": None,
                        "pages": [FakePage("https://acme.com/about", "text", facility_photo_urls=many_urls)]}

        service, _ = _make_service(repo=repo, collection_service=PhotoCollection())
        service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")

        assert len(repo.suppliers[supplier_id]["candidate_facility_photo_urls"]) == 10


class TestReputationSearch:
    """search_reputation is opt-in and off by default -- these tests
    always pass search_reputation=True explicitly. No test here ever
    asserts a Clean/Flagged/Pass/Fail value anywhere: the feature
    stores raw snippets only."""

    def test_disabled_by_default_no_searches_run(self):
        repo = FakeRepo()
        repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})
        scraper = FakeGoogleScraper()
        service, _ = _make_service(repo=repo, google_scraper=scraper)

        service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1")

        assert scraper.calls == []

    def test_three_searches_run_and_snippets_saved_when_enabled(self):
        repo = FakeRepo()
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})
        scraper = FakeGoogleScraper(results_by_query={
            "Acme Co scam": [_reputation_result("Is Acme Co a scam?", "https://forum.example.com/1", "No evidence found...")],
            "Acme Co review": [_reputation_result("Acme Co reviews", "https://reviews.example.com/1", "4.5 stars...")],
            "Acme Co factory tour": [_reputation_result("Acme Co factory tour video", "https://youtube.example.com/1", "Watch our tour...")],
        })
        service, _ = _make_service(repo=repo, google_scraper=scraper)

        outcome = service.run_batch(
            [_row(company_name="Acme Co", website="https://acme.com")], "job-1", search_reputation=True,
        )

        assert outcome.reputation_snippets_found == 1  # row-level counter, not snippet count
        queries = {c["query"] for c in scraper.calls}
        assert queries == {"Acme Co scam", "Acme Co review", "Acme Co factory tour"}

        saved = repo.get_reputation_snippets(supplier_id)
        assert len(saved) == 3
        by_type = {s["query_type"]: s for s in saved}
        assert by_type["scam"]["link"] == "https://forum.example.com/1"
        assert by_type["scam"]["snippet"] == "No evidence found..."
        assert by_type["review"]["link"] == "https://reviews.example.com/1"
        assert by_type["factory_tour"]["link"] == "https://youtube.example.com/1"

    def test_no_verdict_field_is_ever_written(self):
        """Explicit requirement: nothing about this feature computes or
        stores a Clean/Flagged judgment -- only raw search results."""
        repo = FakeRepo()
        repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})
        scraper = FakeGoogleScraper(results_by_query={
            "Acme Co scam": [_reputation_result("Acme Co scam reports", "https://forum.example.com/1", "Multiple complaints...")],
        })
        service, _ = _make_service(repo=repo, google_scraper=scraper)

        service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1", search_reputation=True)

        supplier_id = next(iter(repo.suppliers.keys()))
        saved = repo.get_reputation_snippets(supplier_id)
        assert saved
        for s in saved:
            assert set(s.keys()) == {"query_type", "query_text", "title", "link", "snippet"}

    def test_query_uses_current_canonical_name_after_name_extraction(self):
        """Placeholder rows get a real name extracted earlier in the
        same row (see _attempt_name_extraction) -- reputation search
        must use THAT name, not the domain-derived placeholder."""
        repo = FakeRepo()

        class NamedPageCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                return {"status": "success", "pages_visited": 1, "error": None,
                        "pages": [FakePage(
                            "https://acmetrailer.com/",
                            "Welcome to Acme Trailer Manufacturing Inc. We build custom trailers "
                            "for the agriculture and construction industries nationwide.",
                        )]}

        llm = FakeLLMClient(response={"company_name": "Acme Trailer Manufacturing Inc."})
        scraper = FakeGoogleScraper(results_by_query={
            "Acme Trailer Manufacturing Inc. scam": [_reputation_result("t", "https://x.example.com/1", "s")],
        })
        service, repo = _make_service(repo=repo, collection_service=NamedPageCollection(), llm_client=llm, google_scraper=scraper)

        service.run_batch([_row(website="https://acmetrailer.com")], "job-1", search_reputation=True)

        queries = {c["query"] for c in scraper.calls}
        assert "Acme Trailer Manufacturing Inc. scam" in queries
        assert not any("Acmetrailer" in q for q in queries)

    def test_skipped_when_name_is_still_the_domain_derived_placeholder(self):
        """No real name found this row (e.g. LLM found nothing) -- a
        search for the raw placeholder ('Acmetrailer scam') would be
        useless, so it's skipped rather than wasting a paid search."""
        repo = FakeRepo()

        class NoNameCollection(FakeCollectionService):
            def collect(self, supplier_id, return_pages=False, source_url=None):
                return {"status": "success", "pages_visited": 1, "error": None,
                        "pages": [FakePage("https://acmetrailer.com/", "not much here")]}

        llm = FakeLLMClient(response={"company_name": None})
        scraper = FakeGoogleScraper()
        service, _ = _make_service(repo=repo, collection_service=NoNameCollection(), llm_client=llm, google_scraper=scraper)

        service.run_batch([_row(website="https://acmetrailer.com")], "job-1", search_reputation=True)

        assert scraper.calls == []

    def test_one_query_erroring_does_not_block_the_other_two(self):
        repo = FakeRepo()
        repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})

        class ErroringScraper(FakeGoogleScraper):
            def scrape(self, query, max_results=20, site_filter=None, **kwargs):
                self.calls.append({"query": query, "max_results": max_results})
                if "scam" in query:
                    raise RuntimeError("SerpAPI timeout")
                return self.results_by_query.get(query, [])

        scraper = ErroringScraper(results_by_query={
            "Acme Co review": [_reputation_result("t", "https://x.example.com/1", "s")],
        })
        service, _ = _make_service(repo=repo, google_scraper=scraper)

        outcome = service.run_batch(
            [_row(company_name="Acme Co", website="https://acme.com")], "job-1", search_reputation=True,
        )

        assert outcome.reputation_snippets_found == 1
        assert len(scraper.calls) == 3  # all three still attempted despite one raising

    def test_no_results_for_any_query_counts_as_not_found(self):
        repo = FakeRepo()
        repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})
        scraper = FakeGoogleScraper()  # no results_by_query entries -> every query returns []
        service, _ = _make_service(repo=repo, google_scraper=scraper)

        outcome = service.run_batch(
            [_row(company_name="Acme Co", website="https://acme.com")], "job-1", search_reputation=True,
        )

        assert outcome.reputation_snippets_found == 0
        assert len(scraper.calls) == 3

    def test_repeat_search_does_not_duplicate_snippets(self):
        """save_reputation_snippets is INSERT-OR-IGNORE keyed on
        (supplier_id, query_type, link) -- a second batch run over the
        same supplier must not double the stored evidence."""
        repo = FakeRepo()
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})
        scraper = FakeGoogleScraper(results_by_query={
            "Acme Co scam": [_reputation_result("t", "https://x.example.com/1", "s")],
        })
        service, _ = _make_service(repo=repo, google_scraper=scraper)

        service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-1", search_reputation=True)
        service.run_batch([_row(company_name="Acme Co", website="https://acme.com")], "job-2", search_reputation=True)

        saved = repo.get_reputation_snippets(supplier_id)
        assert len([s for s in saved if s["query_type"] == "scam"]) == 1


class ScriptedCollection(FakeCollectionService):
    """FakeCollectionService that fails collect() for any source_url in
    `dead_urls`, succeeds for everything else -- lets a recovery test
    script "the original domain is dead, the recovered one is not"
    without needing call-order tricks."""

    def __init__(self, dead_urls):
        super().__init__()
        self.dead_urls = set(dead_urls)

    def collect(self, supplier_id, return_pages=False, source_url=None):
        self.calls.append({"supplier_id": supplier_id, "return_pages": return_pages, "source_url": source_url})
        if source_url in self.dead_urls:
            return {"status": "failed", "pages_visited": 0, "error": "could not fetch candidate site", "pages": []}
        return {"status": "success", "pages_visited": 1, "error": None, "pages": []}


class TestDomainRecovery:
    """recover_dead_domains is opt-in and off by default -- these tests
    always pass it explicitly. A recovered candidate gets zero special
    trust: recover() is the real candidate_validator gate (faked here to
    a scripted outcome, exercised for real in
    test_discovery_candidate_validator.py) -- these tests only assert
    BatchService's own wiring around it."""

    def test_disabled_by_default_recovery_never_attempted(self):
        repo = FakeRepo()
        collection = ScriptedCollection(dead_urls={"https://acme-dead.com"})
        validator = FakeCandidateValidator(recovery_result=_recovered_result("acme-real.com"))
        service, repo = _make_service(repo=repo, collection_service=collection, candidate_validator=validator)

        outcome = service.run_batch(
            [_row(company_name="Acme Co", website="https://acme-dead.com")], "job-1",
        )

        assert outcome.domains_recovered == 0
        assert validator.recover_calls == []
        assert len(collection.calls) == 1
        assert outcome.failed == 1

    def test_raises_when_enabled_without_recovery_product_term(self):
        service, _ = _make_service()
        with pytest.raises(ValueError):
            service.run_batch(
                [_row(company_name="Acme Co", website="https://acme.com")], "job-1",
                recover_dead_domains=True,
            )

    def test_only_triggers_after_a_real_collect_failure(self):
        repo = FakeRepo()
        collection = ScriptedCollection(dead_urls=set())  # nothing dead
        validator = FakeCandidateValidator(recovery_result=_recovered_result("acme-real.com"))
        service, repo = _make_service(repo=repo, collection_service=collection, candidate_validator=validator)

        service.run_batch(
            [_row(company_name="Acme Co", website="https://acme.com")], "job-1",
            recover_dead_domains=True, recovery_product_term="trailers",
        )

        assert validator.recover_calls == []

    def test_successful_recovery_updates_domain_and_recollects(self):
        repo = FakeRepo()
        collection = ScriptedCollection(dead_urls={"https://acme-dead.com"})
        validator = FakeCandidateValidator(recovery_result=_recovered_result("acme-real.com"))
        service, repo = _make_service(repo=repo, collection_service=collection, candidate_validator=validator)

        outcome = service.run_batch(
            [_row(company_name="Acme Co", website="https://acme-dead.com")], "job-1",
            recover_dead_domains=True, recovery_product_term="trailers",
        )

        assert outcome.domains_recovered == 1
        assert outcome.succeeded == 1
        assert len(collection.calls) == 2
        assert collection.calls[0]["source_url"] == "https://acme-dead.com"
        assert collection.calls[1]["source_url"] == "acme-real.com"
        assert validator.recover_calls == [
            {"company_name": "Acme Co", "product_term": "trailers", "max_candidates": 2, "existing_country": None},
        ]
        supplier_id = next(iter(repo.suppliers.keys()))
        assert repo.suppliers[supplier_id]["domain"] == "acme-real.com"
        assert repo.history_calls[-1]["changed_by"] == "batch_service"
        row = next(iter(repo.rows.values()))
        assert row["status"] == "success"

    def test_placeholder_name_used_as_search_query_when_row_has_no_company_name(self):
        repo = FakeRepo()
        collection = ScriptedCollection(dead_urls={"https://acmetrailer-dead.com"})
        validator = FakeCandidateValidator(recovery_result=_recovered_result("acmetrailer.com"))
        service, repo = _make_service(repo=repo, collection_service=collection, candidate_validator=validator)

        service.run_batch(
            [_row(website="https://acmetrailer-dead.com")], "job-1",
            recover_dead_domains=True, recovery_product_term="trailers",
        )

        assert validator.recover_calls[0]["company_name"] == "Acmetrailer Dead"

    def test_existing_country_threaded_through_from_supplier_record(self):
        """recover()'s own extra corroboration check (see
        discovery.candidate_validator.CandidateValidator.recover's
        docstring -- added after a real pilot false-matched a dead
        domain onto an unrelated company) needs the supplier's on-file
        country as a hint; batch_service must actually read and pass
        it, not just default it away."""
        repo = FakeRepo()
        repo.create_golden_record({
            "canonical_name": "Acme Co", "domain": "acme-dead.com", "country": "United Kingdom",
        })
        collection = ScriptedCollection(dead_urls={"https://acme-dead.com"})
        validator = FakeCandidateValidator(recovery_result=_recovered_result("acme-real.com"))
        service, repo = _make_service(repo=repo, collection_service=collection, candidate_validator=validator)

        service.run_batch(
            [_row(company_name="Acme Co", website="https://acme-dead.com")], "job-1",
            recover_dead_domains=True, recovery_product_term="trailers",
        )

        assert validator.recover_calls[0]["existing_country"] == "United Kingdom"

    def test_recovery_finding_nothing_leaves_row_failed_and_domain_unchanged(self):
        repo = FakeRepo()
        collection = ScriptedCollection(dead_urls={"https://acme-dead.com"})
        validator = FakeCandidateValidator(recovery_result=None)
        service, repo = _make_service(repo=repo, collection_service=collection, candidate_validator=validator)

        outcome = service.run_batch(
            [_row(company_name="Acme Co", website="https://acme-dead.com")], "job-1",
            recover_dead_domains=True, recovery_product_term="trailers",
        )

        assert outcome.domains_recovered == 0
        assert outcome.failed == 1
        assert len(collection.calls) == 1  # no second collect attempted -- nothing recovered
        supplier_id = next(iter(repo.suppliers.keys()))
        assert repo.suppliers[supplier_id]["domain"] == "acme-dead.com"

    def test_recovery_exception_does_not_abort_the_batch(self):
        repo = FakeRepo()
        collection = ScriptedCollection(dead_urls={"https://acme-dead.com"})
        validator = FakeCandidateValidator(raises=True)
        service, repo = _make_service(repo=repo, collection_service=collection, candidate_validator=validator)

        outcome = service.run_batch(
            [
                _row(row_index=0, company_name="Acme Co", website="https://acme-dead.com"),
                _row(row_index=1, company_name="Beta Co", website="https://beta.com"),
            ],
            "job-1", recover_dead_domains=True, recovery_product_term="trailers",
        )

        assert outcome.total_rows == 2
        assert outcome.domains_recovered == 0
        assert outcome.failed == 1  # the acme-dead.com row
        assert outcome.succeeded == 1  # beta.com row unaffected by the other row's recovery blowing up


class TestMarketplaceRootRejection:
    """A marketplace ROOT (or any *.alibaba.com/made-in-china.com/
    tradeindia.com/etc. subdomain) has no independently-verifiable
    company identity -- found live: extract_domain() strips a URL's
    path/listing-ID entirely, so 11 distinct supplied company names all
    pointed at the same marketplace collapsed onto just 2 supplier
    records via the ordinary domain-exact-match dedup tier, each merge
    silently reporting "success" for the wrong company. These rows must
    hard-fail before any resolve/dedup/collect attempt is ever made."""

    @pytest.mark.parametrize("website", [
        "https://www.made-in-china.com",
        "https://www.alibaba.com/",
        "http://tradeindia.com",
        "https://ledmasters.en.alibaba.com/product/12345.html",  # a specific-looking
        # LISTING subdomain is still excluded, not just the bare root --
        # is_platform_subdomain() rejects any *.alibaba.com uniformly,
        # matching discovery.candidate_validator's own gate 2 exactly
        # (a marketplace storefront is a negative signal either way).
    ])
    def test_marketplace_url_hard_fails_before_any_resolve_attempt(self, website):
        repo = FakeRepo()
        matcher = FakeMatcher(repo)
        collection = FakeCollectionService()
        service, _ = _make_service(repo=repo, matcher=matcher, collection_service=collection)

        outcome = service.run_batch(
            [_row(company_name="Some Cable Co", website=website)], "job-1",
        )

        assert outcome.failed == 1
        assert outcome.succeeded == 0
        assert outcome.processed == outcome.succeeded + outcome.failed
        assert matcher.calls == []       # never even attempted dedup/resolve
        assert collection.calls == []    # never attempted a fetch
        row = next(iter(repo.rows.values()))
        assert row["status"] == "failed"
        assert "marketplace root URL" in row["error_message"]

    def test_distinct_companies_on_the_same_marketplace_do_not_collapse(self):
        """The exact live failure shape: multiple distinct supplied
        names pointed at the same marketplace root must NOT merge into
        one supplier record (or into each other at all) -- each is
        independently, correctly rejected."""
        repo = FakeRepo()
        matcher = FakeMatcher(repo)
        collection = FakeCollectionService()
        service, _ = _make_service(repo=repo, matcher=matcher, collection_service=collection)

        outcome = service.run_batch(
            [
                _row(row_index=0, company_name="Wuxi Speedy Cable Co", website="https://www.made-in-china.com"),
                _row(row_index=1, company_name="Shenzhen Prime Wire Ltd", website="https://www.made-in-china.com/"),
                _row(row_index=2, company_name="Global Brake Parts Trading", website="https://www.alibaba.com"),
            ],
            "job-1",
        )

        assert outcome.failed == 3
        assert len(repo.suppliers) == 0  # no supplier record created for any of them
        for row in repo.rows.values():
            assert row["supplier_id"] is None

    def test_placeholder_website_only_row_on_a_marketplace_root_also_rejected(self):
        """Same gate applies to website-only rows (no company_name) --
        a marketplace root has no derivable identity either way."""
        repo = FakeRepo()
        collection = FakeCollectionService()
        service, _ = _make_service(repo=repo, collection_service=collection)

        outcome = service.run_batch(
            [_row(website="https://www.tradeindia.com")], "job-1",
        )

        assert outcome.failed == 1
        assert outcome.placeholder_names_used == 0  # never even reached placeholder resolution
        assert collection.calls == []

    def test_ordinary_company_own_domain_is_unaffected(self):
        """Regression guard: a normal supplier website (not a known
        marketplace) must be completely unaffected by this gate."""
        repo = FakeRepo()
        collection = FakeCollectionService()
        service, _ = _make_service(repo=repo, collection_service=collection)

        outcome = service.run_batch(
            [_row(company_name="Acme Cable Co", website="https://acmecable.com")], "job-1",
        )

        assert outcome.succeeded == 1
        assert outcome.failed == 0


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


class TestDefaultRegionFallback:
    """main.py batch-upload had no equivalent to main.py collect's own
    --default-region at all until now -- found live: re-collecting a
    real 71-row batch with a GB fallback recovered 41 more phone
    numbers than without one, dwarfing every other phone-extraction
    fix combined (see collection_service.py's own docstring on this
    parameter for why: phonenumbers can't parse a national-format
    number without a region hint, and collect() always runs before
    this class's own address/country extraction step)."""

    def test_threaded_through_to_a_lazily_constructed_collection_service(self):
        service = BatchService(repo=FakeRepo(), default_region_fallback="GB")
        assert service.collection_service.default_region_fallback == "GB"

    def test_none_by_default(self):
        service = BatchService(repo=FakeRepo())
        assert service.collection_service.default_region_fallback is None

    def test_does_not_override_an_explicitly_injected_collection_service(self):
        """Same precedent as candidate_validator's own lazy-construction
        guard -- an explicitly-injected collaborator is used exactly as
        given, never silently reconfigured."""
        injected = FakeCollectionService()
        service = BatchService(repo=FakeRepo(), collection_service=injected, default_region_fallback="GB")
        assert service.collection_service is injected


class TestProductKeywords:
    """_attempt_set_product_keywords reuses batch/supplier_correction.py's
    SupplierCorrectionService.set_product_keywords (not duplicated here) --
    these tests exercise BatchService's own wiring around it: which term
    source wins, when it fires, and that the guard survives a within-
    batch repeat. See that method's own tests in
    tests/test_supplier_correction.py for the guard's own unit coverage."""

    def test_batch_level_term_applied_to_a_fresh_supplier(self):
        repo = FakeRepo()
        service, _ = _make_service(repo=repo)
        outcome = service.run_batch(
            [_row(company_name="Acme Co", website="https://acme.com")], "job-1",
            product_keywords=["gas cylinder manufacturer"],
        )
        assert outcome.product_keywords_set == 1
        assert repo.get_supplier(1)["product_keywords"] == ["gas cylinder manufacturer"]

    def test_per_row_csv_column_overrides_batch_level_term(self):
        repo = FakeRepo()
        service, _ = _make_service(repo=repo)
        outcome = service.run_batch(
            [_row(company_name="Acme Co", website="https://acme.com", product_keywords="metal pressing")],
            "job-1", product_keywords=["gas cylinder manufacturer"],
        )
        assert outcome.product_keywords_set == 1
        assert repo.get_supplier(1)["product_keywords"] == ["metal pressing"]

    def test_no_term_anywhere_is_a_no_op(self):
        repo = FakeRepo()
        service, _ = _make_service(repo=repo)
        outcome = service.run_batch(
            [_row(company_name="Acme Co", website="https://acme.com")], "job-1",
        )
        assert outcome.product_keywords_set == 0
        assert "product_keywords" not in repo.get_supplier(1)

    def test_guard_skips_a_supplier_that_already_has_product_keywords(self):
        repo = FakeRepo()
        matcher = FakeMatcher(repo)
        existing_id = repo.create_golden_record({
            "canonical_name": "Acme Co", "domain": "acme.com", "product_keywords": ["existing tag"],
        })
        service, _ = _make_service(repo=repo, matcher=matcher)
        outcome = service.run_batch(
            [_row(company_name="Acme Co", website="https://acme.com")], "job-1",
            product_keywords=["gas cylinder manufacturer"],
        )
        assert outcome.product_keywords_set == 0
        assert repo.get_supplier(existing_id)["product_keywords"] == ["existing tag"]

    def test_term_set_even_when_collection_fails(self):
        """product_keywords is a pure category tag, not something a fetch
        populates -- a supplier whose site is currently unreachable is
        exactly as entitled to a category tag as one that collects
        cleanly (the real Wessington Cryogenics case tonight)."""
        repo = FakeRepo()
        collection = FakeCollectionService(results={1: {"status": "failed", "error": "could not load homepage"}})
        service, _ = _make_service(repo=repo, collection_service=collection)
        outcome = service.run_batch(
            [_row(company_name="Acme Co", website="https://acme.com")], "job-1",
            product_keywords=["gas cylinder manufacturer"],
        )
        assert outcome.failed == 1
        assert outcome.product_keywords_set == 1
        assert repo.get_supplier(1)["product_keywords"] == ["gas cylinder manufacturer"]

    def test_repeat_domain_row_does_not_double_write(self):
        repo = FakeRepo()
        service, _ = _make_service(repo=repo)
        outcome = service.run_batch(
            [
                _row(row_index=0, company_name="Acme Co", website="https://acme.com"),
                _row(row_index=1, company_name="Acme Co", website="https://acme.com/"),
            ],
            "job-1", product_keywords=["gas cylinder manufacturer"],
        )
        assert outcome.product_keywords_set == 1
        assert repo.get_supplier(1)["product_keywords"] == ["gas cylinder manufacturer"]
