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


class FakeCollectionService:
    """Stand-in for collection.collection_service.CollectionService --
    only .collect(supplier_id) is used by discovery's deep_collect path
    (no return_pages/source_url, unlike batch_service.py's own usage)."""

    def __init__(self, raise_for_supplier_id=None):
        self.calls: list = []
        self._raise_for_supplier_id = raise_for_supplier_id

    def collect(self, supplier_id):
        self.calls.append(supplier_id)
        if self._raise_for_supplier_id == supplier_id:
            raise RuntimeError("collection blew up")
        return {"supplier_id": supplier_id, "status": "success", "pages_visited": 1, "error": None}


class FakeCandidateValidator:
    """`outcomes` maps domain -> ValidationResult; a domain with no
    entry defaults to "not validated" so a test only needs to specify
    the domains it cares about. `recovery_result` (a ValidationResult
    or None) is what .recover() returns -- recovery tests don't need a
    real search+extract_candidates round trip, just a fixed outcome to
    assert discover()'s own wiring around it (recover_calls records
    every invocation for assertion)."""

    def __init__(self, outcomes=None, raise_for_domain=None, recovery_result=None):
        self._outcomes = outcomes or {}
        self._raise_for_domain = raise_for_domain
        self._recovery_result = recovery_result
        self.calls = []
        self.recover_calls = []

    def validate(self, candidate, product_term, skip_soft_trader_signals=False):
        self.calls.append((candidate.domain, product_term))
        if self._raise_for_domain and candidate.domain == self._raise_for_domain:
            raise RuntimeError("validator exploded")
        if candidate.domain in self._outcomes:
            return self._outcomes[candidate.domain]
        return ValidationResult(candidate, False, None, None, None, "not configured in test fake")

    def recover(self, company_name, product_term, google_scraper, country=None, max_candidates=2):
        self.recover_calls.append((company_name, product_term, country, max_candidates))
        return self._recovery_result


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
        collection_service=overrides.get("collection_service"),
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

    def test_a_validated_redirect_candidate_stores_the_resolved_domain(self, repo):
        """Real bug found live: a candidate validated via gate 3.6's
        same-company-redirect corroboration (dexteraxle.com ->
        dextergroup.com) must store the RESOLVED domain on its golden
        record, not the stale pre-redirect one -- otherwise the exact-
        domain-match dedup tier can never recognise an already-existing
        supplier under the real, current domain, creating a genuine
        duplicate."""
        from discovery.candidate_extractor import Candidate

        candidate = Candidate(
            title="Dexter Axle", link="https://dexteraxle.com/",
            snippet="trailer axle manufacturer", domain="dexteraxle.com",
        )
        validator = FakeCandidateValidator(outcomes={
            "dexteraxle.com": ValidationResult(
                candidate, True, "Dexter Group", "US", 95.0, "validated",
                resolved_domain="dextergroup.com",
            ),
        })
        results = [_search_result("https://dexteraxle.com/", title="Dexter Axle", snippet="trailer axle manufacturer")]
        service = _service(repo, results, "dexteraxle.com", candidate_validator=validator)

        outcome = service.discover("trailer axle")

        supplier = repo.get_supplier(outcome.new_supplier_ids[0])
        assert supplier["domain"] == "dextergroup.com"

    def test_a_non_redirect_validated_candidate_uses_its_own_domain_unchanged(self, repo):
        """resolved_domain=None (the default, every existing test's
        shape) must not change behaviour -- candidate.domain is used
        exactly as before."""
        results = [_search_result("https://acmetrailer.com/", title="Acme Trailer Co", snippet="trailer axle manufacturer")]
        service = _service(repo, results, "acmetrailer.com")

        outcome = service.discover("trailer axle")

        supplier = repo.get_supplier(outcome.new_supplier_ids[0])
        assert supplier["domain"] == "acmetrailer.com"

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

        # Merged into an ALREADY-existing supplier -- zero new distinct
        # companies found this run, so candidates_validated must stay 0,
        # not 1 (see that field's own comment on DiscoveryOutcome).
        assert outcome.candidates_validated == 0
        assert outcome.candidates_duplicate == 1
        from storage.database import connection_scope
        with connection_scope(repo.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) AS n FROM suppliers").fetchone()["n"]
        assert count == 1  # merged, not duplicated

    def test_duplicate_supplier_ids_records_which_existing_supplier_was_matched(self, repo):
        """A "duplicate" is a real, successful validation against an
        already-existing supplier, not a failure -- the Find Suppliers
        results screen needs to know WHICH supplier it was to show it
        as a real match, not just a count."""
        existing_id = repo.create_golden_record({
            "canonical_name": "Acme Trailer Co", "domain": "acmetrailer.com", "country": "China",
        })
        results = [_search_result("https://acmetrailer.com/", title="Acme Trailer Co", snippet="trailer axle manufacturer")]
        service = _service(repo, results, "acmetrailer.com")

        outcome = service.discover("trailer axle", country="China")

        assert outcome.duplicate_supplier_ids == [existing_id]


