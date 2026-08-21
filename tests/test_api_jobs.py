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


class FakeVerificationService:
    last_instance = None

    def __init__(self, repo=None):
        self.repo = repo
        self.last_verify_call = None
        self.last_verify_pending_call = None
        self.last_reverify_call = None
        FakeVerificationService.last_instance = self

    def verify(self, supplier_id):
        self.last_verify_call = supplier_id
        return {"supplier_id": supplier_id, "confidence_score": 75, "verdict": "corroborated",
                "inconsistencies": [], "narrative_generated": True}

    def verify_pending(self, limit=20, force=False):
        self.last_verify_pending_call = {"limit": limit, "force": force}
        return {"attempted": 3, "succeeded": 3, "failed": 0, "total_eligible": 3}

    def reverify(self, supplier_id, collection_service=None):
        self.last_reverify_call = supplier_id
        return {
            "collection": {"supplier_id": supplier_id, "status": "success", "pages_visited": 2},
            "verification": {"supplier_id": supplier_id, "confidence_score": 80, "verdict": "corroborated",
                              "inconsistencies": [], "narrative_generated": True},
        }


class FailingFakeVerificationService(FakeVerificationService):
    def verify(self, supplier_id):
        raise RuntimeError("LLM call failed")

    def verify_pending(self, limit=20, force=False):
        raise RuntimeError("LLM call failed")

    def reverify(self, supplier_id, collection_service=None):
        raise RuntimeError("browser crashed")


class TestRunVerificationJob:

    def test_supplier_id_dispatches_to_verify(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "VerificationService", FakeVerificationService)

        repo.create_pipeline_job(job_id="job30", query="[verification] supplier #5", options={"supplier_id": 5})
        jobs_module.run_verification_job("job30", {"supplier_id": 5, "pending": False, "limit": 20, "force": False})

        assert FakeVerificationService.last_instance.last_verify_call == 5
        job = repo.get_pipeline_job("job30")
        assert job["status"] == "completed"
        assert job["stats"]["confidence_score"] == 75

    def test_pending_dispatches_to_verify_pending(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "VerificationService", FakeVerificationService)

        repo.create_pipeline_job(job_id="job31", query="[verification] pending batch",
                                  options={"pending": True, "limit": 10, "force": False})
        jobs_module.run_verification_job("job31", {"supplier_id": None, "pending": True, "limit": 10, "force": False})

        assert FakeVerificationService.last_instance.last_verify_pending_call == {"limit": 10, "force": False}
        job = repo.get_pipeline_job("job31")
        assert job["stats"]["attempted"] == 3

    def test_failing_job_marks_failed_not_raised(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "VerificationService", FailingFakeVerificationService)

        repo.create_pipeline_job(job_id="job32", query="x", options={"supplier_id": 5})
        jobs_module.run_verification_job("job32", {"supplier_id": 5, "pending": False, "limit": 20, "force": False})

        job = repo.get_pipeline_job("job32")
        assert job["status"] == "failed"
        assert "LLM call failed" in job["error"]


class TestRunReverifyJob:

    def test_calls_reverify_with_supplier_id(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "VerificationService", FakeVerificationService)

        repo.create_pipeline_job(job_id="job40", query="[reverify] supplier #7", options={"supplier_id": 7})
        jobs_module.run_reverify_job("job40", 7)

        assert FakeVerificationService.last_instance.last_reverify_call == 7
        job = repo.get_pipeline_job("job40")
        assert job["status"] == "completed"
        assert job["stats"]["verification"]["confidence_score"] == 80

    def test_failing_job_marks_failed_not_raised(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "VerificationService", FailingFakeVerificationService)

        repo.create_pipeline_job(job_id="job41", query="x", options={"supplier_id": 7})
        jobs_module.run_reverify_job("job41", 7)  # must not raise

        job = repo.get_pipeline_job("job41")
        assert job["status"] == "failed"
        assert "browser crashed" in job["error"]


