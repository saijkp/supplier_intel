"""
tests/test_api_jobs.py

Unit tests for api.jobs.run_pipeline_job's own real logic -- until now
this only had indirect HTTP-level coverage via tests/test_api.py,
which deliberately fakes run_pipeline_job itself out entirely (see
that file's own note on why: it exists to avoid a real pipeline run in
the HTTP test, not to test run_pipeline_job's internals). Written
alongside adding PipelineJobRequest.limit, which needed real new logic
here (renaming "limit" -> "results_limit", building scraper_kwargs via
the same build_limit_scraper_kwargs main.py's own --limit uses) that
had no coverage at all before this.

SupplierIntelligencePipeline is constructed directly inside
run_pipeline_job (not injectable via a parameter), so these tests
monkeypatch api.jobs.SupplierIntelligencePipeline itself to a fake
that records what pipeline.run(...) was actually called with, rather
than exercising a real pipeline/scrapers/network.
"""

from __future__ import annotations

import pytest

import api.jobs as jobs_module
from storage.database import initialise_schema
from storage.repository import SupplierRepository


class FakePipeline:
    """Records the exact kwargs run_pipeline_job calls .run() with, and
    exposes .scrapers (a plain name->None dict) since
    build_limit_scraper_kwargs falls back to list(pipeline.scrapers.keys())
    when no explicit sources are given. Also records calls to the three
    standalone enrichment methods for run_enrichment_job's own tests."""

    last_instance = None

    def __init__(self, repo=None):
        self.repo = repo
        self.scrapers = {name: None for name in ("alibaba", "indiamart", "china_1688", "google", "hktdc")}
        self.last_run_kwargs = None
        self.last_enrichment_call = None
        FakePipeline.last_instance = self

    def run(self, query, **kwargs):
        self.last_run_kwargs = {"query": query, **kwargs}
        return {"scraped": 1, "created": 1}

    def run_website_discovery_only(self, force=False, limit=1000):
        self.last_enrichment_call = ("find_websites", {"force": force, "limit": limit})
        return {"website_discovered": 2}

    def run_capability_extraction_only(self, force=False, limit=1000, assess_photos=True):
        self.last_enrichment_call = (
            "extract_capabilities", {"force": force, "limit": limit, "assess_photos": assess_photos},
        )
        return {"capability_extracted": 3, "contact_emails_added": 1, "contact_phones_added": 1,
                "contact_forms_recorded": 0, "photos_assessed": 0}

    def run_facility_verification_only(self, force=False, limit=1000):
        self.last_enrichment_call = ("verify_facilities", {"force": force, "limit": limit})
        return {"facility_address_verified": 1}


class FailingFakePipeline(FakePipeline):
    def run(self, query, **kwargs):
        raise RuntimeError("boom")


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    initialise_schema(db_path)
    r = SupplierRepository(db_path=db_path)
    monkeypatch.setattr("api.jobs.SupplierRepository", lambda: r)
    return r


class TestRunPipelineJobLimitHandling:

    def test_limit_is_renamed_to_results_limit_not_passed_through_as_limit(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "SupplierIntelligencePipeline", FakePipeline)

        repo.create_pipeline_job(job_id="job1", query="wheel hub", options={"sources": ["alibaba"], "limit": 5})
        jobs_module.run_pipeline_job("job1", "wheel hub", {"sources": ["alibaba"], "limit": 5})

        kwargs = FakePipeline.last_instance.last_run_kwargs
        assert kwargs["results_limit"] == 5
        assert "limit" not in kwargs

    def test_limit_builds_max_results_scraper_kwargs_for_pay_per_event_sources(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "SupplierIntelligencePipeline", FakePipeline)

        repo.create_pipeline_job(job_id="job2", query="wheel hub", options={"sources": ["alibaba"], "limit": 5})
        jobs_module.run_pipeline_job("job2", "wheel hub", {"sources": ["alibaba"], "limit": 5})

        kwargs = FakePipeline.last_instance.last_run_kwargs
        assert kwargs["scraper_kwargs"] == {"alibaba": {"max_results": 5}}

    def test_limit_without_explicit_sources_covers_every_configured_scraper(self, repo, monkeypatch):
        """Mirrors main.py's own --limit-without--source behaviour:
        falls back to every scraper the pipeline actually has, not
        just pay-per-event ones, so a bare limit still avoids
        over-fetching from anything."""
        monkeypatch.setattr(jobs_module, "SupplierIntelligencePipeline", FakePipeline)

        repo.create_pipeline_job(job_id="job3", query="wheel hub", options={"limit": 5})
        jobs_module.run_pipeline_job("job3", "wheel hub", {"limit": 5})

        kwargs = FakePipeline.last_instance.last_run_kwargs
        assert kwargs["scraper_kwargs"]["alibaba"] == {"max_results": 5}
        assert kwargs["scraper_kwargs"]["hktdc"] == {"max_pages": 1}

    def test_no_limit_means_no_scraper_kwargs_and_no_results_limit(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "SupplierIntelligencePipeline", FakePipeline)

        repo.create_pipeline_job(job_id="job4", query="wheel hub", options={"sources": ["alibaba"]})
        jobs_module.run_pipeline_job("job4", "wheel hub", {"sources": ["alibaba"]})

        kwargs = FakePipeline.last_instance.last_run_kwargs
        assert kwargs["scraper_kwargs"] == {}
        assert kwargs["results_limit"] is None

    def test_other_options_still_pass_through_unchanged(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "SupplierIntelligencePipeline", FakePipeline)

        options = {
            "sources": ["alibaba"], "limit": 3,
            "run_verification": False, "run_capability_extraction": True,
        }
        repo.create_pipeline_job(job_id="job5", query="wheel hub", options=options)
        jobs_module.run_pipeline_job("job5", "wheel hub", options)

        kwargs = FakePipeline.last_instance.last_run_kwargs
        assert kwargs["run_verification"] is False
        assert kwargs["run_capability_extraction"] is True