class TestDeepCollect:
    """Opt-in (deep_collect=True): a real CollectionService.collect()
    call chained onto every candidate that validates, whether it
    creates a new row or merges into an existing one. Off by default,
    never touches Phase 0's zero-cost database matches (proven
    separately in TestDiscoverToTargetExistingDatabasePhase, since that
    code path never calls discover()/_process_candidate at all)."""

    def test_off_by_default_never_calls_collect(self, repo):
        results = [_search_result("https://acmetrailer.com/", title="Acme Trailer Co", snippet="trailer axle manufacturer")]
        collection = FakeCollectionService()
        service = _service(repo, results, "acmetrailer.com", collection_service=collection)

        service.discover("trailer axle", country="China")

        assert collection.calls == []

    def test_enabled_calls_collect_for_a_new_supplier(self, repo):
        results = [_search_result("https://acmetrailer.com/", title="Acme Trailer Co", snippet="trailer axle manufacturer")]
        collection = FakeCollectionService()
        service = _service(repo, results, "acmetrailer.com", collection_service=collection)

        outcome = service.discover("trailer axle", country="China", deep_collect=True)

        assert collection.calls == outcome.new_supplier_ids
        assert outcome.deep_collected == 1

    def test_enabled_calls_collect_for_a_merged_supplier_not_yet_collected(self, repo):
        existing_id = repo.create_golden_record({
            "canonical_name": "Acme Trailer Co", "domain": "acmetrailer.com", "country": "China",
        })
        results = [_search_result("https://acmetrailer.com/", title="Acme Trailer Co", snippet="trailer axle manufacturer")]
        collection = FakeCollectionService()
        service = _service(repo, results, "acmetrailer.com", collection_service=collection)

        outcome = service.discover("trailer axle", country="China", deep_collect=True)

        assert collection.calls == [existing_id]
        assert outcome.deep_collected == 1

    def test_skips_a_merged_supplier_already_collected(self, repo):
        """The idempotency guard: a merge landing on a supplier
        already fully collected by something else must never re-pay
        for a real headless-browser visit."""
        existing_id = repo.create_golden_record({
            "canonical_name": "Acme Trailer Co", "domain": "acmetrailer.com", "country": "China",
            "collection_status": "success",
        })
        results = [_search_result("https://acmetrailer.com/", title="Acme Trailer Co", snippet="trailer axle manufacturer")]
        collection = FakeCollectionService()
        service = _service(repo, results, "acmetrailer.com", collection_service=collection)

        outcome = service.discover("trailer axle", country="China", deep_collect=True)

        assert collection.calls == []
        assert outcome.deep_collected == 0

    def test_collect_failure_does_not_abort_or_lose_the_supplier(self, repo):
        results = [_search_result("https://acmetrailer.com/", title="Acme Trailer Co", snippet="trailer axle manufacturer")]
        collection = FakeCollectionService(raise_for_supplier_id=1)
        service = _service(repo, results, "acmetrailer.com", collection_service=collection)

        outcome = service.discover("trailer axle", country="China", deep_collect=True)

        assert outcome.new_supplier_ids == [1]  # supplier still created despite collect() raising
        assert outcome.deep_collected == 0      # not counted, since it genuinely failed
        assert repo.get_supplier(1) is not None

    def test_independent_of_skip_soft_trader_signals(self, repo):
        """A candidate validated via the trader-inclusive gate (e.g. a
        companies_house_sic-sourced dealer/installer) gets exactly the
        same deep collect as a strict manufacturer match -- this only
        checks resolve_and_store succeeded, never how validation
        passed. Exercises _record_validation_outcome directly rather
        than simulating a full companies_house_sic round, since that's
        the exact seam this independence claim lives at."""
        from discovery.candidate_extractor import Candidate
        from discovery.discovery_service import DiscoveryOutcome

        collection = FakeCollectionService()
        service = _service(
            repo, [], "installer.example.com",
            candidate_validator=FakeCandidateValidator(), collection_service=collection,
        )
        candidate = Candidate(
            title="Acme Solar Installers", link="https://installer.example.com/",
            snippet="", domain="installer.example.com",
        )
        validation = ValidationResult(
            candidate, True, "Acme Solar Installers", "United Kingdom", 95.0,
            "validated: dealer/installer, self-declaration allowed (skip_soft_trader_signals=True)",
        )
        outcome = DiscoveryOutcome()

        service._record_validation_outcome(
            candidate, validation, "solar panel", "United Kingdom", outcome,
            "discovery", None, deep_collect=True,
        )

        assert len(collection.calls) == 1
        assert outcome.deep_collected == 1


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


class TestDomainRecovery:
    """recover_dead_domains=False (the default) must leave every
    existing test above byte-identical -- these tests only exercise
    the opt-in path. See discovery/candidate_validator.py's own
    TestRecover for the real validate()-gate mechanics; these tests
    are about discover()'s WIRING around it (which rejections trigger
    it, no double-validation, correct bookkeeping)."""

    def test_disabled_by_default_recover_is_never_called(self, repo):
        results = [_search_result("https://deadsite.example.com/", title="Acme Trailer Co")]
        from discovery.candidate_extractor import Candidate
        candidate = Candidate(title="Acme Trailer Co", link="https://deadsite.example.com/", snippet="", domain="deadsite.example.com")
        validator = FakeCandidateValidator(outcomes={
            "deadsite.example.com": ValidationResult(candidate, False, None, None, None, "could not fetch candidate site: DNS failure"),
        })
        service = _service(repo, results, "deadsite.example.com", candidate_validator=validator)

        service.discover("trailer axle")  # recover_dead_domains omitted -- defaults False

        assert validator.recover_calls == []

    def test_dead_domain_triggers_recovery_and_creates_the_recovered_supplier(self, repo):
        from discovery.candidate_extractor import Candidate
        dead_candidate = Candidate(title="Acme Trailer Co", link="https://deadsite.example.com/", snippet="", domain="deadsite.example.com")
        recovered_candidate = Candidate(title="Acme Trailer Co", link="https://acmetrailer-real.com/", snippet="", domain="acmetrailer-real.com")
        recovered_result = ValidationResult(
            recovered_candidate, True, "Acme Trailer Co", "UK", 95.0,
            "validated: name corroborated (score=95), product term found on page",
        )
        validator = FakeCandidateValidator(
            outcomes={"deadsite.example.com": ValidationResult(dead_candidate, False, None, None, None, "could not fetch candidate site: DNS failure")},
            recovery_result=recovered_result,
        )
        results = [_search_result("https://deadsite.example.com/", title="Acme Trailer Co")]
        service = _service(repo, results, "deadsite.example.com", candidate_validator=validator)

        outcome = service.discover("trailer axle", recover_dead_domains=True)

        assert len(validator.recover_calls) == 1
        assert validator.recover_calls[0][0] == "Acme Trailer Co"  # company_name = the dead candidate's title
        assert validator.calls == [("deadsite.example.com", "trailer axle")]  # validate() called once, NOT twice for the recovered candidate
        assert outcome.candidates_examined == 2  # the dead attempt + the recovered one, both real
        assert outcome.candidates_rejected == 1
        assert outcome.candidates_validated == 1
        assert len(outcome.new_supplier_ids) == 1

    @pytest.mark.parametrize("reason", [
        "candidate domain is a known B2B marketplace host: alibaba.com",
        "page self-identifies as a trading company/distributor (matched phrase: 'we are a distributor') -- excluded, not a manufacturer",
        "fetched page text does not mention the searched term 'trailer axle'",
        "extracted name 'Wrong Co' does not match the original search result (score=20)",
    ])
    def test_recovery_not_triggered_for_non_dead_domain_rejections(self, repo, reason):
        from discovery.candidate_extractor import Candidate
        candidate = Candidate(title="Acme Trailer Co", link="https://realsite.example.com/", snippet="", domain="realsite.example.com")
        validator = FakeCandidateValidator(
            outcomes={"realsite.example.com": ValidationResult(candidate, False, None, None, None, reason)},
            recovery_result=None,
        )
        results = [_search_result("https://realsite.example.com/", title="Acme Trailer Co")]
        service = _service(repo, results, "realsite.example.com", candidate_validator=validator)

        service.discover("trailer axle", recover_dead_domains=True)

        assert validator.recover_calls == []

    def test_recovery_returning_none_leaves_the_original_rejection_in_place(self, repo):
        from discovery.candidate_extractor import Candidate
        candidate = Candidate(title="Acme Trailer Co", link="https://deadsite.example.com/", snippet="", domain="deadsite.example.com")
        validator = FakeCandidateValidator(
            outcomes={"deadsite.example.com": ValidationResult(candidate, False, None, None, None, "could not fetch candidate site: timeout")},
            recovery_result=None,
        )
        results = [_search_result("https://deadsite.example.com/", title="Acme Trailer Co")]
        service = _service(repo, results, "deadsite.example.com", candidate_validator=validator)

        outcome = service.discover("trailer axle", recover_dead_domains=True)

        assert len(validator.recover_calls) == 1
        assert outcome.candidates_rejected == 1
        assert outcome.candidates_validated == 0
        assert outcome.new_supplier_ids == []