class FakeDiscoveryService:
    last_instance = None

    def __init__(self, repo=None):
        self.repo = repo
        self.last_discover_call = None
        self.last_discover_to_target_call = None
        FakeDiscoveryService.last_instance = self

    def discover(self, product, category=None, country=None, max_candidates=20, source="serpapi",
                 progress_callback=None):
        from discovery.discovery_service import DiscoveryOutcome

        self.last_discover_call = {
            "product": product, "category": category, "country": country,
            "max_candidates": max_candidates, "source": source,
        }
        if progress_callback:
            from discovery.discovery_service import DiscoveryProgressEvent
            progress_callback(DiscoveryProgressEvent(
                domain="acmetrailer.com", candidate_title="Acme Trailer Co", extracted_name="Acme Trailer Co",
                status="validated", reason="validated: name corroborated (score=95), product term found on page",
                badge="validated", round_examined=1, round_validated=1,
            ))
        return DiscoveryOutcome(candidates_found=5, candidates_validated=2, candidates_rejected=3,
                                 new_supplier_ids=[10, 11])

    def discover_to_target(self, product, target_count, category=None, country=None, max_multiplier=5,
                            progress_callback=None):
        from discovery.discovery_service import DiscoveryToTargetOutcome

        self.last_discover_to_target_call = {
            "product": product, "target_count": target_count, "category": category,
            "country": country, "max_multiplier": max_multiplier,
        }
        return DiscoveryToTargetOutcome(
            product=product, target_count=target_count, ceiling=target_count * max_multiplier,
            rounds_run=1, candidates_validated=target_count, reached_target=True,
            stopped_reason="target_reached", new_supplier_ids=[20, 21],
        )


class FailingFakeDiscoveryService(FakeDiscoveryService):
    def discover(self, product, category=None, country=None, max_candidates=20, source="serpapi",
                 progress_callback=None):
        raise RuntimeError("search API down")


class TestRunDiscoveryJob:

    def test_dispatches_with_all_options(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "DiscoveryService", FakeDiscoveryService)

        repo.create_pipeline_job(job_id="job50", query="[discovery] trailer axle",
                                  options={"product": "trailer axle", "category": "Axles", "country": "China", "max_candidates": 15})
        jobs_module.run_discovery_job("job50", {
            "product": "trailer axle", "category": "Axles", "country": "China", "max_candidates": 15,
        })

        call = FakeDiscoveryService.last_instance.last_discover_call
        assert call == {
            "product": "trailer axle", "category": "Axles", "country": "China",
            "max_candidates": 15, "source": "serpapi",
        }
        job = repo.get_pipeline_job("job50")
        assert job["status"] == "completed"
        assert job["stats"]["candidates_found"] == 5
        assert job["stats"]["new_supplier_ids"] == [10, 11]

    def test_missing_optional_options_default_correctly(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "DiscoveryService", FakeDiscoveryService)

        repo.create_pipeline_job(job_id="job51", query="[discovery] trailer axle", options={"product": "trailer axle"})
        jobs_module.run_discovery_job("job51", {"product": "trailer axle"})

        assert FakeDiscoveryService.last_instance.last_discover_call == {
            "product": "trailer axle", "category": None, "country": None,
            "max_candidates": 20, "source": "serpapi",
        }

    def test_source_llm_is_passed_through(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "DiscoveryService", FakeDiscoveryService)

        repo.create_pipeline_job(job_id="job53", query="[discovery] jockey wheel",
                                  options={"product": "jockey wheel", "source": "llm"})
        jobs_module.run_discovery_job("job53", {"product": "jockey wheel", "source": "llm"})

        assert FakeDiscoveryService.last_instance.last_discover_call["source"] == "llm"

    def test_failing_job_marks_failed_not_raised(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "DiscoveryService", FailingFakeDiscoveryService)

        repo.create_pipeline_job(job_id="job52", query="[discovery] trailer axle", options={"product": "trailer axle"})
        jobs_module.run_discovery_job("job52", {"product": "trailer axle"})  # must not raise

        job = repo.get_pipeline_job("job52")
        assert job["status"] == "failed"
        assert "search API down" in job["error"]

    def test_progress_is_written_incrementally(self, repo, monkeypatch):
        """The new capability: unlike every other existing behaviour
        here, this job type previously had NO progress callback at all
        -- now every discover() call (not just target_count ones) wires
        one, so a live per-candidate feed works for plain product
        searches too."""
        monkeypatch.setattr(jobs_module, "DiscoveryService", FakeDiscoveryService)

        repo.create_pipeline_job(job_id="job54", query="[discovery] trailer axle", options={"product": "trailer axle"})
        jobs_module.run_discovery_job("job54", {"product": "trailer axle"})

        job = repo.get_pipeline_job("job54")
        assert job["progress"]["events"][0]["domain"] == "acmetrailer.com"
        assert job["progress"]["events"][0]["status"] == "validated"
        assert job["progress"]["examined"] == 1
        assert job["progress"]["target"] is None

    def test_target_count_routes_to_discover_to_target(self, repo, monkeypatch):
        """Regression-guards the existing no-target-count path (still
        calls discover()) while confirming target_count actually
        switches to the round-based orchestrator."""
        monkeypatch.setattr(jobs_module, "DiscoveryService", FakeDiscoveryService)

        repo.create_pipeline_job(job_id="job55", query="[discovery] forklift",
                                  options={"product": "forklift", "target_count": 10, "max_multiplier": 4})
        jobs_module.run_discovery_job("job55", {"product": "forklift", "target_count": 10, "max_multiplier": 4})

        instance = FakeDiscoveryService.last_instance
        assert instance.last_discover_call is None  # discover() was NOT called
        assert instance.last_discover_to_target_call == {
            "product": "forklift", "target_count": 10, "category": None,
            "country": None, "max_multiplier": 4,
        }
        job = repo.get_pipeline_job("job55")
        assert job["status"] == "completed"
        assert job["stats"]["new_supplier_ids"] == [20, 21]
        assert job["stats"]["reached_target"] is True

    def test_no_target_count_still_calls_plain_discover(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "DiscoveryService", FakeDiscoveryService)

        repo.create_pipeline_job(job_id="job56", query="[discovery] trailer axle", options={"product": "trailer axle"})
        jobs_module.run_discovery_job("job56", {"product": "trailer axle"})

        instance = FakeDiscoveryService.last_instance
        assert instance.last_discover_call is not None
        assert instance.last_discover_to_target_call is None


