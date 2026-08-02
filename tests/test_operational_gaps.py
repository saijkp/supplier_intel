"""
tests/test_operational_gaps.py

Tests for the operational-gap build: the review-queue merge capability
(Phase 1 Gap 6), incremental re-scrape tracking (Gap 4), and sweep/
campaign mode.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from storage.database import initialise_schema, connection_scope
from storage.repository import SupplierRepository
from pipeline.orchestrator import SupplierIntelligencePipeline
from scrapers.base_scraper import BaseScraper, ScraperResult
from normalizers.hktdc_normalizer import HKTDCNormalizer


@pytest.fixture()
def repo(tmp_path):
    db_path = tmp_path / "test.db"
    initialise_schema(db_path)
    return SupplierRepository(db_path=db_path)


# ═════════════════════════════════════════════════════════════
# Review queue: merge_two_suppliers
# ═════════════════════════════════════════════════════════════

class TestMergeTwoSuppliers:

    def test_merges_non_clobbering_scalars_and_unions_json_fields(self, repo):
        keep_id = repo.create_golden_record({
            "canonical_name": "Foo Co", "country": "China",
            "primary_categories": ["LED Lighting"],
        })
        remove_id = repo.create_golden_record({
            "canonical_name": "Foo Co Variant", "city": "Shenzhen",
            "primary_categories": ["Fasteners"],
        })

        repo.merge_two_suppliers(keep_id, remove_id)

        kept = repo.get_supplier(keep_id)
        assert kept["city"] == "Shenzhen"
        assert kept["country"] == "China"
        assert set(kept["primary_categories"]) == {"LED Lighting", "Fasteners"}

    def test_removes_the_duplicate_record(self, repo):
        keep_id = repo.create_golden_record({"canonical_name": "Foo Co"})
        remove_id = repo.create_golden_record({"canonical_name": "Foo Co Variant"})

        repo.merge_two_suppliers(keep_id, remove_id)

        assert repo.get_supplier(remove_id) is None
        assert repo.get_supplier(keep_id) is not None

    def test_reassigns_shipment_records(self, repo):
        keep_id = repo.create_golden_record({"canonical_name": "Foo Co"})
        remove_id = repo.create_golden_record({"canonical_name": "Foo Co Variant"})
        repo.add_shipment_record({
            "supplier_id": remove_id, "source": "importyeti",
            "consignee_name": "Ifor Williams Trailers", "value_usd": 5000.0,
        })

        repo.merge_two_suppliers(keep_id, remove_id)

        shipments = repo.get_shipments_for_supplier(keep_id)
        assert len(shipments) == 1
        assert shipments[0]["consignee_name"] == "Ifor Williams Trailers"

    def test_reassigns_raw_source_data_links(self, repo):
        keep_id = repo.create_golden_record({"canonical_name": "Foo Co"})
        remove_id = repo.create_golden_record({"canonical_name": "Foo Co Variant"})
        raw_id = repo.save_raw(source="hktdc", raw_data={"a": 1})
        repo.mark_raw_processed(raw_id, golden_record_id=remove_id)

        repo.merge_two_suppliers(keep_id, remove_id)

        raw = repo.get_raw(raw_id)
        assert raw["golden_record_id"] == keep_id

    def test_reassigns_other_pending_dedup_candidates(self, repo):
        keep_id = repo.create_golden_record({"canonical_name": "Foo Co"})
        remove_id = repo.create_golden_record({"canonical_name": "Foo Co Variant"})
        third_id = repo.create_golden_record({"canonical_name": "Third Co"})
        other_candidate_id = repo.add_to_review_queue(third_id, remove_id, match_score=0.8)

        repo.merge_two_suppliers(keep_id, remove_id)

        candidate = repo.get_review_candidate(other_candidate_id)
        assert candidate["supplier_id_b"] == keep_id

    def test_raises_for_same_id(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Foo Co"})
        with pytest.raises(ValueError):
            repo.merge_two_suppliers(supplier_id, supplier_id)

    def test_raises_for_missing_supplier(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Foo Co"})
        with pytest.raises(ValueError):
            repo.merge_two_suppliers(supplier_id, 999999)
        with pytest.raises(ValueError):
            repo.merge_two_suppliers(999999, supplier_id)

    def test_conflicting_uscc_is_preserved_not_overwritten(self, repo):
        """Two suppliers with different, non-null confirmed USCCs should
        never silently swap identities. merge_into_golden's non-clobbering
        rule means keep_id's own uscc always survives — remove_id's is
        discarded along with the rest of that (now-deleted) record. This
        can never raise a UNIQUE-constraint error, since keep_id's value
        is never overwritten with remove_id's differing one."""
        keep_id = repo.create_golden_record({"canonical_name": "Foo Co", "uscc": "91440101MA5ABCDE1M"})
        remove_id = repo.create_golden_record({"canonical_name": "Bar Co", "uscc": "91440101MA5FGHIJ22"})

        repo.merge_two_suppliers(keep_id, remove_id)

        assert repo.get_supplier(keep_id)["uscc"] == "91440101MA5ABCDE1M"
        assert repo.get_supplier(remove_id) is None