class TestDiscoverMaxCandidates:

    def test_stops_once_max_candidates_reached_across_queries(self, repo):
        results = [
            _search_result(f"https://company{i}.example.com/", title=f"Company {i}", snippet="trailer axle manufacturer")
            for i in range(10)
        ]
        service = _service(repo, results, "company0.example.com")

        outcome = service.discover("trailer axle", max_candidates=3)

        assert outcome.candidates_found == 3


def _multi_candidate_service(repo, n=5):
    """n candidates, all of which will validate successfully -- for
    tests that need more than one real candidate in play (target_count
    early-stop, progress-callback ordering)."""
    from discovery.candidate_extractor import Candidate
    results = [
        _search_result(f"https://company{i}.example.com/", title=f"Company {i}", snippet="trailer axle manufacturer")
        for i in range(n)
    ]
    outcomes = {}
    for i in range(n):
        domain = f"company{i}.example.com"
        candidate = Candidate(title=f"Company {i}", link=f"https://{domain}/", snippet="trailer axle manufacturer", domain=domain)
        outcomes[domain] = ValidationResult(
            candidate, True, f"Company {i}", "China", 95.0,
            "validated: name corroborated (score=95), product term found on page",
        )
    validator = FakeCandidateValidator(outcomes=outcomes)
    service = DiscoveryService(
        repo=repo, google_scraper=FakeGoogleScraper(results=results), website_fetcher=SimpleNamespace(),
        candidate_validator=validator, matcher=SupplierMatcher(repo),
    )
    return service, validator


class TestDiscoverTargetCountEarlyStop:

    def test_stops_validating_once_target_reached(self, repo):
        service, validator = _multi_candidate_service(repo, n=5)

        outcome = service.discover("trailer axle", max_candidates=5, target_count=2)

        assert outcome.candidates_validated == 2
        assert len(validator.calls) == 2  # never validated the remaining 3

    def test_target_count_none_validates_everything_unchanged(self, repo):
        """Regression guard: the default (no target_count) behaviour --
        every collected candidate gets validated -- must be unchanged."""
        service, validator = _multi_candidate_service(repo, n=5)

        outcome = service.discover("trailer axle", max_candidates=5)

        assert outcome.candidates_validated == 5
        assert len(validator.calls) == 5

    def test_target_count_higher_than_available_candidates_validates_all(self, repo):
        service, validator = _multi_candidate_service(repo, n=3)

        outcome = service.discover("trailer axle", max_candidates=3, target_count=10)

        assert outcome.candidates_validated == 3
        assert len(validator.calls) == 3


class TestDiscoverProgressCallback:

    def test_fires_for_a_validated_candidate(self, repo):
        service, _ = _multi_candidate_service(repo, n=1)
        events = []

        service.discover("trailer axle", progress_callback=events.append)

        assert len(events) == 1
        assert events[0].status == "validated"
        assert events[0].domain == "company0.example.com"
        assert events[0].badge == "validated"
        assert events[0].round_examined == 1
        assert events[0].round_validated == 1

    def test_fires_for_a_rejected_candidate_with_the_real_reason(self, repo):
        """Uses a term-missing rejection, not marketplace -- a
        marketplace-host candidate never actually reaches
        _process_candidate via the serpapi path in real life
        (discovery.candidate_extractor.extract_candidates already
        filters those out upstream, see its own module docstring and
        candidate_validator.py's gate-2 comment on the deliberate
        double-filtering); _classify_reason_badge's own mapping for
        "marketplace" is covered directly in TestClassifyReasonBadge
        instead of via this end-to-end path."""
        from discovery.candidate_extractor import Candidate
        domain = "otherco.example.com"
        candidate = Candidate(title="Other Co", link=f"https://{domain}/", snippet="", domain=domain)
        validator = FakeCandidateValidator(outcomes={
            domain: ValidationResult(
                candidate, False, "Other Co", None, None,
                "fetched page text does not mention the searched term 'trailer axle'",
            ),
        })
        service = DiscoveryService(
            repo=repo, google_scraper=FakeGoogleScraper(results=[_search_result(f"https://{domain}/", title="Other Co")]),
            website_fetcher=SimpleNamespace(), candidate_validator=validator, matcher=SupplierMatcher(repo),
        )
        events = []

        service.discover("trailer axle", progress_callback=events.append)

        assert events[0].status == "rejected"
        assert events[0].badge == "term_missing"
        assert events[0].reason == "fetched page text does not mention the searched term 'trailer axle'"

    def test_fires_for_a_validator_exception_as_rejected(self, repo):
        results = [_search_result("https://acmetrailer.com/", title="Acme Trailer Co", snippet="trailer axle manufacturer")]
        validator = FakeCandidateValidator(outcomes={}, raise_for_domain="acmetrailer.com")
        service = _service(repo, results, "acmetrailer.com", candidate_validator=validator)
        events = []

        service.discover("trailer axle", progress_callback=events.append)

        assert events[0].status == "rejected"
        assert "exception" in events[0].reason

    def test_fires_for_a_duplicate_merged_candidate(self, repo):
        repo.create_golden_record({"canonical_name": "Acme Trailer Co", "domain": "acmetrailer.com", "country": "China"})
        results = [_search_result("https://acmetrailer.com/", title="Acme Trailer Co", snippet="trailer axle manufacturer")]
        service = _service(repo, results, "acmetrailer.com")
        events = []

        service.discover("trailer axle", country="China", progress_callback=events.append)

        assert events[0].status == "duplicate"

    def test_callback_exception_does_not_abort_discovery(self, repo):
        service = _service(repo, [_search_result("https://acmetrailer.com/", title="Acme Trailer Co", snippet="trailer axle manufacturer")], "acmetrailer.com")

        def exploding_callback(event):
            raise RuntimeError("progress UI blew up")

        outcome = service.discover("trailer axle", progress_callback=exploding_callback)

        assert outcome.candidates_validated == 1  # discovery itself still completed