class FakeCompanyWebsiteFinder:
    last_call = None

    def __init__(self, google_scraper, own_website_scraper):
        pass

    def find_website(self, company_name, country=None):
        from scrapers.company_website_finder import WebsiteFindingResult
        FakeCompanyWebsiteFinder.last_call = {"company_name": company_name, "country": country}
        result = FakeCompanyWebsiteFinder.next_result
        return WebsiteFindingResult(
            company_name=company_name, domain=result.get("domain"), validated=result.get("validated", False),
            candidate_url=result.get("domain"), name_match_score=result.get("score"),
            reason=result.get("reason", "not configured in test fake"),
        )


class ExplodingCompanyWebsiteFinder:
    def __init__(self, *args, **kwargs):
        raise AssertionError("CompanyWebsiteFinder must not be constructed for URL-shaped input")


class FakeBatchServiceForSingleCompany:
    """Records the exact rows run_batch was called with, and simulates
    the real BatchService's DB side effect (writing a batch_upload_rows
    row with a supplier_id) closely enough for
    run_single_company_job's own resolved_supplier_id readback (via
    repo.get_batch_upload_rows) to be genuinely exercised, not just
    assumed."""

    last_instance = None

    def __init__(self, repo=None):
        self.repo = repo
        self.last_run_batch_rows = None
        FakeBatchServiceForSingleCompany.last_instance = self

    def run_batch(self, rows, batch_job_id, progress_callback=None, search_reputation=False):
        from batch.batch_service import BatchOutcome

        self.last_run_batch_rows = rows
        row = rows[0]
        supplier_id = self.repo.create_golden_record({
            "canonical_name": row.company_name or "Acmetrailer", "domain": row.website,
        })
        row_id = self.repo.create_batch_upload_row(
            batch_job_id=batch_job_id, row_index=row.row_index,
            original_columns=row.original_columns, company_name=row.company_name, website=row.website,
        )
        self.repo.update_batch_upload_row(row_id, {"status": "success", "supplier_id": supplier_id})
        self.last_resolved_supplier_id = supplier_id
        outcome = BatchOutcome(total_rows=1, processed=1, succeeded=1)
        if progress_callback:
            progress_callback(outcome)
        return outcome


