"""
tests/test_search_and_product_matching.py

Tests for:
  1. The restructured trailer-component taxonomy (assembly vs component)
  2. scrapers.google_search_scraper.GoogleSearchScraper (mocked SerpAPI)
  3. normalizers.google_search_normalizer.GoogleSearchNormalizer
  4. verification.product_matcher.ProductMatcher (mocked OpenAI client)
"""

from __future__ import annotations

import httpx
import pytest

from config.settings import TRAILER_COMPONENT_TAXONOMY, get_search_terms
from scrapers.google_search_scraper import GoogleSearchScraper
from normalizers.google_search_normalizer import GoogleSearchNormalizer
from verification.product_matcher import ProductMatcher
from storage.database import initialise_schema
from storage.repository import SupplierRepository
from pipeline.orchestrator import SupplierIntelligencePipeline


class TestTaxonomy:

    def test_assembly_terms_exclude_bulbs(self):
        assembly_terms = get_search_terms(level="assembly")
        assert not any("bulb" in t.lower() for t in assembly_terms)

    def test_component_terms_include_bulbs(self):
        component_terms = get_search_terms(level="component")
        assert any("bulb" in t.lower() for t in component_terms)

    def test_combined_level_and_category_filter(self):
        terms = get_search_terms(level="assembly", category="LED Lighting")
        assert "trailer rear combination lamp assembly" in terms
        assert not any("bulb" in t.lower() for t in terms)

    def test_no_filter_returns_everything(self):
        all_terms = get_search_terms()
        total_expected = sum(len(e["search_terms"]) for e in TRAILER_COMPONENT_TAXONOMY.values())
        assert len(all_terms) == total_expected

    def test_taxonomy_entries_well_formed(self):
        for key, entry in TRAILER_COMPONENT_TAXONOMY.items():
            assert entry["level"] in ("assembly", "component")
            assert entry["category"]
            assert len(entry["search_terms"]) > 0


class FakeSerpResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class FakeSerpClient:
    def __init__(self, json_data):
        self._json_data = json_data
        self.last_params = None

    def get(self, url, params=None):
        self.last_params = params
        return FakeSerpResponse(self._json_data)


WEB_SEARCH_RESPONSE = {
    "organic_results": [
        {
            "title": "Istanbul Axle Manufacturing Co | Home",
            "link": "https://istanbulaxle.com.tr",
            "snippet": "Leading manufacturer of trailer axles in Turkey.",
            "displayed_link": "istanbulaxle.com.tr",
        },
        {
            "title": "Trailer Parts - Alibaba.com",
            "link": "https://alibaba.com/some-listing",
            "snippet": "Wholesale trailer parts.",
        },
    ]
}

REVERSE_IMAGE_RESPONSE = {
    "image_results": [
        {"title": "Rear combination lamp - Foo Co", "link": "https://foo.com/product/1"},
        {"title": "Similar lamp - Bar Co", "link": "https://bar.com/product/2"},
    ]
}


class TestGoogleSearchScraper:

    def test_scrape_returns_error_without_api_key(self):
        scraper = GoogleSearchScraper(api_key=None, http_client=FakeSerpClient({}), enable_delays=False)
        results = scraper.scrape("trailer axle manufacturer")
        assert len(results) == 1
        assert results[0].success is False
        assert "SERPAPI_KEY" in results[0].error

    def test_scrape_parses_organic_results(self):
        client = FakeSerpClient(WEB_SEARCH_RESPONSE)
        scraper = GoogleSearchScraper(api_key="fake-key", http_client=client, enable_delays=False)

        results = scraper.scrape("trailer axle manufacturer")
        assert len(results) == 2
        assert results[0].raw_data["title"] == "Istanbul Axle Manufacturing Co | Home"
        assert results[0].raw_data["link"] == "https://istanbulaxle.com.tr"

    def test_scrape_applies_site_filter(self):
        client = FakeSerpClient(WEB_SEARCH_RESPONSE)
        scraper = GoogleSearchScraper(api_key="fake-key", http_client=client, enable_delays=False)

        scraper.scrape("trailer axle", site_filter="alibaba.com")
        assert client.last_params["q"] == "site:alibaba.com trailer axle"

    def test_scrape_handles_transport_error(self):
        class FailingClient:
            def get(self, url, params=None):
                raise httpx.ConnectError("connection refused")

        scraper = GoogleSearchScraper(api_key="fake-key", http_client=FailingClient(), enable_delays=False)
        results = scraper.scrape("trailer axle")
        assert results[0].success is False

    def test_search_by_image_url_parses_results(self):
        client = FakeSerpClient(REVERSE_IMAGE_RESPONSE)
        scraper = GoogleSearchScraper(api_key="fake-key", http_client=client, enable_delays=False)

        results = scraper.search_by_image_url("https://example.com/reference-photo.jpg")
        assert len(results) == 2
        assert results[0].source == "google_reverse_image"
        assert results[0].raw_data["source_image_url"] == "https://example.com/reference-photo.jpg"

    def test_search_by_image_url_requires_api_key(self):
        scraper = GoogleSearchScraper(api_key=None, http_client=FakeSerpClient({}), enable_delays=False)
        results = scraper.search_by_image_url("https://example.com/photo.jpg")
        assert results[0].success is False