class TestClassifyReasonBadge:

    def test_marketplace(self):
        from discovery.discovery_service import _classify_reason_badge
        assert _classify_reason_badge("candidate domain is a known B2B marketplace host: alibaba.com") == "marketplace"

    def test_fetch_failed_variants(self):
        from discovery.discovery_service import _classify_reason_badge
        assert _classify_reason_badge("fetch failed: timeout") == "fetch_failed"
        assert _classify_reason_badge("could not fetch candidate site: 404") == "fetch_failed"
        assert _classify_reason_badge("fetched page had no readable text") == "fetch_failed"

    def test_uk_not_registered(self):
        from discovery.discovery_service import _classify_reason_badge
        assert _classify_reason_badge("no confirmed active UK Companies House registration (...)") == "uk_not_registered"

    def test_term_missing(self):
        from discovery.discovery_service import _classify_reason_badge
        assert _classify_reason_badge("fetched page text does not mention the searched term 'trailer axle'") == "term_missing"

    def test_trader(self):
        from discovery.discovery_service import _classify_reason_badge
        assert _classify_reason_badge("page self-identifies as a trading company/distributor (matched phrase: 'authorised dealer') -- excluded, not a manufacturer") == "trader"

    def test_validated(self):
        from discovery.discovery_service import _classify_reason_badge
        assert _classify_reason_badge("validated: name corroborated (score=95), product term found on page") == "validated"

    def test_name_mismatch(self):
        from discovery.discovery_service import _classify_reason_badge
        assert _classify_reason_badge("extracted name 'Foo Ltd' does not match the original search result (score=40)") == "name_mismatch"

    def test_unrecognised_reason_falls_back_to_other(self):
        from discovery.discovery_service import _classify_reason_badge
        assert _classify_reason_badge("something this function has never seen before") == "other"


