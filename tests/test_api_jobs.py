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
    when no explicit sources are given."""

    last_instance = None

    def __init__(self, repo=None):
        self.repo = repo
        self.scrapers = {name: None for name in ("alibaba", "indiamart", "china_1688", "google", "hktdc")}
        self.last_run_kwargs = None
        FakePipeline.last_instance = self

    def run(self, query, **kwargs):
        self.last_run_kwargs = {"query": query, **kwargs}
        return {"scraped": 1, "created": 1}


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
