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

    def test_discovered_supplier_is_findable_by_the_product_it_was_discovered_for(self, repo):
        """Real bug: without product_keywords set, a supplier discovered
        for "trailer axle" was invisible to search_suppliers_full's own
        product-term search unless the company's name happened to
        contain "trailer axle" -- searching for the exact term that
        found them returned nothing."""
        results = [_search_result("https://acmetrailer.com/", title="Acme Trailer Co", snippet="trailer axle manufacturer")]
        service = _service(repo, results, "acmetrailer.com")

        outcome = service.discover("trailer axle", country="China")

        supplier = repo.get_supplier(outcome.new_supplier_ids[0])
        assert supplier["product_keywords"] == ["trailer axle"]
        found = repo.search_suppliers_full(product_query="trailer axle")
        assert [s["id"] for s in found] == [outcome.new_supplier_ids[0]]

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


class FakeLLMCandidateSource:
    """Mirrors FakeGoogleScraper's convention: a fixed return value,
    recording every call for assertion."""

    def __init__(self, candidates=None, stats=None):
        from discovery.llm_candidate_source import GenerationStats
        self._candidates = candidates or []
        self._stats = stats or GenerationStats(
            raw_generated=len(self._candidates), deduplicated=len(self._candidates),
        )
        self.calls = []

    def find_candidates(self, product, country=None, max_candidates=20):
        self.calls.append((product, country, max_candidates))
        return self._candidates, self._stats


class TestDiscoverLLMSource:
    """source='llm' must flow through the exact same
    CandidateValidator/SupplierMatcher pipeline as source='serpapi'
    (the default) -- only candidate generation and raw_source_data
    provenance differ. See discovery/llm_candidate_source.py."""

    def _llm_service(self, repo, candidates, validated_domain, extracted_name="Acme Trailer Co", extracted_country="China", **overrides):
        llm_source = overrides.get("llm_candidate_source") or FakeLLMCandidateSource(candidates=candidates)
        validator = overrides.get("candidate_validator")
        if validator is None:
            validator = FakeCandidateValidator(outcomes={
                validated_domain: ValidationResult(
                    candidates[0], True, extracted_name, extracted_country, 95.0, "validated: name corroborated",
                ),
            })
        return DiscoveryService(
            repo=repo, google_scraper=FakeGoogleScraper(results=[]), website_fetcher=SimpleNamespace(),
            candidate_validator=validator, matcher=overrides.get("matcher") or SupplierMatcher(repo),
            llm_candidate_source=llm_source,
        )

    def test_source_llm_uses_llm_candidate_source_not_google_scraper(self, repo):
        from discovery.candidate_extractor import Candidate
        candidate = Candidate(title="Acme Trailer Co", link="https://acmetrailer.com", snippet="makes trailer axles", domain="acmetrailer.com")
        google_scraper = FakeGoogleScraper(results=[])
        llm_source = FakeLLMCandidateSource(candidates=[candidate])
        service = self._llm_service(repo, [candidate], "acmetrailer.com", llm_candidate_source=llm_source)
        service.google_scraper = google_scraper

        service.discover("trailer axle", source="llm")

        assert llm_source.calls == [("trailer axle", None, 20)]
        assert google_scraper.queries == []

    def test_source_llm_writes_raw_source_data_with_llm_discovery_provenance(self, repo):
        from discovery.candidate_extractor import Candidate
        candidate = Candidate(title="Acme Trailer Co", link="https://acmetrailer.com", snippet="makes trailer axles", domain="acmetrailer.com")
        service = self._llm_service(repo, [candidate], "acmetrailer.com")

        service.discover("trailer axle", source="llm")

        from storage.database import connection_scope
        with connection_scope(repo.db_path) as conn:
            discovery_rows = conn.execute("SELECT * FROM raw_source_data WHERE source = 'discovery'").fetchall()
            llm_rows = conn.execute("SELECT * FROM raw_source_data WHERE source = 'llm-discovery'").fetchall()
        assert discovery_rows == []
        assert len(llm_rows) == 1
        assert llm_rows[0]["golden_record_id"] is not None

    def test_source_llm_still_creates_a_supplier_via_the_same_validator_and_matcher(self, repo):
        from discovery.candidate_extractor import Candidate
        candidate = Candidate(title="Acme Trailer Co", link="https://acmetrailer.com", snippet="makes trailer axles", domain="acmetrailer.com")
        service = self._llm_service(repo, [candidate], "acmetrailer.com")

        outcome = service.discover("trailer axle", source="llm")

        assert len(outcome.new_supplier_ids) == 1
        supplier = repo.get_supplier(outcome.new_supplier_ids[0])
        assert supplier["canonical_name"] == "Acme Trailer Co"
        assert supplier["domain"] == "acmetrailer.com"

    def test_source_llm_rejected_candidate_is_not_stored_but_is_recorded(self, repo):
        """The critical requirement: an LLM-proposed candidate whose
        site doesn't resolve or doesn't corroborate must be dropped,
        not written to suppliers -- exactly like a bad serpapi hit."""
        from discovery.candidate_extractor import Candidate
        candidate = Candidate(title="Ghost Co", link="https://ghostco-imaginary.example", snippet="", domain="ghostco-imaginary.example")
        validator = FakeCandidateValidator(outcomes={
            "ghostco-imaginary.example": ValidationResult(
                candidate, False, None, None, None, "could not fetch candidate site: connection refused",
            ),
        })
        service = self._llm_service(repo, [candidate], "ghostco-imaginary.example", candidate_validator=validator)

        outcome = service.discover("trailer axle", source="llm")

        assert outcome.new_supplier_ids == []
        assert outcome.candidates_rejected == 1
        from storage.database import connection_scope
        with connection_scope(repo.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) AS n FROM suppliers").fetchone()["n"]
        assert count == 0

    def test_candidates_generated_reflects_llm_source_stats(self, repo):
        from discovery.candidate_extractor import Candidate
        from discovery.llm_candidate_source import GenerationStats
        candidate = Candidate(title="Acme Trailer Co", link="https://acmetrailer.com", snippet="", domain="acmetrailer.com")
        llm_source = FakeLLMCandidateSource(
            candidates=[candidate], stats=GenerationStats(raw_generated=7, deduplicated=1),
        )
        service = self._llm_service(repo, [candidate], "acmetrailer.com", llm_candidate_source=llm_source)

        outcome = service.discover("trailer axle", source="llm")

        assert outcome.candidates_generated == 7
        assert outcome.candidates_found == 1

    def test_unknown_source_raises(self, repo):
        service = DiscoveryService(
            repo=repo, google_scraper=FakeGoogleScraper(results=[]), website_fetcher=SimpleNamespace(),
            candidate_validator=FakeCandidateValidator(), matcher=SupplierMatcher(repo),
        )
        with pytest.raises(ValueError):
            service.discover("trailer axle", source="bogus")


