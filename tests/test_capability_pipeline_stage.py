"""
tests/test_capability_pipeline_stage.py

Tests for SupplierIntelligencePipeline's new, opt-in
capability-extraction stage. Uses fake own-website scraper and
extractor objects (no network, no OpenAI call) — mirrors
tests/test_pipeline.py's own fake-scraper convention.
"""

from __future__ import annotations

import pytest

from pipeline.orchestrator import SupplierIntelligencePipeline
from storage.database import initialise_schema
from storage.repository import SupplierRepository
from scrapers.own_website_scraper import OwnWebsiteFetchResult, OwnWebsitePage
from verification.capability_extractor import CapabilityFinding


class FakeOwnWebsiteScraper:
    def __init__(self, result_by_domain=None, default_result=None):
        self._by_domain = result_by_domain or {}
        self._default = default_result
        self.fetched_domains = []

    def fetch(self, domain):
        self.fetched_domains.append(domain)
        if domain in self._by_domain:
            return self._by_domain[domain]
        if self._default is not None:
            return self._default
        return OwnWebsiteFetchResult(domain=domain, pages=[OwnWebsitePage(url=domain, text="some text")])


class FakeCapabilityExtractor:
    def __init__(self, findings=None):
        self._findings = findings if findings is not None else []
        self.extract_calls = 0

    def extract_from_pages(self, pages):
        self.extract_calls += 1
        return list(self._findings)


@pytest.fixture()
def repo(tmp_path):
    db_path = tmp_path / "test.db"
    initialise_schema(db_path)
    return SupplierRepository(db_path=db_path)


def _finding(**overrides):
    base = dict(
        reported_term="rotomoulding", canonical_term="rotational moulding", category="process",
        relationship="in_house", confidence=0.9, evidence="we operate...", source_url="https://acme.example.com",
    )
    base.update(overrides)
    return CapabilityFinding(**base)