class TestDiscoverToTarget:

    def test_round_1_alone_reaches_target_stops_there(self, repo):
        service, validator = _multi_candidate_service(repo, n=5)

        outcome = service.discover_to_target("trailer axle", target_count=2)

        assert outcome.reached_target is True
        assert outcome.stopped_reason == "target_reached"
        assert outcome.rounds_run == 1
        assert outcome.candidates_validated == 2

    def test_ceiling_is_target_times_multiplier(self, repo):
        service, validator = _multi_candidate_service(repo, n=3)

        outcome = service.discover_to_target("trailer axle", target_count=4, max_multiplier=5)

        assert outcome.ceiling == 20

    def test_round_2_triggers_when_round_1_falls_short_but_found_candidates(self, repo):
        """Round 1 (no extra_role_words) only has 2 candidates available
        to it; round 2 (extra_role_words populated) has more -- proves
        discover_to_target actually re-invokes discover() with broadened
        queries rather than just giving up after one round."""
        from discovery.candidate_extractor import Candidate

        def make_outcome(domain, name):
            candidate = Candidate(title=name, link=f"https://{domain}/", snippet="", domain=domain)
            return ValidationResult(candidate, True, name, "UK", 90.0, "validated: name corroborated (score=90), product term found on page")

        base_results = [_search_result("https://mfr1.example.com/", title="Mfr One")]
        role_word_results = [_search_result("https://dealer1.example.com/", title="Dealer One")]

        def scrape(query, max_results=20, **kwargs):
            if "dealer" in query:
                return role_word_results
            return base_results

        google_scraper = FakeGoogleScraper()
        google_scraper.scrape = scrape
        validator = FakeCandidateValidator(outcomes={
            "mfr1.example.com": make_outcome("mfr1.example.com", "Mfr One"),
            "dealer1.example.com": make_outcome("dealer1.example.com", "Dealer One"),
        })
        service = DiscoveryService(
            repo=repo, google_scraper=google_scraper, website_fetcher=SimpleNamespace(),
            candidate_validator=validator, matcher=SupplierMatcher(repo),
        )

        outcome = service.discover_to_target("forklift", target_count=2, max_multiplier=10)

        assert outcome.rounds_run == 2
        assert outcome.candidates_validated == 2
        assert outcome.reached_target is True

    def test_same_domain_revalidated_across_rounds_does_not_inflate_candidates_validated(self, repo):
        """The exact bug found live: round 2's role-word-broadened query
        can re-surface a domain round 1 already validated and created --
        a real, independent SerpAPI hit (extract_candidates already
        dedupes WITHIN one round; this is the ACROSS-rounds case it
        can't catch). Re-validating it (a real second fetch+LLM call)
        merges it back into the very supplier round 1 just created --
        zero new distinct companies, but candidates_validated was
        summing every successful validate() pass, inflating past the
        true distinct-company count (a live run showed "8 validated"
        for only 4 actual distinct companies)."""
        from discovery.candidate_extractor import Candidate

        def make_outcome(domain, name):
            candidate = Candidate(title=name, link=f"https://{domain}/", snippet="", domain=domain)
            return ValidationResult(candidate, True, name, "UK", 90.0, "validated: name corroborated (score=90), product term found on page")

        base_results = [_search_result("https://sharedco.example.com/", title="Shared Co")]
        role_word_results = [_search_result("https://sharedco.example.com/", title="Shared Co")]

        def scrape(query, max_results=20, **kwargs):
            if "dealer" in query:
                return role_word_results
            return base_results

        google_scraper = FakeGoogleScraper()
        google_scraper.scrape = scrape
        validator = FakeCandidateValidator(outcomes={
            "sharedco.example.com": make_outcome("sharedco.example.com", "Shared Co"),
        })
        service = DiscoveryService(
            repo=repo, google_scraper=google_scraper, website_fetcher=SimpleNamespace(),
            candidate_validator=validator, matcher=SupplierMatcher(repo),
        )

        outcome = service.discover_to_target("forklift", target_count=2, max_multiplier=10, allow_llm_fallback=False)

        assert outcome.rounds_run == 2
        assert len(outcome.new_supplier_ids) == 1  # one real company, found twice
        assert outcome.candidates_duplicate == 1   # round 2's re-hit correctly recorded as a merge
        assert outcome.candidates_validated == 1   # NOT 2 -- must match new_supplier_ids, not raw validate() passes
        # duplicate_supplier_ids records round 2's merge target -- which is
        # THIS SAME RUN's own round-1 supplier, so it overlaps
        # new_supplier_ids. A caller combining both lists (e.g. the Find
        # Suppliers results screen) must dedupe, never assume disjoint.
        assert outcome.duplicate_supplier_ids == outcome.new_supplier_ids

    def test_no_round_2_when_round_1_found_zero_raw_candidates(self, repo):
        google_scraper = FakeGoogleScraper(results=[])
        validator = FakeCandidateValidator(outcomes={})
        service = DiscoveryService(
            repo=repo, google_scraper=google_scraper, website_fetcher=SimpleNamespace(),
            candidate_validator=validator, matcher=SupplierMatcher(repo),
        )

        outcome = service.discover_to_target("nonexistent widget", target_count=5)

        assert outcome.rounds_run == 1
        assert outcome.stopped_reason == "no_new_candidates_found"

    def test_llm_fallback_used_only_as_round_3_last_resort(self, repo):
        from discovery.llm_candidate_source import GenerationStats

        google_scraper = FakeGoogleScraper(results=[])  # rounds 1+2 find nothing real
        llm_source = FakeLLMCandidateSource(candidates=[])
        validator = FakeCandidateValidator(outcomes={})
        service = DiscoveryService(
            repo=repo, google_scraper=google_scraper, website_fetcher=SimpleNamespace(),
            candidate_validator=validator, matcher=SupplierMatcher(repo), llm_candidate_source=llm_source,
        )

        # Force round 1 to report at least one raw candidate found (so
        # round 2 isn't skipped) but zero validated, by giving google
        # results with no matching validator outcome -- everything
        # rejected, forcing the loop through to round 3.
        google_scraper._results = [_search_result("https://x.example.com/", title="X")]

        outcome = service.discover_to_target("trailer axle", target_count=5, max_multiplier=3)

        assert outcome.used_llm_fallback is True
        assert outcome.rounds_run == 3
        assert len(llm_source.calls) == 1

    def test_llm_fallback_disabled_stops_after_round_2(self, repo):
        google_scraper = FakeGoogleScraper(results=[_search_result("https://x.example.com/", title="X")])
        validator = FakeCandidateValidator(outcomes={})  # nothing ever validates
        service = DiscoveryService(
            repo=repo, google_scraper=google_scraper, website_fetcher=SimpleNamespace(),
            candidate_validator=validator, matcher=SupplierMatcher(repo),
        )

        outcome = service.discover_to_target("trailer axle", target_count=5, allow_llm_fallback=False)

        assert outcome.used_llm_fallback is False
        assert outcome.rounds_run == 2

    def test_progress_events_are_stamped_with_their_round_number(self, repo):
        service, validator = _multi_candidate_service(repo, n=5)
        events = []

        service.discover_to_target("trailer axle", target_count=2, progress_callback=events.append)

        assert all(e.round == 1 for e in events)