class TestDiscoverResolutionCounters:
    """website_resolved/content_matched -- the funnel breakdown behind
    `main.py discover --source llm`'s report. Exercised via the
    serpapi path (the shared _process_candidate code, not source-specific)
    since these counters aren't specific to either source."""

    def test_fetch_failure_counts_as_not_resolved(self, repo):
        results = [_search_result("https://acmetrailer.com/", title="Acme Trailer Co", snippet="trailer axle manufacturer")]
        from discovery.candidate_extractor import Candidate
        candidate = Candidate(title="Acme Trailer Co", link="https://acmetrailer.com/", snippet="trailer axle manufacturer", domain="acmetrailer.com")
        validator = FakeCandidateValidator(outcomes={
            "acmetrailer.com": ValidationResult(candidate, False, None, None, None, "could not fetch candidate site: 404"),
        })
        service = _service(repo, results, "acmetrailer.com", candidate_validator=validator)

        outcome = service.discover("trailer axle")

        assert outcome.website_resolved == 0
        assert outcome.content_matched == 0

    def test_resolved_but_term_missing_counts_as_resolved_not_matched(self, repo):
        results = [_search_result("https://acmetrailer.com/", title="Acme Trailer Co", snippet="trailer axle manufacturer")]
        from discovery.candidate_extractor import Candidate
        candidate = Candidate(title="Acme Trailer Co", link="https://acmetrailer.com/", snippet="trailer axle manufacturer", domain="acmetrailer.com")
        validator = FakeCandidateValidator(outcomes={
            "acmetrailer.com": ValidationResult(
                candidate, False, "Acme Trailer Co", "China", 90.0,
                "fetched page text does not mention the searched term 'trailer axle'",
            ),
        })
        service = _service(repo, results, "acmetrailer.com", candidate_validator=validator)

        outcome = service.discover("trailer axle")

        assert outcome.website_resolved == 1
        assert outcome.content_matched == 0

    def test_fully_validated_counts_as_both(self, repo):
        results = [_search_result("https://acmetrailer.com/", title="Acme Trailer Co", snippet="trailer axle manufacturer")]
        service = _service(repo, results, "acmetrailer.com")

        outcome = service.discover("trailer axle")

        assert outcome.website_resolved == 1
        assert outcome.content_matched == 1

    def test_trader_exclusion_still_counts_as_content_matched(self, repo):
        """The product term WAS found (gate 5 passed) -- only gate 6
        (trader self-declaration) failed. content_matched must reflect
        gate 5's own outcome, not the final validated verdict."""
        results = [_search_result("https://acmetrailer.com/", title="Acme Trailer Co", snippet="trailer axle manufacturer")]
        from discovery.candidate_extractor import Candidate
        candidate = Candidate(title="Acme Trailer Co", link="https://acmetrailer.com/", snippet="trailer axle manufacturer", domain="acmetrailer.com")
        validator = FakeCandidateValidator(outcomes={
            "acmetrailer.com": ValidationResult(
                candidate, False, "Acme Trailer Co", "China", 90.0,
                "page self-identifies as a trading company/distributor (matched phrase: 'we are a distributor') -- excluded, not a manufacturer",
            ),
        })
        service = _service(repo, results, "acmetrailer.com", candidate_validator=validator)

        outcome = service.discover("trailer axle")

        assert outcome.website_resolved == 1
        assert outcome.content_matched == 1
        assert outcome.candidates_rejected == 1  # still rejected overall