class TestRunSingleCompanyJob:

    def test_url_shaped_input_skips_the_website_finder_entirely(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "BatchService", FakeBatchServiceForSingleCompany)
        import scrapers.company_website_finder as finder_module
        monkeypatch.setattr(finder_module, "CompanyWebsiteFinder", ExplodingCompanyWebsiteFinder)

        repo.create_pipeline_job(job_id="job60", query="[single-company] acmetrailer.com",
                                  options={"input_text": "acmetrailer.com"})
        jobs_module.run_single_company_job("job60", {"input_text": "acmetrailer.com"})  # must not raise

        job = repo.get_pipeline_job("job60")
        assert job["status"] == "completed"
        rows = FakeBatchServiceForSingleCompany.last_instance.last_run_batch_rows
        assert rows[0].company_name is None
        assert rows[0].website == "acmetrailer.com"

    def test_bare_name_input_calls_find_website_with_company_name_and_country(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "BatchService", FakeBatchServiceForSingleCompany)
        import scrapers.company_website_finder as finder_module
        monkeypatch.setattr(finder_module, "CompanyWebsiteFinder", FakeCompanyWebsiteFinder)
        FakeCompanyWebsiteFinder.next_result = {
            "domain": "acmetrailer.com", "validated": True, "score": 92.0, "reason": "matched",
        }

        repo.create_pipeline_job(job_id="job61", query="[single-company] Acme Trailer Co",
                                  options={"input_text": "Acme Trailer Co", "country": "China"})
        jobs_module.run_single_company_job("job61", {"input_text": "Acme Trailer Co", "country": "China"})

        assert FakeCompanyWebsiteFinder.last_call == {"company_name": "Acme Trailer Co", "country": "China"}

    def test_unvalidated_website_finder_result_short_circuits_to_needs_url(self, repo, monkeypatch):
        FakeBatchServiceForSingleCompany.last_instance = None  # reset -- class attribute persists across tests
        monkeypatch.setattr(jobs_module, "BatchService", FakeBatchServiceForSingleCompany)
        import scrapers.company_website_finder as finder_module
        monkeypatch.setattr(finder_module, "CompanyWebsiteFinder", FakeCompanyWebsiteFinder)
        FakeCompanyWebsiteFinder.next_result = {
            "validated": False, "reason": "no non-platform, non-directory result found",
        }

        repo.create_pipeline_job(job_id="job62", query="[single-company] Nonexistent Widgets Ltd",
                                  options={"input_text": "Nonexistent Widgets Ltd"})
        jobs_module.run_single_company_job("job62", {"input_text": "Nonexistent Widgets Ltd"})

        job = repo.get_pipeline_job("job62")
        assert job["status"] == "completed"
        assert job["stats"]["status"] == "needs_url"
        assert job["stats"]["website_finder_reason"] == "no non-platform, non-directory result found"
        assert FakeBatchServiceForSingleCompany.last_instance is None  # run_batch was never reached

    def test_validated_result_resolves_a_real_supplier_id(self, repo, monkeypatch):
        monkeypatch.setattr(jobs_module, "BatchService", FakeBatchServiceForSingleCompany)
        import scrapers.company_website_finder as finder_module
        monkeypatch.setattr(finder_module, "CompanyWebsiteFinder", FakeCompanyWebsiteFinder)
        FakeCompanyWebsiteFinder.next_result = {
            "domain": "acmetrailer.com", "validated": True, "score": 92.0, "reason": "matched",
        }

        repo.create_pipeline_job(job_id="job63", query="[single-company] Acme Trailer Co",
                                  options={"input_text": "Acme Trailer Co"})
        jobs_module.run_single_company_job("job63", {"input_text": "Acme Trailer Co"})

        job = repo.get_pipeline_job("job63")
        assert job["status"] == "completed"
        assert job["stats"]["resolved_domain"] == "acmetrailer.com"
        assert job["stats"]["resolved_supplier_id"] == FakeBatchServiceForSingleCompany.last_instance.last_resolved_supplier_id
        assert job["stats"]["succeeded"] == 1