class TestRunEnrichmentJob:

    def test_find_websites_dispatches_to_the_right_pipeline_method(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "SupplierIntelligencePipeline", FakePipeline)

        repo.create_pipeline_job(job_id="job10", query="[enrichment] find_websites", options={"limit": 5})
        jobs_module.run_enrichment_job("job10", "find_websites", {"force": False, "limit": 5})

        stage, kwargs = FakePipeline.last_instance.last_enrichment_call
        assert stage == "find_websites"
        assert kwargs == {"force": False, "limit": 5}
        job = repo.get_pipeline_job("job10")
        assert job["status"] == "completed"
        assert job["stats"]["website_discovered"] == 2

    def test_extract_capabilities_dispatches_with_assess_photos(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "SupplierIntelligencePipeline", FakePipeline)

        repo.create_pipeline_job(job_id="job11", query="[enrichment] extract_capabilities", options={"limit": 5})
        jobs_module.run_enrichment_job(
            "job11", "extract_capabilities", {"force": False, "limit": 5, "assess_photos": True},
        )

        stage, kwargs = FakePipeline.last_instance.last_enrichment_call
        assert stage == "extract_capabilities"
        assert kwargs == {"force": False, "limit": 5, "assess_photos": True}

    def test_extract_capabilities_defaults_assess_photos_to_false(self, repo, monkeypatch):
        """Mirrors EnrichmentJobRequest's own default -- unlike the
        CLI's --extract-capabilities (which assesses photos by
        default), the API defaults photos off since it's the most
        expensive part of this stage."""
        monkeypatch.setattr(jobs_module, "SupplierIntelligencePipeline", FakePipeline)

        repo.create_pipeline_job(job_id="job12", query="[enrichment] extract_capabilities", options={})
        jobs_module.run_enrichment_job("job12", "extract_capabilities", {})

        stage, kwargs = FakePipeline.last_instance.last_enrichment_call
        assert kwargs["assess_photos"] is False

    def test_verify_facilities_dispatches_with_limit(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "SupplierIntelligencePipeline", FakePipeline)

        repo.create_pipeline_job(job_id="job13", query="[enrichment] verify_facilities", options={"limit": 5})
        jobs_module.run_enrichment_job("job13", "verify_facilities", {"force": True, "limit": 5})

        stage, kwargs = FakePipeline.last_instance.last_enrichment_call
        assert stage == "verify_facilities"
        assert kwargs == {"force": True, "limit": 5}

    def test_no_limit_defaults_to_1000_not_none(self, repo, monkeypatch):
        """None (the API default, meaning "no cap") must not be passed
        straight to the pipeline methods, whose own default is the
        int 1000 -- passing None through would be a TypeError against
        the SQL LIMIT ? parameter binding."""
        monkeypatch.setattr(jobs_module, "SupplierIntelligencePipeline", FakePipeline)

        repo.create_pipeline_job(job_id="job14", query="[enrichment] find_websites", options={})
        jobs_module.run_enrichment_job("job14", "find_websites", {})

        _, kwargs = FakePipeline.last_instance.last_enrichment_call
        assert kwargs["limit"] == 1000

    def test_unknown_stage_marks_job_failed_not_raised(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "SupplierIntelligencePipeline", FakePipeline)

        repo.create_pipeline_job(job_id="job15", query="[enrichment] bogus", options={})
        jobs_module.run_enrichment_job("job15", "bogus_stage", {})  # must not raise

        job = repo.get_pipeline_job("job15")
        assert job["status"] == "failed"
        assert "bogus_stage" in job["error"]