class TestGoogleSearchNormalizer:

    def test_normalise_strips_title_boilerplate(self):
        normalizer = GoogleSearchNormalizer()
        result = normalizer.normalise({
            "title": "Istanbul Axle Manufacturing Co | Home",
            "link": "https://istanbulaxle.com.tr",
            "snippet": "Leading manufacturer of trailer axles.",
        })
        assert result["canonical_name"] == "Istanbul Axle Manufacturing Co"
        assert result["domain"] == "istanbulaxle.com.tr"
        assert result["moq_notes"] == "Leading manufacturer of trailer axles."

    def test_normalise_platform_domain_not_treated_as_own_website(self):
        normalizer = GoogleSearchNormalizer()
        result = normalizer.normalise({
            "title": "Trailer Parts - Alibaba.com",
            "link": "https://foo.en.alibaba.com/product",
        })
        assert "domain" not in result

    def test_normalise_missing_title(self):
        normalizer = GoogleSearchNormalizer()
        result = normalizer.normalise({"link": "https://foo.com"})
        assert result["canonical_name"] == ""

    def test_normalise_output_compatible_with_repository(self, tmp_path):
        db_path = tmp_path / "test.db"
        initialise_schema(db_path)
        repo = SupplierRepository(db_path=db_path)
        normalizer = GoogleSearchNormalizer()

        supplier_data = normalizer.normalise({
            "title": "Istanbul Axle Co - Manufacturer in Turkey",
            "link": "https://istanbulaxle.com.tr",
        })
        supplier_id = repo.create_golden_record(supplier_data)
        supplier = repo.get_supplier(supplier_id)
        assert supplier["canonical_name"] == "Istanbul Axle Co"


class FakeMessage:
    def __init__(self, text):
        self.content = text


class FakeChoice:
    def __init__(self, text):
        self.message = FakeMessage(text)


class FakeCompletion:
    def __init__(self, text):
        self.choices = [FakeChoice(text)]


class FakeChatCompletionsAPI:
    def __init__(self, response_text=None, raise_error=None):
        self._response_text = response_text
        self._raise_error = raise_error
        self.last_call_kwargs = None

    def create(self, **kwargs):
        self.last_call_kwargs = kwargs
        if self._raise_error:
            raise self._raise_error
        return FakeCompletion(self._response_text)


class FakeChatAPI:
    def __init__(self, response_text=None, raise_error=None):
        self.completions = FakeChatCompletionsAPI(response_text=response_text, raise_error=raise_error)


class FakeOpenAIClient:
    def __init__(self, response_text=None, raise_error=None):
        self.chat = FakeChatAPI(response_text=response_text, raise_error=raise_error)