class TestResolveReviewCandidateAsMerge:

    def test_default_keeps_b(self, repo):
        matcher_a = repo.create_golden_record({"canonical_name": "New Scrape Co"})
        matcher_b = repo.create_golden_record({"canonical_name": "Established Co"})
        candidate_id = repo.add_to_review_queue(matcher_a, matcher_b, match_score=0.8)

        kept_id = repo.resolve_review_candidate_as_merge(candidate_id)

        assert kept_id == matcher_b
        assert repo.get_supplier(matcher_a) is None
        assert repo.get_supplier(matcher_b) is not None

    def test_keep_a_option(self, repo):
        matcher_a = repo.create_golden_record({"canonical_name": "New Scrape Co"})
        matcher_b = repo.create_golden_record({"canonical_name": "Established Co"})
        candidate_id = repo.add_to_review_queue(matcher_a, matcher_b, match_score=0.8)

        kept_id = repo.resolve_review_candidate_as_merge(candidate_id, keep="a")

        assert kept_id == matcher_a
        assert repo.get_supplier(matcher_b) is None

    def test_marks_candidate_merged(self, repo):
        id_a = repo.create_golden_record({"canonical_name": "A"})
        id_b = repo.create_golden_record({"canonical_name": "B"})
        candidate_id = repo.add_to_review_queue(id_a, id_b, match_score=0.8)

        repo.resolve_review_candidate_as_merge(candidate_id)

        assert repo.get_pending_review_candidates() == []

    def test_raises_on_already_resolved_candidate(self, repo):
        id_a = repo.create_golden_record({"canonical_name": "A"})
        id_b = repo.create_golden_record({"canonical_name": "B"})
        candidate_id = repo.add_to_review_queue(id_a, id_b, match_score=0.8)
        repo.resolve_review_candidate_as_merge(candidate_id)

        with pytest.raises(ValueError, match="already"):
            repo.resolve_review_candidate_as_merge(candidate_id)

    def test_raises_on_unknown_candidate(self, repo):
        with pytest.raises(ValueError):
            repo.resolve_review_candidate_as_merge(999999)

    def test_invalid_keep_value_raises(self, repo):
        id_a = repo.create_golden_record({"canonical_name": "A"})
        id_b = repo.create_golden_record({"canonical_name": "B"})
        candidate_id = repo.add_to_review_queue(id_a, id_b, match_score=0.8)
        with pytest.raises(ValueError):
            repo.resolve_review_candidate_as_merge(candidate_id, keep="c")


class TestResolveReviewCandidateAsReject:

    def test_rejects_without_touching_records(self, repo):
        id_a = repo.create_golden_record({"canonical_name": "A Co"})
        id_b = repo.create_golden_record({"canonical_name": "B Co"})
        candidate_id = repo.add_to_review_queue(id_a, id_b, match_score=0.8)

        repo.resolve_review_candidate_as_reject(candidate_id)

        assert repo.get_supplier(id_a) is not None
        assert repo.get_supplier(id_b) is not None
        assert repo.get_pending_review_candidates() == []
        candidate = repo.get_review_candidate(candidate_id)
        assert candidate["status"] == "rejected"

    def test_raises_on_already_resolved(self, repo):
        id_a = repo.create_golden_record({"canonical_name": "A"})
        id_b = repo.create_golden_record({"canonical_name": "B"})
        candidate_id = repo.add_to_review_queue(id_a, id_b, match_score=0.8)
        repo.resolve_review_candidate_as_reject(candidate_id)
        with pytest.raises(ValueError):
            repo.resolve_review_candidate_as_reject(candidate_id)