class TestCapabilityExtractionStage:

    def test_off_by_default_in_run(self, repo):
        """run_capability_extraction defaults to False — this stage
        must never fire unless explicitly requested, given its
        different cost/traffic profile from the rest of a default run."""
        repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        own_site = FakeOwnWebsiteScraper()
        pipeline = SupplierIntelligencePipeline(
            repo=repo, scrapers={}, normalizers={},
            own_website_scraper=own_site, capability_extractor=FakeCapabilityExtractor(),
        )
        stats = pipeline.run("wheel bearings", sources=[])
        assert stats["capability_extracted"] == 0
        assert own_site.fetched_domains == []

    def test_opting_in_runs_the_stage(self, repo):
        repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        own_site = FakeOwnWebsiteScraper()
        extractor = FakeCapabilityExtractor(findings=[_finding()])
        pipeline = SupplierIntelligencePipeline(
            repo=repo, scrapers={}, normalizers={},
            own_website_scraper=own_site, capability_extractor=extractor,
        )
        stats = pipeline.run("wheel bearings", sources=[], run_capability_extraction=True)

        assert stats["capability_extracted"] == 1
        assert own_site.fetched_domains == ["acme.example.com"]

    def test_run_capability_extraction_only_runs_independent_of_run(self, repo):
        repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        pipeline = SupplierIntelligencePipeline(
            repo=repo, scrapers={}, normalizers={},
            own_website_scraper=FakeOwnWebsiteScraper(),
            capability_extractor=FakeCapabilityExtractor(findings=[_finding()]),
        )
        stats = pipeline.run_capability_extraction_only()
        assert stats["capability_extracted"] == 1

    def test_supplier_without_domain_is_skipped(self, repo):
        repo.create_golden_record({"canonical_name": "No Domain Co", "domain": None})
        own_site = FakeOwnWebsiteScraper()
        pipeline = SupplierIntelligencePipeline(
            repo=repo, scrapers={}, normalizers={},
            own_website_scraper=own_site, capability_extractor=FakeCapabilityExtractor(),
        )
        pipeline.run_capability_extraction_only()
        assert own_site.fetched_domains == []

    def test_already_attempted_supplier_is_not_re_fetched(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        repo.mark_capability_extraction_attempted(supplier_id)
        own_site = FakeOwnWebsiteScraper()
        pipeline = SupplierIntelligencePipeline(
            repo=repo, scrapers={}, normalizers={},
            own_website_scraper=own_site, capability_extractor=FakeCapabilityExtractor(),
        )
        pipeline.run_capability_extraction_only()
        assert own_site.fetched_domains == []

    def test_force_re_attempts_already_processed_suppliers(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        repo.mark_capability_extraction_attempted(supplier_id)
        own_site = FakeOwnWebsiteScraper()
        pipeline = SupplierIntelligencePipeline(
            repo=repo, scrapers={}, normalizers={},
            own_website_scraper=own_site, capability_extractor=FakeCapabilityExtractor(),
        )
        pipeline.run_capability_extraction_only(force=True)
        assert own_site.fetched_domains == ["acme.example.com"]

    def test_fetch_failure_still_marks_attempted_and_does_not_crash_the_stage(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "dead.example.com"})
        own_site = FakeOwnWebsiteScraper(
            default_result=OwnWebsiteFetchResult(domain="dead.example.com", success=False, error="timeout")
        )
        pipeline = SupplierIntelligencePipeline(
            repo=repo, scrapers={}, normalizers={},
            own_website_scraper=own_site, capability_extractor=FakeCapabilityExtractor(),
        )
        stats = pipeline.run_capability_extraction_only()
        assert stats["capability_extracted"] == 0
        supplier = repo.get_supplier(supplier_id)
        assert supplier["capability_extracted_at"] is not None  # attempted, not stuck in retry loop

    def test_one_supplier_raising_does_not_abort_the_batch(self, repo):
        repo.create_golden_record({"canonical_name": "Broken Co", "domain": "broken.example.com"})
        repo.create_golden_record({"canonical_name": "Good Co", "domain": "good.example.com"})

        class ExplodingOwnSite:
            def fetch(self, domain):
                if domain == "broken.example.com":
                    raise RuntimeError("network exploded")
                return OwnWebsiteFetchResult(domain=domain, pages=[OwnWebsitePage(url=domain, text="hi")])

        pipeline = SupplierIntelligencePipeline(
            repo=repo, scrapers={}, normalizers={},
            own_website_scraper=ExplodingOwnSite(),
            capability_extractor=FakeCapabilityExtractor(findings=[_finding()]),
        )
        stats = pipeline.run_capability_extraction_only()
        assert stats["capability_extracted"] == 1  # good.example.com's finding still recorded

    def test_duplicate_findings_across_runs_do_not_inflate_the_stat(self, repo):
        repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        pipeline = SupplierIntelligencePipeline(
            repo=repo, scrapers={}, normalizers={},
            own_website_scraper=FakeOwnWebsiteScraper(),
            capability_extractor=FakeCapabilityExtractor(findings=[_finding()]),
        )
        first = pipeline.run_capability_extraction_only()
        second = pipeline.run_capability_extraction_only(force=True)
        assert first["capability_extracted"] == 1
        assert second["capability_extracted"] == 0  # same finding already on file


class TestContactExtractionReusesTheSameFetchedPages:
    """Contact details come from the exact same page fetch capability
    extraction already paid for -- no second HTTP round trip, no LLM
    call. These tests use pages with real extractable contact text
    (the fake own-website scraper's default page text has none)."""

    def _pipeline_with_contact_bearing_page(self, repo, *, page_text, domain="acme.example.com"):
        own_site = FakeOwnWebsiteScraper(
            default_result=OwnWebsiteFetchResult(
                domain=domain, pages=[OwnWebsitePage(url=domain, text=page_text)]
            )
        )
        return SupplierIntelligencePipeline(
            repo=repo, scrapers={}, normalizers={},
            own_website_scraper=own_site, capability_extractor=FakeCapabilityExtractor(),
        )

    def test_email_found_on_page_is_recorded_as_primary(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        pipeline = self._pipeline_with_contact_bearing_page(
            repo, page_text="Contact us: sales@acme.example.com"
        )
        stats = pipeline.run_capability_extraction_only()
        assert stats["contact_emails_added"] == 1
        assert repo.get_supplier(supplier_id)["primary_email"] == "sales@acme.example.com"

    def test_phone_found_on_page_with_country_hint_is_recorded(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme", "domain": "acme.example.com", "country": "China",
        })
        pipeline = self._pipeline_with_contact_bearing_page(
            repo, page_text="Tel: 0574-8765 4321"
        )
        stats = pipeline.run_capability_extraction_only()
        assert stats["contact_phones_added"] == 1
        assert repo.get_supplier(supplier_id)["primary_phone"].startswith("+86")

    def test_page_with_no_contact_details_leaves_stats_at_zero(self, repo):
        repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        pipeline = self._pipeline_with_contact_bearing_page(
            repo, page_text="We are a leading manufacturer of trailer components since 1995."
        )
        stats = pipeline.run_capability_extraction_only()
        assert stats["contact_emails_added"] == 0
        assert stats["contact_phones_added"] == 0

    def test_existing_contact_details_are_never_overwritten_by_extraction(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme", "domain": "acme.example.com", "primary_email": "already@acme.com",
        })
        pipeline = self._pipeline_with_contact_bearing_page(
            repo, page_text="Contact us: different@acme.example.com"
        )
        pipeline.run_capability_extraction_only()
        assert repo.get_supplier(supplier_id)["primary_email"] == "already@acme.com"

    def test_contact_form_url_is_recorded_when_no_email_exists_anywhere(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        own_site = FakeOwnWebsiteScraper(
            default_result=OwnWebsiteFetchResult(
                domain="acme.example.com",
                pages=[OwnWebsitePage(
                    url="acme.example.com/contact", text="Get in touch with us below.",
                    has_contact_form=True,
                )],
            )
        )
        pipeline = SupplierIntelligencePipeline(
            repo=repo, scrapers={}, normalizers={},
            own_website_scraper=own_site, capability_extractor=FakeCapabilityExtractor(),
        )
        stats = pipeline.run_capability_extraction_only()

        assert stats["contact_forms_recorded"] == 1
        supplier = repo.get_supplier(supplier_id)
        assert supplier["contact_form_url"] == "acme.example.com/contact"
        assert supplier["primary_email"] is None

    def test_contact_form_is_not_recorded_when_an_email_was_also_found(self, repo):
        """Email must always win over the form fallback, even within
        the same page fetch."""
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        own_site = FakeOwnWebsiteScraper(
            default_result=OwnWebsiteFetchResult(
                domain="acme.example.com",
                pages=[OwnWebsitePage(
                    url="acme.example.com/contact", text="Email us: sales@acme.example.com",
                    has_contact_form=True,
                )],
            )
        )
        pipeline = SupplierIntelligencePipeline(
            repo=repo, scrapers={}, normalizers={},
            own_website_scraper=own_site, capability_extractor=FakeCapabilityExtractor(),
        )
        stats = pipeline.run_capability_extraction_only()

        assert stats["contact_forms_recorded"] == 0
        supplier = repo.get_supplier(supplier_id)
        assert supplier["primary_email"] == "sales@acme.example.com"
        assert supplier["contact_form_url"] is None


class FakeWebsiteFinder:
    def __init__(self, result_by_name=None, default_result=None):
        self._by_name = result_by_name or {}
        self._default = default_result
        self.searched_names = []

    def find_website(self, company_name, country=None):
        self.searched_names.append(company_name)
        if company_name in self._by_name:
            return self._by_name[company_name]
        return self._default


class TestWebsiteDiscoveryStage:

    def test_off_by_default_in_run(self, repo):
        repo.create_golden_record({"canonical_name": "No Domain Co", "domain": None})
        finder = FakeWebsiteFinder()
        pipeline = SupplierIntelligencePipeline(
            repo=repo, scrapers={}, normalizers={}, website_finder=finder,
        )
        stats = pipeline.run("wheel bearings", sources=[])
        assert stats["website_discovered"] == 0
        assert finder.searched_names == []

    def test_validated_result_sets_the_domain_and_increments_stat(self, repo):
        from scrapers.company_website_finder import WebsiteFindingResult

        supplier_id = repo.create_golden_record({"canonical_name": "Acme Trailer Parts", "domain": None})
        finder = FakeWebsiteFinder(default_result=WebsiteFindingResult(
            company_name="Acme Trailer Parts", domain="acme-trailer.com", validated=True,
            candidate_url="https://acme-trailer.com/", name_match_score=90.0, reason="matched",
        ))
        pipeline = SupplierIntelligencePipeline(
            repo=repo, scrapers={}, normalizers={}, website_finder=finder,
        )
        stats = pipeline.run_website_discovery_only()

        assert stats["website_discovered"] == 1
        assert repo.get_supplier(supplier_id)["domain"] == "acme-trailer.com"

    def test_unvalidated_result_does_not_set_a_domain(self, repo):
        from scrapers.company_website_finder import WebsiteFindingResult

        supplier_id = repo.create_golden_record({"canonical_name": "Acme Trailer Parts", "domain": None})
        finder = FakeWebsiteFinder(default_result=WebsiteFindingResult(
            company_name="Acme Trailer Parts", domain=None, validated=False,
            candidate_url="https://maybe.example.com/", name_match_score=20.0, reason="too weak",
        ))
        pipeline = SupplierIntelligencePipeline(
            repo=repo, scrapers={}, normalizers={}, website_finder=finder,
        )
        stats = pipeline.run_website_discovery_only()

        assert stats["website_discovered"] == 0
        assert repo.get_supplier(supplier_id)["domain"] is None

    def test_supplier_still_marked_attempted_even_when_not_validated(self, repo):
        from scrapers.company_website_finder import WebsiteFindingResult

        supplier_id = repo.create_golden_record({"canonical_name": "Acme Trailer Parts", "domain": None})
        finder = FakeWebsiteFinder(default_result=WebsiteFindingResult(
            company_name="Acme Trailer Parts", domain=None, validated=False,
            candidate_url=None, name_match_score=None, reason="nothing found",
        ))
        pipeline = SupplierIntelligencePipeline(
            repo=repo, scrapers={}, normalizers={}, website_finder=finder,
        )
        pipeline.run_website_discovery_only()
        assert repo.get_supplier(supplier_id)["website_search_attempted_at"] is not None

    def test_one_supplier_raising_does_not_abort_the_batch(self, repo):
        from scrapers.company_website_finder import WebsiteFindingResult

        repo.create_golden_record({"canonical_name": "Broken Co", "domain": None})
        repo.create_golden_record({"canonical_name": "Good Co", "domain": None})

        class ExplodingFinder:
            def find_website(self, company_name, country=None):
                if company_name == "Broken Co":
                    raise RuntimeError("SerpAPI exploded")
                return WebsiteFindingResult(
                    company_name=company_name, domain="good.example.com", validated=True,
                    candidate_url="https://good.example.com/", name_match_score=95.0, reason="matched",
                )

        pipeline = SupplierIntelligencePipeline(
            repo=repo, scrapers={}, normalizers={}, website_finder=ExplodingFinder(),
        )
        stats = pipeline.run_website_discovery_only()
        assert stats["website_discovered"] == 1  # Good Co still processed

    def test_found_domain_is_immediately_eligible_for_capability_extraction_in_the_same_run(self, repo):
        """The whole point of running website discovery before
        capability extraction within one run() call."""
        from scrapers.company_website_finder import WebsiteFindingResult

        repo.create_golden_record({"canonical_name": "Acme Trailer Parts", "domain": None})
        finder = FakeWebsiteFinder(default_result=WebsiteFindingResult(
            company_name="Acme Trailer Parts", domain="acme-trailer.com", validated=True,
            candidate_url="https://acme-trailer.com/", name_match_score=90.0, reason="matched",
        ))
        own_site = FakeOwnWebsiteScraper()
        capability_extractor = FakeCapabilityExtractor(findings=[_finding()])
        pipeline = SupplierIntelligencePipeline(
            repo=repo, scrapers={}, normalizers={}, website_finder=finder,
            own_website_scraper=own_site, capability_extractor=capability_extractor,
        )

        stats = pipeline.run(
            "wheel bearings", sources=[],
            run_website_discovery=True, run_capability_extraction=True,
        )

        assert stats["website_discovered"] == 1
        assert stats["capability_extracted"] == 1
        assert own_site.fetched_domains == ["acme-trailer.com"]


class TestPipelineConstructorDoesNotAssumeGoogleScraperPresent:
    """Regression test for a real bug caught during review: the default
    website_finder construction originally did self.scrapers["google"],
    which raised KeyError for any caller passing a custom scrapers dict
    without a "google" key -- exactly what most of this codebase's own
    existing tests do."""

    def test_empty_scrapers_dict_does_not_raise(self, repo):
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers={}, normalizers={})
        assert pipeline.website_finder is not None

    def test_custom_scrapers_dict_without_google_does_not_raise(self, repo):
        pipeline = SupplierIntelligencePipeline(
            repo=repo, scrapers={"alibaba": None}, normalizers={},
        )
        assert pipeline.website_finder is not None