class TestProductMatcherClassify:

    def test_classify_match(self):
        client = FakeOpenAIClient(response_text="VERDICT: match\nREASONING: Complete lamp housing with connector visible.")
        matcher = ProductMatcher(client=client)

        result = matcher.classify_product(
            b"fakebytes", "image/jpeg",
            "complete trailer rear combination lamp assembly with housing, not a bare bulb",
        )
        assert result["verdict"] == "match"

    def test_classify_no_match_bulb_vs_assembly(self):
        client = FakeOpenAIClient(
            response_text="VERDICT: no_match\nREASONING: This shows a bare LED bulb, not a complete lamp assembly with housing."
        )
        matcher = ProductMatcher(client=client)

        result = matcher.classify_product(
            b"fakebytes", "image/jpeg",
            "complete trailer rear combination lamp assembly with housing",
        )
        assert result["verdict"] == "no_match"
        assert "bulb" in result["reasoning"].lower()

    def test_classify_handles_api_error(self):
        client = FakeOpenAIClient(raise_error=RuntimeError("API unavailable"))
        matcher = ProductMatcher(client=client)
        result = matcher.classify_product(b"x", "image/jpeg", "anything")
        assert result["verdict"] == "uncertain"
        assert "API unavailable" in result["reasoning"]

    def test_classify_handles_malformed_response(self):
        client = FakeOpenAIClient(response_text="not sure what this is")
        matcher = ProductMatcher(client=client)
        result = matcher.classify_product(b"x", "image/jpeg", "anything")
        assert result["verdict"] == "uncertain"


class TestProductMatcherCompareToReferences:

    def test_compare_match(self):
        client = FakeOpenAIClient(response_text="VERDICT: match\nREASONING: Same complete assembly type.")
        matcher = ProductMatcher(client=client)

        references = [{"image_bytes": b"ref1", "media_type": "image/jpeg"}]
        candidate = {"image_bytes": b"cand", "media_type": "image/jpeg"}

        result = matcher.compare_to_references(references, candidate, product_context="trailer tail light assembly")
        assert result["verdict"] == "match"
        assert result["reference_count"] == 1

    def test_compare_supports_multiple_reference_photos(self):
        client = FakeOpenAIClient(response_text="VERDICT: match\nREASONING: Consistent across all angles shown.")
        matcher = ProductMatcher(client=client)

        references = [
            {"image_bytes": b"ref1", "media_type": "image/jpeg"},
            {"image_bytes": b"ref2", "media_type": "image/jpeg"},
            {"image_bytes": b"ref3", "media_type": "image/png"},
        ]
        candidate = {"image_bytes": b"cand", "media_type": "image/jpeg"}

        result = matcher.compare_to_references(references, candidate)
        assert result["reference_count"] == 3

        content = client.chat.completions.last_call_kwargs["messages"][0]["content"]
        image_items = [c for c in content if c["type"] == "image_url"]
        assert len(image_items) == 4

    def test_compare_empty_references_returns_uncertain(self):
        matcher = ProductMatcher(client=FakeOpenAIClient())
        result = matcher.compare_to_references([], {"image_bytes": b"x", "media_type": "image/jpeg"})
        assert result["verdict"] == "uncertain"
        assert result["reference_count"] == 0

    def test_compare_no_match_component_vs_assembly(self):
        client = FakeOpenAIClient(
            response_text="VERDICT: no_match\nREASONING: References show complete housings; candidate shows only a bare bulb."
        )
        matcher = ProductMatcher(client=client)

        references = [{"image_bytes": b"ref", "media_type": "image/jpeg"}]
        candidate = {"image_bytes": b"cand", "media_type": "image/jpeg"}

        result = matcher.compare_to_references(references, candidate, product_context="trailer rear combination lamp")
        assert result["verdict"] == "no_match"


@pytest.fixture()
def repo(tmp_path):
    db_path = tmp_path / "test.db"
    initialise_schema(db_path)
    return SupplierRepository(db_path=db_path)


class TestPipelineGoogleIntegration:

    def test_google_registered_as_default_source(self, repo):
        pipeline = SupplierIntelligencePipeline(repo=repo)
        assert "google" in pipeline.scrapers
        assert "google" in pipeline.normalizers

    def test_google_source_creates_supplier(self, repo):
        client = FakeSerpClient(WEB_SEARCH_RESPONSE)
        scrapers = {"google": GoogleSearchScraper(api_key="fake-key", http_client=client, enable_delays=False)}
        normalizers = {"google": GoogleSearchNormalizer()}

        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers=scrapers, normalizers=normalizers)
        stats = pipeline.run("trailer axle manufacturer", sources=["google"])

        assert stats["scraped"] == 2
        suppliers = repo.list_suppliers(limit=10)
        names = {s["canonical_name"] for s in suppliers}
        assert "Istanbul Axle Manufacturing Co" in names