class TestRunPipelineJobOutcomes:

    def test_successful_run_marks_job_completed_with_stats(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "SupplierIntelligencePipeline", FakePipeline)

        repo.create_pipeline_job(job_id="job6", query="wheel hub", options={})
        jobs_module.run_pipeline_job("job6", "wheel hub", {})

        job = repo.get_pipeline_job("job6")
        assert job["status"] == "completed"
        assert job["stats"]["created"] == 1

    def test_failing_run_marks_job_failed_not_raised(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "SupplierIntelligencePipeline", FailingFakePipeline)

        repo.create_pipeline_job(job_id="job7", query="wheel hub", options={})
        jobs_module.run_pipeline_job("job7", "wheel hub", {})  # must not raise

        job = repo.get_pipeline_job("job7")
        assert job["status"] == "failed"
        assert "boom" in job["error"]


class FakeCollectionService:
    """CollectionService is constructed directly inside
    run_collection_job (not injectable via a parameter), same pattern
    as SupplierIntelligencePipeline in run_pipeline_job -- these tests
    monkeypatch api.jobs.CollectionService itself."""

    last_instance = None

    def __init__(self, repo=None):
        self.repo = repo
        self.last_collect_call = None
        self.last_collect_pending_call = None
        FakeCollectionService.last_instance = self

    def collect(self, supplier_id):
        self.last_collect_call = supplier_id
        return {"supplier_id": supplier_id, "status": "success", "pages_visited": 3}

    def collect_pending(self, limit=20, force=False):
        self.last_collect_pending_call = {"limit": limit, "force": force}
        return {"attempted": 2, "succeeded": 2, "failed": 0, "total_eligible": 2, "status": "completed"}


class FailingFakeCollectionService(FakeCollectionService):
    def collect(self, supplier_id):
        raise RuntimeError("browser crashed")

    def collect_pending(self, limit=20, force=False):
        raise RuntimeError("browser crashed")


class TestRunCollectionJob:

    def test_supplier_id_dispatches_to_collect(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "CollectionService", FakeCollectionService)

        repo.create_pipeline_job(job_id="job20", query="[collection] supplier #5", options={"supplier_id": 5})
        jobs_module.run_collection_job("job20", {"supplier_id": 5, "pending": False, "limit": 20, "force": False})

        assert FakeCollectionService.last_instance.last_collect_call == 5
        assert FakeCollectionService.last_instance.last_collect_pending_call is None
        job = repo.get_pipeline_job("job20")
        assert job["status"] == "completed"
        assert job["stats"]["pages_visited"] == 3

    def test_pending_dispatches_to_collect_pending_with_limit_and_force(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "CollectionService", FakeCollectionService)

        repo.create_pipeline_job(job_id="job21", query="[collection] pending batch",
                                  options={"pending": True, "limit": 15, "force": True})
        jobs_module.run_collection_job("job21", {"supplier_id": None, "pending": True, "limit": 15, "force": True})

        assert FakeCollectionService.last_instance.last_collect_call is None
        assert FakeCollectionService.last_instance.last_collect_pending_call == {"limit": 15, "force": True}
        job = repo.get_pipeline_job("job21")
        assert job["status"] == "completed"
        assert job["stats"]["attempted"] == 2

    def test_supplier_id_takes_priority_over_pending_if_both_set(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "CollectionService", FakeCollectionService)

        repo.create_pipeline_job(job_id="job22", query="x", options={"supplier_id": 9, "pending": True})
        jobs_module.run_collection_job("job22", {"supplier_id": 9, "pending": True, "limit": 20, "force": False})

        assert FakeCollectionService.last_instance.last_collect_call == 9
        assert FakeCollectionService.last_instance.last_collect_pending_call is None

    def test_failing_job_marks_failed_not_raised(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "CollectionService", FailingFakeCollectionService)

        repo.create_pipeline_job(job_id="job23", query="x", options={"supplier_id": 5})
        jobs_module.run_collection_job("job23", {"supplier_id": 5, "pending": False, "limit": 20, "force": False})  # must not raise

        job = repo.get_pipeline_job("job23")
        assert job["status"] == "failed"
        assert "browser crashed" in job["error"]