def _china_1688_result(offer_id, company_name, **overrides):
    from scrapers.base_scraper import ScraperResult
    raw = {
        "offerId": offer_id, "companyName": company_name, "title": "拖车支撑轮",
        "titleEn": "Trailer jockey wheel", "province": "浙江省", "city": "温州市",
        "supplierUrl": f"https://winport.m.1688.com/page/index.html?memberId={offer_id}",
        "merchantSigns": {"powerfulMerchant": False, "trustPass": True, "factory": True, "industrySeller": False},
        "yearsActive": None,
    }
    raw.update(overrides)
    return ScraperResult(source="china_1688", source_id=str(offer_id), raw_data=raw, success=True)


class FakeChina1688Scraper:
    """Mirrors FakeGoogleScraper's convention: a fixed return value,
    recording every call for assertion. `raise_error` simulates the
    real scraper's own never-raises contract (it catches Apify failures
    internally and returns [error_result(...)]), so tests exercise
    DiscoveryService's handling of that error-result shape, not an
    actual exception path."""

    def __init__(self, results=None, error_result=None):
        from scrapers.base_scraper import ScraperResult
        self._results = results if results is not None else []
        self._error_result = error_result or ScraperResult(
            source="china_1688", source_id="", raw_data={}, success=False, error="actor run failed",
        )
        self.calls = []

    def scrape(self, query, max_results=20, require_super_factory=True, **kwargs):
        self.calls.append((query, max_results, require_super_factory))
        return self._results