class FakePhotoDownloader:
    def __init__(self, success=True):
        self.success = success
        self.downloaded_urls = []

    def download_all(self, urls):
        self.downloaded_urls.extend(urls)
        from scrapers.photo_downloader import DownloadedPhoto
        return [
            DownloadedPhoto(url=u, image_bytes=b"fake" if self.success else None,
                             media_type="image/jpeg" if self.success else None, success=self.success)
            for u in urls
        ]


class FakeFactoryPhotoVerifier:
    def __init__(self, verdict="plausible_factory"):
        self.verdict = verdict
        self.assess_calls = 0

    def assess_photos(self, photos, product_category="", company_name=""):
        self.assess_calls += 1
        return {"verdict": self.verdict, "reasoning": "fake assessment", "photo_count": len(photos)}


class TestPhotoVerificationReusesTheSameFetchedPages:

    def test_photos_from_the_fetch_are_downloaded_and_assessed(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        own_site = FakeOwnWebsiteScraper(default_result=OwnWebsiteFetchResult(
            domain="acme.example.com",
            pages=[OwnWebsitePage(
                url="acme.example.com", text="hi",
                image_urls=["https://acme.example.com/factory.jpg"],
            )],
        ))
        downloader = FakePhotoDownloader()
        verifier = FakeFactoryPhotoVerifier(verdict="plausible_factory")
        pipeline = SupplierIntelligencePipeline(
            repo=repo, scrapers={}, normalizers={},
            own_website_scraper=own_site, capability_extractor=FakeCapabilityExtractor(),
            photo_downloader=downloader, factory_photo_verifier=verifier,
        )

        stats = pipeline.run_capability_extraction_only()

        assert stats["photos_assessed"] == 1
        assert downloader.downloaded_urls == ["https://acme.example.com/factory.jpg"]
        assert verifier.assess_calls == 1
        supplier = repo.get_supplier(supplier_id)
        assert supplier["factory_photo_verdict"] == "plausible_factory"
        assert supplier["factory_photo_urls"] == ["https://acme.example.com/factory.jpg"]
        assert supplier["factory_photo_assessed_at"] is not None

    def test_no_images_on_the_page_means_no_assessment_call(self, repo):
        repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        verifier = FakeFactoryPhotoVerifier()
        pipeline = SupplierIntelligencePipeline(
            repo=repo, scrapers={}, normalizers={},
            own_website_scraper=FakeOwnWebsiteScraper(), capability_extractor=FakeCapabilityExtractor(),
            factory_photo_verifier=verifier,
        )
        stats = pipeline.run_capability_extraction_only()
        assert stats["photos_assessed"] == 0
        assert verifier.assess_calls == 0

    def test_failed_downloads_are_excluded_from_assessment(self, repo):
        repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        own_site = FakeOwnWebsiteScraper(default_result=OwnWebsiteFetchResult(
            domain="acme.example.com",
            pages=[OwnWebsitePage(
                url="acme.example.com", text="hi", image_urls=["https://acme.example.com/broken.jpg"],
            )],
        ))
        downloader = FakePhotoDownloader(success=False)
        verifier = FakeFactoryPhotoVerifier()
        pipeline = SupplierIntelligencePipeline(
            repo=repo, scrapers={}, normalizers={},
            own_website_scraper=own_site, capability_extractor=FakeCapabilityExtractor(),
            photo_downloader=downloader, factory_photo_verifier=verifier,
        )
        stats = pipeline.run_capability_extraction_only()
        assert stats["photos_assessed"] == 0
        assert verifier.assess_calls == 0

    def test_duplicate_image_urls_across_pages_are_deduplicated(self, repo):
        repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        own_site = FakeOwnWebsiteScraper(default_result=OwnWebsiteFetchResult(
            domain="acme.example.com",
            pages=[
                OwnWebsitePage(url="acme.example.com", text="hi", image_urls=["https://a.example.com/1.jpg"]),
                OwnWebsitePage(url="acme.example.com/about", text="hi", image_urls=["https://a.example.com/1.jpg"]),
            ],
        ))
        downloader = FakePhotoDownloader()
        pipeline = SupplierIntelligencePipeline(
            repo=repo, scrapers={}, normalizers={},
            own_website_scraper=own_site, capability_extractor=FakeCapabilityExtractor(),
            photo_downloader=downloader, factory_photo_verifier=FakeFactoryPhotoVerifier(),
        )
        pipeline.run_capability_extraction_only()
        assert downloader.downloaded_urls == ["https://a.example.com/1.jpg"]