class TestDiscoverToTargetExistingDatabasePhase:
    """Phase 0 -- the database-first check before spending on fresh
    discovery, same mechanism sourcing.sourcing_agent.
    SourcingAgentService.run()'s own Phase 1 already uses
    (search_suppliers_full(product_query=...)). No formal category tag
    involved: a supplier's product_keywords already carries the exact
    product string from whichever prior discovery run found it (see
    discovery_service.py's _record_validation_outcome), so a plain
    product-term search here is already the right shape of match."""

    def test_existing_match_reduces_target_before_round_1(self, repo):
        repo.create_golden_record({"canonical_name": "Acme Trailer Axles", "domain": "acme-axles.example.com", "product_keywords": ["trailer axle"]})
        google_scraper = FakeGoogleScraper(results=[
            _search_result("https://newco.example.com/", title="New Co", snippet="trailer axle manufacturer"),
        ])
        from discovery.candidate_extractor import Candidate
        outcome_map = {
            "newco.example.com": ValidationResult(
                Candidate(title="New Co", link="https://newco.example.com/", snippet="", domain="newco.example.com"),
                True, "New Co", "UK", 90.0, "validated: name corroborated (score=90), product term found on page",
            ),
        }
        validator = FakeCandidateValidator(outcomes=outcome_map)
        service = DiscoveryService(
            repo=repo, google_scraper=google_scraper, website_fetcher=SimpleNamespace(),
            candidate_validator=validator, matcher=SupplierMatcher(repo),
        )

        outcome = service.discover_to_target("trailer axle", target_count=2)

        assert outcome.existing_supplier_ids != []
        assert outcome.rounds_run == 1  # only the shortfall (1, not 2) needed fresh discovery
        assert outcome.reached_target is True
        assert len(outcome.new_supplier_ids) == 1  # exactly the shortfall, not the full target_count

    def test_database_alone_reaches_target_no_fresh_discovery_spent(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Trailer Axles", "domain": "acme-axles.example.com", "product_keywords": ["trailer axle"]})
        google_scraper = FakeGoogleScraper(results=[
            _search_result("https://newco.example.com/", title="New Co", snippet="trailer axle manufacturer"),
        ])
        validator = FakeCandidateValidator(outcomes={})
        service = DiscoveryService(
            repo=repo, google_scraper=google_scraper, website_fetcher=SimpleNamespace(),
            candidate_validator=validator, matcher=SupplierMatcher(repo),
        )

        outcome = service.discover_to_target("trailer axle", target_count=1)

        assert outcome.existing_supplier_ids == [supplier_id]
        assert outcome.rounds_run == 0
        assert outcome.reached_target is True
        assert outcome.stopped_reason == "target_reached_from_existing_database"
        assert google_scraper.queries == []  # zero spend -- discover() never ran at all

    def test_deep_collect_never_applies_to_phase_0_matches(self, repo):
        """deep_collect=True passed straight through to discover_to_target
        must never trigger a real collect() for a Phase 0 database
        match -- that code path never calls discover()/_process_candidate
        at all, so this is a real structural guarantee, not just a
        runtime check that could drift."""
        repo.create_golden_record({"canonical_name": "Acme Trailer Axles", "domain": "acme-axles.example.com", "product_keywords": ["trailer axle"]})
        collection = FakeCollectionService()
        service = DiscoveryService(
            repo=repo, google_scraper=FakeGoogleScraper(results=[]), website_fetcher=SimpleNamespace(),
            candidate_validator=FakeCandidateValidator(outcomes={}), matcher=SupplierMatcher(repo),
            collection_service=collection,
        )

        outcome = service.discover_to_target("trailer axle", target_count=1, deep_collect=True)

        assert outcome.stopped_reason == "target_reached_from_existing_database"
        assert collection.calls == []
        assert outcome.deep_collected == 0

    def test_no_existing_match_behaves_exactly_as_before(self, repo):
        """No pre-existing supplier at all -- Phase 0 finds nothing,
        remaining_target == target_count, rounds proceed unchanged."""
        service, validator = _multi_candidate_service(repo, n=5)

        outcome = service.discover_to_target("trailer axle", target_count=2)

        assert outcome.existing_supplier_ids == []
        assert outcome.rounds_run == 1
        assert len(outcome.new_supplier_ids) == 2

    def test_flagged_existing_supplier_never_counted(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Old Broker Co", "domain": "oldbroker.example.com", "product_keywords": ["trailer axle"]})
        repo.update_supplier_fields_with_history(
            supplier_id, {"flagged": True, "flag_reason": "not a real manufacturer"}, changed_by="manual",
        )
        service, validator = _multi_candidate_service(repo, n=5)

        outcome = service.discover_to_target("trailer axle", target_count=2)

        assert outcome.existing_supplier_ids == []
        assert supplier_id not in outcome.existing_supplier_ids

    def test_category_param_also_matches_via_primary_categories(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme Fasteners", "domain": "acme-fasteners.example.com",
            "primary_categories": ["Metal Pressing"],
        })
        google_scraper = FakeGoogleScraper(results=[])
        validator = FakeCandidateValidator(outcomes={})
        service = DiscoveryService(
            repo=repo, google_scraper=google_scraper, website_fetcher=SimpleNamespace(),
            candidate_validator=validator, matcher=SupplierMatcher(repo),
        )

        outcome = service.discover_to_target("fasteners", target_count=1, category="Metal Pressing")

        assert outcome.existing_supplier_ids == [supplier_id]

    def test_progress_events_fired_for_existing_matches_at_round_zero(self, repo):
        repo.create_golden_record({"canonical_name": "Acme Trailer Axles", "domain": "acme-axles.example.com", "product_keywords": ["trailer axle"]})
        google_scraper = FakeGoogleScraper(results=[])
        validator = FakeCandidateValidator(outcomes={})
        service = DiscoveryService(
            repo=repo, google_scraper=google_scraper, website_fetcher=SimpleNamespace(),
            candidate_validator=validator, matcher=SupplierMatcher(repo),
        )
        events = []

        service.discover_to_target("trailer axle", target_count=1, progress_callback=events.append)

        assert len(events) == 1
        assert events[0].round == 0
        assert events[0].status == "existing"


class TestDiscoverToTargetPhase05TradeSource:
    """Phase 0.5 -- opt-in (check_trade_source=True), informational
    only. Tests the WIRING only (does discover_to_target call the
    finder when asked, does a real candidate fire the right event, does
    Round 1 proceed regardless) -- the finder's own fetchability logic
    is covered in isolation by tests/test_trade_source_finder.py, so
    these monkeypatch discovery.discovery_service.find_candidate_trade_source
    directly rather than re-testing real search+fetch behaviour through
    two layers of fakes."""

    def test_off_by_default_never_called(self, repo, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "discovery.discovery_service.find_candidate_trade_source",
            lambda *a, **k: calls.append((a, k)),
        )
        service, _ = _multi_candidate_service(repo, n=1)

        service.discover_to_target("trailer axle", target_count=1)

        assert calls == []

    def test_enabled_calls_the_finder_with_the_product(self, repo, monkeypatch):
        calls = []

        def fake_find(product, google_scraper=None, **kwargs):
            calls.append(product)
            return None

        monkeypatch.setattr("discovery.discovery_service.find_candidate_trade_source", fake_find)
        service, _ = _multi_candidate_service(repo, n=1)

        service.discover_to_target("trailer axle", target_count=1, check_trade_source=True)

        assert calls == ["trailer axle"]

    def test_found_candidate_fires_a_round_zero_trade_source_event(self, repo, monkeypatch):
        from discovery.trade_source_finder import TradeSourceCandidate

        monkeypatch.setattr(
            "discovery.discovery_service.find_candidate_trade_source",
            lambda *a, **k: TradeSourceCandidate(domain="assoc.example", title="Trailer Axle Assoc", snippet="A real trade body"),
        )
        service, _ = _multi_candidate_service(repo, n=1)
        events = []

        service.discover_to_target("trailer axle", target_count=1, check_trade_source=True, progress_callback=events.append)

        trade_events = [e for e in events if e.status == "trade_source_found"]
        assert len(trade_events) == 1
        assert trade_events[0].round == 0
        assert trade_events[0].domain == "assoc.example"
        assert trade_events[0].candidate_title == "Trailer Axle Assoc"
        assert trade_events[0].badge == "trade_source"

    def test_no_candidate_found_fires_no_event(self, repo, monkeypatch):
        monkeypatch.setattr("discovery.discovery_service.find_candidate_trade_source", lambda *a, **k: None)
        service, _ = _multi_candidate_service(repo, n=1)
        events = []

        service.discover_to_target("trailer axle", target_count=1, check_trade_source=True, progress_callback=events.append)

        assert all(e.status != "trade_source_found" for e in events)

    def test_finder_exception_does_not_block_round_1(self, repo, monkeypatch):
        def raiser(*a, **k):
            raise RuntimeError("serpapi down")

        monkeypatch.setattr("discovery.discovery_service.find_candidate_trade_source", raiser)
        service, _ = _multi_candidate_service(repo, n=1)

        outcome = service.discover_to_target("trailer axle", target_count=1, check_trade_source=True)

        assert outcome.reached_target is True
        assert outcome.rounds_run == 1

    def test_skipped_entirely_when_phase_0_already_reached_target(self, repo, monkeypatch):
        """Phase 0.5 only makes sense as a pre-Round-1 step -- if the
        existing database alone already satisfies target_count, there's
        no fresh discovery about to happen, so the extra search would
        be pure waste."""
        repo.create_golden_record({"canonical_name": "Acme Trailer Axles", "domain": "acme-axles.example.com", "product_keywords": ["trailer axle"]})
        calls = []
        monkeypatch.setattr(
            "discovery.discovery_service.find_candidate_trade_source",
            lambda *a, **k: calls.append(a) or None,
        )
        service, _ = _multi_candidate_service(repo, n=1)

        service.discover_to_target("trailer axle", target_count=1, check_trade_source=True)

        assert calls == []


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


class FakeCompaniesHouseSicSource:
    """Mirrors FakeLLMCandidateSource's convention exactly."""

    def __init__(self, candidates=None, stats=None):
        from discovery.companies_house_sic_source import SicGenerationStats
        self._candidates = candidates or []
        self._stats = stats or SicGenerationStats(
            companies_found=len(self._candidates), deduplicated=len(self._candidates),
        )
        self.calls = []

    def find_candidates(self, sic_codes, max_candidates=20, name_keywords=None):
        self.calls.append((sic_codes, max_candidates, name_keywords))
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


class TestDiscoverCompaniesHouseSicSource:
    """source='companies_house_sic' must flow through the exact same
    CandidateValidator/SupplierMatcher pipeline as source='serpapi',
    but with skip_soft_trader_signals=True (see
    discovery/companies_house_sic_source.py's own docstring for why)."""

    def _sic_service(self, repo, candidates, validated_domain, extracted_name="Acme Handling Ltd", extracted_country="United Kingdom", **overrides):
        sic_source = overrides.get("companies_house_sic_source") or FakeCompaniesHouseSicSource(candidates=candidates)
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
            companies_house_sic_source=sic_source,
        )

    def test_requires_sic_codes(self, repo):
        from discovery.candidate_extractor import Candidate
        candidate = Candidate(title="Acme Handling Ltd", link="https://acmehandling.co.uk", snippet="CH #123", domain="acmehandling.co.uk")
        service = self._sic_service(repo, [candidate], "acmehandling.co.uk")
        with pytest.raises(ValueError, match="requires sic_codes"):
            service.discover("material handling equipment", source="companies_house_sic")

    def test_uses_companies_house_sic_source_not_google_scraper(self, repo):
        from discovery.candidate_extractor import Candidate
        candidate = Candidate(title="Acme Handling Ltd", link="https://acmehandling.co.uk", snippet="CH #123", domain="acmehandling.co.uk")
        google_scraper = FakeGoogleScraper(results=[])
        sic_source = FakeCompaniesHouseSicSource(candidates=[candidate])
        service = self._sic_service(repo, [candidate], "acmehandling.co.uk", companies_house_sic_source=sic_source)
        service.google_scraper = google_scraper

        service.discover("material handling equipment", source="companies_house_sic", sic_codes=["28220", "46140"])

        assert sic_source.calls == [(["28220", "46140"], 20, None)]
        assert google_scraper.queries == []

    def test_sic_name_keywords_is_passed_through_to_the_source(self, repo):
        from discovery.candidate_extractor import Candidate
        candidate = Candidate(title="Acme Handling Ltd", link="https://acmehandling.co.uk", snippet="CH #123", domain="acmehandling.co.uk")
        sic_source = FakeCompaniesHouseSicSource(candidates=[candidate])
        service = self._sic_service(repo, [candidate], "acmehandling.co.uk", companies_house_sic_source=sic_source)

        service.discover(
            "material handling equipment", source="companies_house_sic",
            sic_codes=["28220"], sic_name_keywords=["forklift", "handling"],
        )

        assert sic_source.calls == [(["28220"], 20, ["forklift", "handling"])]

    def test_writes_raw_source_data_with_companies_house_sic_provenance(self, repo):
        from discovery.candidate_extractor import Candidate
        candidate = Candidate(title="Acme Handling Ltd", link="https://acmehandling.co.uk", snippet="CH #123", domain="acmehandling.co.uk")
        service = self._sic_service(repo, [candidate], "acmehandling.co.uk")

        service.discover("material handling equipment", source="companies_house_sic", sic_codes=["28220"])

        from storage.database import connection_scope
        with connection_scope(repo.db_path) as conn:
            discovery_rows = conn.execute("SELECT * FROM raw_source_data WHERE source = 'discovery'").fetchall()
            sic_rows = conn.execute("SELECT * FROM raw_source_data WHERE source = 'companies-house-sic'").fetchall()
        assert discovery_rows == []
        assert len(sic_rows) == 1
        assert sic_rows[0]["golden_record_id"] is not None

    def test_still_creates_a_supplier_via_the_same_validator_and_matcher(self, repo):
        from discovery.candidate_extractor import Candidate
        candidate = Candidate(title="Acme Handling Ltd", link="https://acmehandling.co.uk", snippet="CH #123", domain="acmehandling.co.uk")
        service = self._sic_service(repo, [candidate], "acmehandling.co.uk")

        outcome = service.discover("material handling equipment", source="companies_house_sic", sic_codes=["28220"])

        assert len(outcome.new_supplier_ids) == 1
        supplier = repo.get_supplier(outcome.new_supplier_ids[0])
        assert supplier["canonical_name"] == "Acme Handling Ltd"
        assert supplier["domain"] == "acmehandling.co.uk"

    def test_validator_is_called_with_skip_soft_trader_signals_true(self, repo):
        from discovery.candidate_extractor import Candidate
        candidate = Candidate(title="Acme Handling Ltd", link="https://acmehandling.co.uk", snippet="CH #123", domain="acmehandling.co.uk")

        class RecordingValidator:
            def __init__(self):
                self.calls = []

            def validate(self, candidate, product_term, skip_soft_trader_signals=False):
                self.calls.append(skip_soft_trader_signals)
                return ValidationResult(candidate, True, "Acme Handling Ltd", "United Kingdom", 95.0, "validated: name corroborated")

        validator = RecordingValidator()
        service = self._sic_service(repo, [candidate], "acmehandling.co.uk", candidate_validator=validator)

        service.discover("material handling equipment", source="companies_house_sic", sic_codes=["28220"])

        assert validator.calls == [True]


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