class TestDiscover1688Source:
    """source='1688' is deliberately diagnostic-only right now -- see
    DiscoveryService._discover_1688's own docstring for why it stops
    short of CandidateValidator/SupplierMatcher (China1688Scraper's real
    output has no independent company-website field to validate
    against, only marketplace-hosted URLs)."""

    def _service_1688(self, repo, scraper):
        return DiscoveryService(
            repo=repo, google_scraper=FakeGoogleScraper(results=[]), website_fetcher=SimpleNamespace(),
            candidate_validator=FakeCandidateValidator(), matcher=SupplierMatcher(repo),
            china_1688_scraper=scraper,
        )

    def test_calls_china_1688_scraper_not_google_scraper(self, repo):
        scraper = FakeChina1688Scraper(results=[_china_1688_result("1001", "瑞安市嘉业汽摩附件有限公司")])
        google_scraper = FakeGoogleScraper(results=[])
        service = self._service_1688(repo, scraper)
        service.google_scraper = google_scraper

        service.discover("拖车支撑轮", source="1688", max_candidates=20)

        assert scraper.calls == [("拖车支撑轮", 20, False)]  # require_super_factory=False -- see module docstring
        assert google_scraper.queries == []

    def test_creates_no_supplier_rows(self, repo):
        """The core requirement: diagnostic-only, nothing inserted."""
        scraper = FakeChina1688Scraper(results=[
            _china_1688_result("1001", "瑞安市嘉业汽摩附件有限公司"),
            _china_1688_result("1002", "温州市龙湾永中南牧五金加工厂"),
        ])
        service = self._service_1688(repo, scraper)

        outcome = service.discover("拖车支撑轮", source="1688")

        assert outcome.candidates_found == 2
        assert outcome.new_supplier_ids == []
        from storage.database import connection_scope
        with connection_scope(repo.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) AS n FROM suppliers").fetchone()["n"]
        assert count == 0

    def test_raw_listings_are_returned_verbatim_for_inspection(self, repo):
        result = _china_1688_result("1001", "瑞安市嘉业汽摩附件有限公司")
        scraper = FakeChina1688Scraper(results=[result])
        service = self._service_1688(repo, scraper)

        outcome = service.discover("拖车支撑轮", source="1688")

        assert outcome.raw_1688_listings == [result.raw_data]
        assert outcome.raw_1688_listings[0]["companyName"] == "瑞安市嘉业汽摩附件有限公司"

    def test_writes_raw_source_data_evidence_with_china_1688_provenance(self, repo):
        result = _china_1688_result("1001", "瑞安市嘉业汽摩附件有限公司")
        scraper = FakeChina1688Scraper(results=[result])
        service = self._service_1688(repo, scraper)

        service.discover("拖车支撑轮", source="1688")

        from storage.database import connection_scope
        with connection_scope(repo.db_path) as conn:
            rows = conn.execute("SELECT * FROM raw_source_data WHERE source = 'china_1688'").fetchall()
        assert len(rows) == 1
        assert rows[0]["source_id"] == "1001"
        assert rows[0]["golden_record_id"] is None  # never linked to a supplier -- nothing was created

    def test_scraper_error_result_is_skipped_not_stored(self, repo):
        """China1688Scraper never raises -- an Apify failure comes back
        as a [error_result(...)] list (success=False), not an exception.
        Real case this covers: the Apify account's own monthly usage
        cap being hit."""
        from scrapers.base_scraper import ScraperResult
        error_result = ScraperResult(
            source="china_1688", source_id="", raw_data={}, success=False,
            error="Monthly usage hard limit exceeded",
        )
        scraper = FakeChina1688Scraper(results=[error_result])
        service = self._service_1688(repo, scraper)

        outcome = service.discover("拖车支撑轮", source="1688")  # must not raise

        assert outcome.candidates_found == 0
        assert outcome.raw_1688_listings == []
        from storage.database import connection_scope
        with connection_scope(repo.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) AS n FROM raw_source_data WHERE source='china_1688'").fetchone()["n"]
        assert count == 0

    def test_scraper_raising_does_not_propagate(self, repo):
        class ExplodingScraper:
            def scrape(self, query, max_results=20, require_super_factory=True, **kwargs):
                raise RuntimeError("Apify client exploded")

        service = self._service_1688(repo, ExplodingScraper())

        outcome = service.discover("拖车支撑轮", source="1688")  # must not raise

        assert outcome.candidates_found == 0

    def test_writes_a_discovery_runs_summary_row(self, repo):
        scraper = FakeChina1688Scraper(results=[_china_1688_result("1001", "瑞安市嘉业汽摩附件有限公司")])
        service = self._service_1688(repo, scraper)

        service.discover("拖车支撑轮", source="1688", category="Running gear")

        from storage.database import connection_scope
        with connection_scope(repo.db_path) as conn:
            rows = conn.execute("SELECT * FROM discovery_runs").fetchall()
        assert len(rows) == 1
        assert rows[0]["product_query"] == "拖车支撑轮"
        assert rows[0]["category"] == "Running gear"
        assert rows[0]["country"] == "China"
        assert rows[0]["candidates_found"] == 1