# ═════════════════════════════════════════════════════════════
# Incremental scraping
# ═════════════════════════════════════════════════════════════

class TestIncrementalScraping:

    def test_never_scraped_returns_false(self, repo):
        assert repo.was_recently_scraped("alibaba", "LED marker light", within_days=30) is False

    def test_recently_scraped_returns_true(self, repo):
        repo.record_source_query_run("alibaba", "LED marker light", results_count=5)
        assert repo.was_recently_scraped("alibaba", "LED marker light", within_days=30) is True

    def test_old_run_outside_window_returns_false(self, repo):
        with connection_scope(repo.db_path) as conn:
            old_date = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT INTO source_query_runs (source, query, run_at, results_count) VALUES (?, ?, ?, ?)",
                ("alibaba", "LED marker light", old_date, 5),
            )
        assert repo.was_recently_scraped("alibaba", "LED marker light", within_days=30) is False

    def test_zero_days_always_false(self, repo):
        repo.record_source_query_run("alibaba", "LED marker light")
        assert repo.was_recently_scraped("alibaba", "LED marker light", within_days=0) is False

    def test_different_source_not_matched(self, repo):
        repo.record_source_query_run("alibaba", "LED marker light")
        assert repo.was_recently_scraped("hktdc", "LED marker light", within_days=30) is False

    def test_different_query_not_matched(self, repo):
        repo.record_source_query_run("alibaba", "LED marker light")
        assert repo.was_recently_scraped("alibaba", "trailer axle", within_days=30) is False

    def test_get_last_source_query_run(self, repo):
        repo.record_source_query_run("alibaba", "LED marker light", results_count=3)
        repo.record_source_query_run("alibaba", "LED marker light", results_count=7)

        last = repo.get_last_source_query_run("alibaba", "LED marker light")
        assert last["results_count"] == 7


class FakeScraper(BaseScraper):
    def __init__(self, source_name, results=None):
        super().__init__(source_name, enable_delays=False)
        self._results = results or []
        self.call_count = 0

    def scrape(self, query, **kwargs):
        self.call_count += 1
        return self._results


class TestPipelineIncrementalScraping:

    def test_second_run_within_window_is_skipped(self, repo):
        scraper = FakeScraper("hktdc", [
            ScraperResult(source="hktdc", source_id="", raw_data={"company_name": "Foo Co", "country": "China"}, success=True),
        ])
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers={"hktdc": scraper}, normalizers={"hktdc": HKTDCNormalizer()})

        first = pipeline.run("LED marker light", sources=["hktdc"], incremental_days=30)
        second = pipeline.run("LED marker light", sources=["hktdc"], incremental_days=30)

        assert first["scraped"] == 1
        assert second["scraped"] == 0
        assert second["skipped_incremental"] == 1
        assert scraper.call_count == 1

    def test_force_rescrape_bypasses_incremental_skip(self, repo):
        scraper = FakeScraper("hktdc", [
            ScraperResult(source="hktdc", source_id="", raw_data={"company_name": "Foo Co", "country": "China"}, success=True),
        ])
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers={"hktdc": scraper}, normalizers={"hktdc": HKTDCNormalizer()})

        pipeline.run("LED marker light", sources=["hktdc"], incremental_days=30)
        second = pipeline.run("LED marker light", sources=["hktdc"], incremental_days=30, force_rescrape=True)

        assert second["scraped"] == 1
        assert scraper.call_count == 2

    def test_default_incremental_days_zero_always_scrapes(self, repo):
        scraper = FakeScraper("hktdc", [
            ScraperResult(source="hktdc", source_id="", raw_data={"company_name": "Foo Co", "country": "China"}, success=True),
        ])
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers={"hktdc": scraper}, normalizers={"hktdc": HKTDCNormalizer()})

        pipeline.run("LED marker light", sources=["hktdc"])
        second = pipeline.run("LED marker light", sources=["hktdc"])

        assert second["scraped"] == 1
        assert scraper.call_count == 2

    def test_different_query_not_skipped(self, repo):
        scraper = FakeScraper("hktdc", [
            ScraperResult(source="hktdc", source_id="", raw_data={"company_name": "Foo Co", "country": "China"}, success=True),
        ])
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers={"hktdc": scraper}, normalizers={"hktdc": HKTDCNormalizer()})

        pipeline.run("LED marker light", sources=["hktdc"], incremental_days=30)
        second = pipeline.run("trailer axle", sources=["hktdc"], incremental_days=30)

        assert second["scraped"] == 1


