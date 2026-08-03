"""
tests/test_discovery_service.py

Tests for discovery/discovery_service.py -- the orchestrator wiring
query_builder -> GoogleSearchScraper -> candidate_extractor ->
CandidateValidator -> SupplierMatcher -> SupplierRepository. Fakes the
network/LLM-touching pieces (google_scraper, candidate_validator);
uses a REAL SupplierMatcher against a real temp-file repo, so dedup
behaviour (a rediscovered existing supplier merges rather than
duplicates) is genuinely exercised, not just asserted by inspection.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from deduplication.matcher import SupplierMatcher
from discovery.candidate_validator import ValidationResult
from discovery.discovery_service import DiscoveryService
from storage.database import initialise_schema
from storage.repository import SupplierRepository


def _search_result(link, title="", snippet=""):
    return SimpleNamespace(success=True, raw_data={"link": link, "title": title, "snippet": snippet})


class FakeGoogleScraper:
    """Returns the same fixed result set for every query by default, or
    a per-query mapping if given -- mirrors FakeScraper conventions
    used throughout this test suite."""

    def __init__(self, results=None, results_by_query=None, raise_error=None):
        self._results = results if results is not None else []
        self._results_by_query = results_by_query or {}
        self._raise_error = raise_error
        self.queries = []

    def scrape(self, query, max_results=20, **kwargs):
        self.queries.append(query)
        if self._raise_error:
            raise self._raise_error
        return self._results_by_query.get(query, self._results)


class FakeCandidateValidator:
    """`outcomes` maps domain -> ValidationResult; a domain with no
    entry defaults to "not validated" so a test only needs to specify
    the domains it cares about."""

    def __init__(self, outcomes=None, raise_for_domain=None):
        self._outcomes = outcomes or {}
        self._raise_for_domain = raise_for_domain
        self.calls = []

    def validate(self, candidate, product_term):
        self.calls.append((candidate.domain, product_term))
        if self._raise_for_domain and candidate.domain == self._raise_for_domain:
            raise RuntimeError("validator exploded")
        if candidate.domain in self._outcomes:
            return self._outcomes[candidate.domain]
        return ValidationResult(candidate, False, None, None, None, "not configured in test fake")


@pytest.fixture()
def repo(tmp_path):
    db_path = tmp_path / "test.db"
    initialise_schema(db_path)
    return SupplierRepository(db_path=db_path)


def _service(repo, google_results, validated_domain, extracted_name="Acme Trailer Co", extracted_country="China", **overrides):
    google_scraper = overrides.get("google_scraper") or FakeGoogleScraper(results=google_results)
    candidate = None
    validator = overrides.get("candidate_validator")
    if validator is None:
        from discovery.candidate_extractor import Candidate
        candidate = Candidate(title="Acme Trailer Co", link=f"https://{validated_domain}/", snippet="trailer axle manufacturer", domain=validated_domain)
        validator = FakeCandidateValidator(outcomes={
            validated_domain: ValidationResult(
                candidate, True, extracted_name, extracted_country, 95.0, "validated",
            ),
        })
    return DiscoveryService(
        repo=repo, google_scraper=google_scraper, website_fetcher=SimpleNamespace(),
        candidate_validator=validator, matcher=overrides.get("matcher") or SupplierMatcher(repo),
    )


class TestDiscoverCreatesNewSuppliers:

    def test_validated_candidate_creates_a_new_supplier(self, repo):
        results = [_search_result("https://acmetrailer.com/", title="Acme Trailer Co", snippet="trailer axle manufacturer")]
        service = _service(repo, results, "acmetrailer.com")

        outcome = service.discover("trailer axle", country="China")

        assert outcome.candidates_found == 1
        assert outcome.candidates_validated == 1
        assert outcome.candidates_rejected == 0
        assert len(outcome.new_supplier_ids) == 1
        assert outcome.review_queued_supplier_ids == []
        supplier = repo.get_supplier(outcome.new_supplier_ids[0])
        assert supplier["canonical_name"] == "Acme Trailer Co"
        assert supplier["domain"] == "acmetrailer.com"
        assert supplier["discovery_source"] == "discovery_service"

    def test_writes_a_discovery_runs_summary_row(self, repo):
        results = [_search_result("https://acmetrailer.com/", title="Acme Trailer Co", snippet="trailer axle manufacturer")]
        service = _service(repo, results, "acmetrailer.com")

        service.discover("trailer axle", category="Axles", country="China")

        # discovery_runs has no dedicated getter yet -- check via raw SQL through repo's own connection helper
        from storage.database import connection_scope
        with connection_scope(repo.db_path) as conn:
            rows = conn.execute("SELECT * FROM discovery_runs").fetchall()
        assert len(rows) == 1
        assert rows[0]["product_query"] == "trailer axle"
        assert rows[0]["category"] == "Axles"
        assert rows[0]["candidates_validated"] == 1

    def test_accepted_candidate_evidence_is_written_to_raw_source_data(self, repo):
        results = [_search_result("https://acmetrailer.com/", title="Acme Trailer Co", snippet="trailer axle manufacturer")]
        service = _service(repo, results, "acmetrailer.com")

        service.discover("trailer axle")

        from storage.database import connection_scope
        with connection_scope(repo.db_path) as conn:
            rows = conn.execute("SELECT * FROM raw_source_data WHERE source = 'discovery'").fetchall()
        assert len(rows) == 1
        assert rows[0]["processing_status"] == "processed"
        assert rows[0]["golden_record_id"] is not None


class TestDiscoverRejectsAndRecordsEvidence:

    def test_rejected_candidate_is_not_created_but_is_recorded(self, repo):
        results = [_search_result("https://acmetrailer.com/", title="Acme Trailer Co", snippet="trailer axle manufacturer")]
        from discovery.candidate_extractor import Candidate
        candidate = Candidate(title="Acme Trailer Co", link="https://acmetrailer.com/", snippet="trailer axle manufacturer", domain="acmetrailer.com")
        validator = FakeCandidateValidator(outcomes={
            "acmetrailer.com": ValidationResult(candidate, False, None, None, None, "no company name found in page text"),
        })
        service = _service(repo, results, "acmetrailer.com", candidate_validator=validator)

        outcome = service.discover("trailer axle")

        assert outcome.candidates_validated == 0
        assert outcome.candidates_rejected == 1
        assert outcome.new_supplier_ids == []
        from storage.database import connection_scope
        with connection_scope(repo.db_path) as conn:
            rows = conn.execute("SELECT * FROM raw_source_data WHERE source = 'discovery'").fetchall()
        assert len(rows) == 1
        assert rows[0]["processing_status"] == "failed"
        assert "no company name found" in rows[0]["error_message"]

    def test_platform_domain_results_never_reach_the_validator(self, repo):
        """candidate_extractor already filters these out -- the
        validator (and its LLM call) should never even be invoked."""
        results = [_search_result("https://acme.en.alibaba.com/", title="Acme on Alibaba")]
        validator = FakeCandidateValidator()
        service = _service(repo, results, "acmetrailer.com", candidate_validator=validator)

        outcome = service.discover("trailer axle")

        assert outcome.candidates_found == 0
        assert validator.calls == []


class FakeMatcher:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def resolve_and_store(self, candidate):
        self.calls.append(candidate)
        return self._result


class TestDiscoverReviewQueued:

    def test_review_queued_action_counts_as_a_new_supplier_not_a_merge(self, repo):
        """review_queued creates a genuinely new row (pending human
        dedup review against a close-but-not-auto-merge match) -- it
        must be counted alongside 'created' in new_supplier_ids, not
        conflated with 'merged' (which creates no new row at all)."""
        existing_id = repo.create_golden_record({"canonical_name": "Acme Trailer Manufacturing", "domain": "other-domain.example.com"})
        new_id = repo.create_golden_record({"canonical_name": "Acme Trailer Co", "domain": "acmetrailer.com"})
        results = [_search_result("https://acmetrailer.com/", title="Acme Trailer Co", snippet="trailer axle manufacturer")]
        fake_matcher = FakeMatcher({
            "action": "review_queued", "new_supplier_id": new_id, "matched_supplier_id": existing_id,
            "confidence": 0.8, "signals": {}, "dedup_candidate_id": 1,
        })
        service = _service(repo, results, "acmetrailer.com", matcher=fake_matcher)

        outcome = service.discover("trailer axle")

        assert outcome.candidates_duplicate == 0
        assert outcome.new_supplier_ids == [new_id]
        assert outcome.review_queued_supplier_ids == [new_id]


class TestDiscoverDeduplication:

    def test_rediscovering_an_existing_supplier_by_exact_domain_merges_not_duplicates(self, repo):
        """The core "never invent companies / eliminate duplicates"
        requirement -- proven against the REAL SupplierMatcher, not a
        fake, since this is exactly the dedup engine already in
        production."""
        repo.create_golden_record({"canonical_name": "Acme Trailer Co", "domain": "acmetrailer.com", "country": "China"})

        results = [_search_result("https://acmetrailer.com/", title="Acme Trailer Co", snippet="trailer axle manufacturer")]
        service = _service(repo, results, "acmetrailer.com")

        outcome = service.discover("trailer axle", country="China")

        assert outcome.candidates_validated == 1
        assert outcome.candidates_duplicate == 1
        from storage.database import connection_scope
        with connection_scope(repo.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) AS n FROM suppliers").fetchone()["n"]
        assert count == 1  # merged, not duplicated


class TestDiscoverFaultIsolation:

    def test_one_query_failing_does_not_abort_other_queries(self, repo):
        google_scraper = FakeGoogleScraper(
            results_by_query={
                '"trailer axle" manufacturer': [_search_result("https://acmetrailer.com/", title="Acme Trailer Co", snippet="trailer axle manufacturer")],
            },
        )

        def flaky_scrape(query, max_results=20, **kwargs):
            google_scraper.queries.append(query)
            if "supplier" in query:
                raise RuntimeError("search API down")
            return google_scraper._results_by_query.get(query, [])

        google_scraper.scrape = flaky_scrape
        service = _service(repo, [], "acmetrailer.com", google_scraper=google_scraper)

        outcome = service.discover("trailer axle")  # must not raise
        assert outcome.candidates_found == 1

    def test_one_candidate_validator_exception_does_not_abort_others(self, repo):
        results = [
            _search_result("https://acmetrailer.com/", title="Acme Trailer Co", snippet="trailer axle manufacturer"),
            _search_result("https://bestaxles.com/", title="Best Axles Ltd", snippet="trailer axle manufacturer"),
        ]
        from discovery.candidate_extractor import Candidate
        good_candidate = Candidate(title="Best Axles Ltd", link="https://bestaxles.com/", snippet="trailer axle manufacturer", domain="bestaxles.com")
        validator = FakeCandidateValidator(
            outcomes={"bestaxles.com": ValidationResult(good_candidate, True, "Best Axles Ltd", "China", 90.0, "validated")},
            raise_for_domain="acmetrailer.com",
        )
        service = _service(repo, results, "acmetrailer.com", candidate_validator=validator)

        outcome = service.discover("trailer axle")  # must not raise

        assert outcome.candidates_found == 2
        assert outcome.candidates_rejected == 1  # the exploding one
        assert outcome.candidates_validated == 1  # the good one still processed


class TestDiscoverMaxCandidates:

    def test_stops_once_max_candidates_reached_across_queries(self, repo):
        results = [
            _search_result(f"https://company{i}.example.com/", title=f"Company {i}", snippet="trailer axle manufacturer")
            for i in range(10)
        ]
        service = _service(repo, results, "company0.example.com")

        outcome = service.discover("trailer axle", max_candidates=3)

        assert outcome.candidates_found == 3