class TestBackfillProductKeywords:
    """discover() itself already writes product_keywords on create (see
    TestDiscoverCreatesNewSuppliers) -- these tests cover the separate
    repair path for suppliers created before that fix existed, using
    only pipeline_jobs history plus a bare repo (no google_scraper/
    validator/matcher needed, since backfill never re-runs discovery)."""

    def _service(self, repo):
        return DiscoveryService(repo=repo, google_scraper=FakeGoogleScraper(results=[]))

    def test_backfills_from_a_completed_discovery_job(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Winch Co"})
        repo.create_pipeline_job(job_id="job-1", query="[discovery] winch", options={})
        repo.mark_pipeline_job_completed("job-1", stats={"new_supplier_ids": [supplier_id]})

        result = self._service(repo).backfill_product_keywords()

        assert result["updated_supplier_ids"] == [supplier_id]
        assert repo.get_supplier(supplier_id)["product_keywords"] == ["winch"]

    def test_supplier_that_already_has_product_keywords_is_left_untouched(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme Winch Co", "product_keywords": ["already set"],
        })
        repo.create_pipeline_job(job_id="job-1", query="[discovery] winch", options={})
        repo.mark_pipeline_job_completed("job-1", stats={"new_supplier_ids": [supplier_id]})

        result = self._service(repo).backfill_product_keywords()

        assert result["updated_supplier_ids"] == []
        assert result["already_had_keywords_supplier_ids"] == [supplier_id]
        assert repo.get_supplier(supplier_id)["product_keywords"] == ["already set"]

    def test_non_discovery_jobs_are_ignored(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co"})
        repo.create_pipeline_job(job_id="job-1", query="wheel bearings", options={})
        repo.mark_pipeline_job_completed("job-1", stats={"created": 1})
        repo.create_pipeline_job(job_id="job-2", query="[collection] supplier #1", options={})
        repo.mark_pipeline_job_completed("job-2", stats={"status": "success"})

        result = self._service(repo).backfill_product_keywords()

        assert result["updated_supplier_ids"] == []
        assert repo.get_supplier(supplier_id)["product_keywords"] is None

    def test_uncompleted_discovery_job_is_ignored(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Winch Co"})
        repo.create_pipeline_job(job_id="job-1", query="[discovery] winch", options={})
        # left running, never marked completed

        result = self._service(repo).backfill_product_keywords()

        assert result["updated_supplier_ids"] == []
        assert repo.get_supplier(supplier_id)["product_keywords"] is None

    def test_supplier_id_no_longer_present_is_reported_not_raised(self, repo):
        repo.create_pipeline_job(job_id="job-1", query="[discovery] winch", options={})
        repo.mark_pipeline_job_completed("job-1", stats={"new_supplier_ids": [999999]})

        result = self._service(repo).backfill_product_keywords()

        assert result["missing_supplier_ids"] == [999999]
        assert result["updated_supplier_ids"] == []

    def test_is_safe_to_run_twice(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Winch Co"})
        repo.create_pipeline_job(job_id="job-1", query="[discovery] winch", options={})
        repo.mark_pipeline_job_completed("job-1", stats={"new_supplier_ids": [supplier_id]})
        service = self._service(repo)

        first = service.backfill_product_keywords()
        second = service.backfill_product_keywords()

        assert first["updated_supplier_ids"] == [supplier_id]
        assert second["updated_supplier_ids"] == []
        assert second["already_had_keywords_supplier_ids"] == [supplier_id]

    def test_writes_a_supplier_change_log_entry(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Winch Co"})
        repo.create_pipeline_job(job_id="job-1", query="[discovery] winch", options={})
        repo.mark_pipeline_job_completed("job-1", stats={"new_supplier_ids": [supplier_id]})

        self._service(repo).backfill_product_keywords()

        log = repo.get_supplier_change_log(supplier_id)
        assert any(entry["field_name"] == "product_keywords" for entry in log)