# ═════════════════════════════════════════════════════════════
# Sweep / campaign mode
# ═════════════════════════════════════════════════════════════

class TestRunCampaign:

    def test_runs_each_query_and_aggregates_totals(self, repo):
        scraper = FakeScraper("hktdc", [
            ScraperResult(source="hktdc", source_id="", raw_data={"company_name": "Foo Co", "country": "China"}, success=True),
        ])
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers={"hktdc": scraper}, normalizers={"hktdc": HKTDCNormalizer()})

        result = pipeline.run_campaign(
            queries=["LED marker light", "trailer axle"],
            sources=["hktdc"], enable_delays=False,
        )

        assert result["queries_total"] == 2
        assert result["queries_run"] == 2
        assert result["queries_failed"] == 0
        assert result["totals"]["scraped"] == 2
        assert len(result["per_query"]) == 2

    def test_default_queries_use_component_taxonomy(self, repo):
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers={}, normalizers={})
        result = pipeline.run_campaign(sources=[], enable_delays=False)

        from config.settings import TRAILER_COMPONENT_SEARCH_TERMS
        assert result["queries_total"] == len(TRAILER_COMPONENT_SEARCH_TERMS)

    def test_query_failure_is_isolated(self, repo, monkeypatch):
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers={}, normalizers={})

        call_count = {"n": 0}
        original_run = pipeline.run

        def flaky_run(query, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated failure")
            return original_run(query, **kwargs)

        monkeypatch.setattr(pipeline, "run", flaky_run)
        result = pipeline.run_campaign(queries=["query one", "query two"], sources=[], enable_delays=False)

        assert result["queries_run"] == 1
        assert result["queries_failed"] == 1

    def test_results_limit_is_forwarded_to_every_query(self, repo):
        """sweep --limit's own cost-safety cap -- run_campaign forwards
        **run_kwargs straight into self.run() per query, so
        results_limit (and scraper_kwargs, built the same way main.py
        run's --limit builds it) must apply to every term in the
        catalogue, not just the first."""
        scrapers = {
            "hktdc": FakeScraper("hktdc", [
                ScraperResult(source="hktdc", source_id=str(i), raw_data={"company_name": f"Company {i}", "country": "China"}, success=True)
                for i in range(5)
            ]),
        }
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers=scrapers, normalizers={"hktdc": HKTDCNormalizer()})

        result = pipeline.run_campaign(
            queries=["trailer axle", "trailer coupling"],
            sources=["hktdc"], enable_delays=False, results_limit=2,
        )

        assert result["totals"]["scraped"] == 4  # 2 kept per query x 2 queries, not 10
        for query_result in result["per_query"]:
            assert query_result["scraped"] == 2

    def test_incremental_days_defaults_to_30_for_campaigns(self, repo):
        """Campaign mode should default to skipping recently-scraped
        pairs even though run() itself defaults to always-scrape."""
        scraper = FakeScraper("hktdc", [
            ScraperResult(source="hktdc", source_id="", raw_data={"company_name": "Foo Co", "country": "China"}, success=True),
        ])
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers={"hktdc": scraper}, normalizers={"hktdc": HKTDCNormalizer()})

        pipeline.run_campaign(queries=["LED marker light"], sources=["hktdc"], enable_delays=False)
        second = pipeline.run_campaign(queries=["LED marker light"], sources=["hktdc"], enable_delays=False)

        assert second["totals"]["skipped_incremental"] == 1