class TestExportForBatchUpload:
    """discovery.discovery_service.DiscoveryService.export_for_batch_upload
    -- the CSV bridge from discover() to batch-upload's fuller
    enrichment pipeline. No fakes needed here beyond the repo itself --
    this reads already-stored supplier rows, it doesn't run discovery."""

    def _service(self, repo):
        return DiscoveryService(repo=repo, google_scraper=SimpleNamespace(), website_fetcher=SimpleNamespace())

    def _discovered(self, repo, name, domain, product="injection moulding manufacturer"):
        return repo.create_golden_record({
            "canonical_name": name, "domain": domain,
            "discovery_source": "discovery_service", "product_keywords": [product],
        })

    def test_exports_company_name_and_website_headers(self, repo, tmp_path):
        self._discovered(repo, "Acme Moulding Co", "acme-moulding.com")
        output = tmp_path / "out.csv"

        path, count = self._service(repo).export_for_batch_upload(
            "injection moulding manufacturer", output_path=str(output),
        )

        assert count == 1
        text = path.read_text(encoding="utf-8-sig")
        lines = text.splitlines()
        assert lines[0] == "Company Name,Website"
        assert lines[1] == "Acme Moulding Co,acme-moulding.com"

    def test_multiple_discovered_suppliers_all_appear(self, repo, tmp_path):
        self._discovered(repo, "First Co", "first.com")
        self._discovered(repo, "Second Co", "second.com")
        output = tmp_path / "out.csv"

        path, count = self._service(repo).export_for_batch_upload(
            "injection moulding manufacturer", output_path=str(output),
        )

        assert count == 2
        text = path.read_text(encoding="utf-8-sig")
        assert "First Co,first.com" in text
        assert "Second Co,second.com" in text

    def test_domain_is_written_bare_without_a_scheme(self, repo, tmp_path):
        """SiteCollector already handles a bare domain via its own
        www/scheme candidate fallback -- no need to format a URL here."""
        self._discovered(repo, "Acme Co", "acme.com")
        output = tmp_path / "out.csv"

        self._service(repo).export_for_batch_upload("injection moulding manufacturer", output_path=str(output))

        text = output.read_text(encoding="utf-8-sig")
        assert "https://" not in text
        assert "acme.com" in text

    def test_suppliers_from_a_different_product_are_excluded(self, repo, tmp_path):
        self._discovered(repo, "Acme Moulding Co", "acme-moulding.com", product="injection moulding manufacturer")
        self._discovered(repo, "Bearing Co", "bearing.com", product="wheel bearings")
        output = tmp_path / "out.csv"

        path, count = self._service(repo).export_for_batch_upload(
            "injection moulding manufacturer", output_path=str(output),
        )

        assert count == 1
        text = path.read_text(encoding="utf-8-sig")
        assert "Bearing Co" not in text

    def test_non_discovered_suppliers_are_never_included(self, repo, tmp_path):
        """A bulk-imported supplier whose product_keywords happens to
        match must never leak into a discovery export."""
        repo.create_golden_record({
            "canonical_name": "Bulk Import Co", "domain": "bulkimport.com",
            "product_keywords": ["injection moulding manufacturer"],
        })
        output = tmp_path / "out.csv"

        path, count = self._service(repo).export_for_batch_upload(
            "injection moulding manufacturer", output_path=str(output),
        )

        assert count == 0
        text = path.read_text(encoding="utf-8-sig")
        assert "Bulk Import Co" not in text

    def test_no_matches_still_writes_a_header_only_csv(self, repo, tmp_path):
        output = tmp_path / "out.csv"

        path, count = self._service(repo).export_for_batch_upload(
            "nonexistent product", output_path=str(output),
        )

        assert count == 0
        assert path.read_text(encoding="utf-8-sig").strip() == "Company Name,Website"

    def test_default_output_path_is_a_slug_of_the_product(self, repo, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._discovered(repo, "Acme Moulding Co", "acme-moulding.com")

        path, count = self._service(repo).export_for_batch_upload("injection moulding manufacturer")

        assert count == 1
        assert path.name == "discovered_injection_moulding_manufacturer.csv"
        assert path.exists()

    def test_missing_domain_exports_as_empty_not_none(self, repo, tmp_path):
        """domain isn't required to create a golden record (unlike
        canonical_name) -- must export as an empty cell, not the
        literal string "None"."""
        repo.create_golden_record({
            "canonical_name": "No Domain Co", "discovery_source": "discovery_service",
            "product_keywords": ["injection moulding manufacturer"],
        })
        output = tmp_path / "out.csv"

        self._service(repo).export_for_batch_upload("injection moulding manufacturer", output_path=str(output))

        text = output.read_text(encoding="utf-8-sig")
        assert "No Domain Co," in text
        assert "None" not in text
