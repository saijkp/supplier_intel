"""
tests/test_geographic_expansion.py

Tests for the geographic-expansion build: scrapers.global_directory_scraper
(Turkey/Vietnam/Eastern Europe, mocked HTTP) and its normalizer.
"""

from __future__ import annotations

import httpx
import pytest

from scrapers.global_directory_scraper import GlobalDirectoryScraper, DIRECTORY_SOURCES
from normalizers.global_directory_normalizer import GlobalDirectoryNormalizer
from storage.database import initialise_schema
from storage.repository import SupplierRepository
from pipeline.orchestrator import SupplierIntelligencePipeline


def _tim_card_html(name, country="", products=("brake caliper",)):
    country_html = f'<div class="member-country">{country}</div>' if country else ""
    product_tags = "".join(f'<span class="sector-tag">{p}</span>' for p in products)
    return f"""
    <div class="member-card">
        <h3 class="member-name">{name}</h3>
        {country_html}
        {product_tags}
        <a class="member-website" href="https://{name.lower().replace(' ', '')}.com.tr">Site</a>
        <a class="member-profile" href="/member/{name.lower().replace(' ', '-')}">Profile</a>
    </div>
    """


def _europages_card_html(name, country="Poland", products=("axle manufacturing",)):
    product_tags = "".join(f'<span class="activity-tag">{p}</span>' for p in products)
    return f"""
    <div class="company-card">
        <div class="company-name">{name}</div>
        <div class="company-country">{country}</div>
        {product_tags}
        <a class="company-website" href="https://{name.lower().replace(' ', '')}.pl">Site</a>
        <a class="company-link" href="/company/{name.lower().replace(' ', '-')}">Profile</a>
    </div>
    """


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class FakeDirectoryClient:
    def __init__(self, pages):
        self._pages = pages
        self.requested_urls = []

    def get(self, url):
        self.requested_urls.append(url)
        page_num = int(url.split("page=")[-1])
        if page_num <= len(self._pages):
            return FakeResponse(self._pages[page_num - 1])
        return FakeResponse("<html><body>no results</body></html>")


# ═════════════════════════════════════════════════════════════
# GlobalDirectoryScraper
# ═════════════════════════════════════════════════════════════

class TestGlobalDirectoryScraper:

    def test_unknown_directory_raises(self):
        with pytest.raises(ValueError, match="Unknown directory"):
            GlobalDirectoryScraper(directory="not_a_real_directory")

    def test_source_name_matches_directory(self):
        scraper = GlobalDirectoryScraper(directory="turkey_tim", http_client=FakeDirectoryClient([]), enable_delays=False)
        assert scraper.source_name == "turkey_tim"

    def test_scrape_applies_country_hint_when_card_omits_it(self):
        page1 = f"<html><body>{_tim_card_html('Istanbul Axle Co')}</body></html>"
        scraper = GlobalDirectoryScraper(directory="turkey_tim", http_client=FakeDirectoryClient([page1]), enable_delays=False)

        results = scraper.scrape("trailer axle", max_pages=2)
        assert len(results) == 1
        assert results[0].raw_data["country"] == "Turkey"
        assert results[0].raw_data["directory"] == "turkey_tim"

    def test_scrape_prefers_explicit_country_over_hint(self):
        page1 = f"<html><body>{_tim_card_html('Some Co', country='Greece')}</body></html>"
        scraper = GlobalDirectoryScraper(directory="turkey_tim", http_client=FakeDirectoryClient([page1]), enable_delays=False)

        results = scraper.scrape("trailer axle")
        assert results[0].raw_data["country"] == "Greece"

    def test_europages_has_no_hardcoded_country_hint(self):
        page1 = f"<html><body>{_europages_card_html('Warsaw Fasteners Sp')}</body></html>"
        scraper = GlobalDirectoryScraper(directory="europages_eastern_europe", http_client=FakeDirectoryClient([page1]), enable_delays=False)

        results = scraper.scrape("fasteners")
        assert results[0].raw_data["country"] == "Poland"

    def test_scrape_stops_at_empty_page(self):
        page1 = f"<html><body>{_tim_card_html('Company A')}</body></html>"
        scraper = GlobalDirectoryScraper(directory="turkey_tim", http_client=FakeDirectoryClient([page1]), enable_delays=False)
        results = scraper.scrape("widgets", max_pages=5)
        assert len(results) == 1

    def test_scrape_returns_error_result_on_first_page_failure(self):
        class FailingClient:
            def get(self, url):
                raise httpx.ConnectError("connection refused")

        scraper = GlobalDirectoryScraper(directory="vietnam_vcci", http_client=FailingClient(), enable_delays=False)
        results = scraper.scrape("widgets")
        assert len(results) == 1
        assert results[0].success is False

    def test_all_configured_directories_are_selectable(self):
        for directory in DIRECTORY_SOURCES:
            scraper = GlobalDirectoryScraper(directory=directory, http_client=FakeDirectoryClient([]), enable_delays=False)
            assert scraper.source_name == directory

    def test_directory_sources_config_shape(self):
        for directory, config in DIRECTORY_SOURCES.items():
            assert "name" in config
            assert "search_url_template" in config
            assert "selectors" in config
            assert "{query}" in config["search_url_template"]
            assert "{page}" in config["search_url_template"]


# ═════════════════════════════════════════════════════════════
# GlobalDirectoryNormalizer
# ═════════════════════════════════════════════════════════════

class TestGlobalDirectoryNormalizer:

    def test_normalise_maps_fields_and_builds_notes(self):
        normalizer = GlobalDirectoryNormalizer()
        result = normalizer.normalise({
            "company_name": "Istanbul Axle Co",
            "country": "Turkey",
            "products": ["trailer axle", "leaf spring"],
            "website": "https://istanbulaxle.com.tr",
            "directory_name": "Turkish Exporters Assembly (TIM) Member Directory",
        })

        assert result["canonical_name"] == "Istanbul Axle Co"
        assert result["domain"] == "istanbulaxle.com.tr"
        assert result["country"] == "Turkey"
        assert result["product_keywords"] == ["trailer axle", "leaf spring"]
        assert "Turkish Exporters Assembly" in result["notes"]

    def test_normalise_missing_name(self):
        normalizer = GlobalDirectoryNormalizer()
        result = normalizer.normalise({"country": "Turkey"})
        assert result["canonical_name"] == ""

    def test_normalise_drops_empty_fields(self):
        normalizer = GlobalDirectoryNormalizer()
        result = normalizer.normalise({"company_name": "Bare Co"})
        assert "domain" not in result
        assert "notes" not in result

    def test_normalise_output_compatible_with_repository(self, tmp_path):
        db_path = tmp_path / "test.db"
        initialise_schema(db_path)
        repo = SupplierRepository(db_path=db_path)
        normalizer = GlobalDirectoryNormalizer()

        supplier_data = normalizer.normalise({
            "company_name": "Istanbul Axle Co",
            "country": "Turkey",
            "products": ["trailer axle"],
            "directory_name": "TIM Member Directory",
        })
        supplier_id = repo.create_golden_record(supplier_data)
        supplier = repo.get_supplier(supplier_id)

        assert supplier["canonical_name"] == "Istanbul Axle Co"
        assert supplier["country"] == "Turkey"


# ═════════════════════════════════════════════════════════════
# Pipeline integration
# ═════════════════════════════════════════════════════════════

@pytest.fixture()
def repo(tmp_path):
    db_path = tmp_path / "test.db"
    initialise_schema(db_path)
    return SupplierRepository(db_path=db_path)


class TestPipelineExhibitionRegistration:
    """Regression coverage for a real bug: the pipeline used to register
    only ONE exhibition (CIAPE) under a fixed 'shanghai_expo' key, so
    Auto Shanghai — and now Automechanika Frankfurt — were fully built
    and unit-tested in isolation but never actually reachable through a
    real pipeline run. Every exhibition in EXHIBITION_SOURCES must now
    be independently registered."""

    def test_every_configured_exhibition_is_registered(self, repo):
        from scrapers.shanghai_expo_scraper import EXHIBITION_SOURCES
        pipeline = SupplierIntelligencePipeline(repo=repo)
        for exhibition in EXHIBITION_SOURCES:
            assert exhibition in pipeline.scrapers, f"{exhibition} not reachable via pipeline.scrapers"
            assert exhibition in pipeline.normalizers, f"{exhibition} not reachable via pipeline.normalizers"

    def test_automechanika_frankfurt_specifically_registered(self, repo):
        pipeline = SupplierIntelligencePipeline(repo=repo)
        assert "automechanika_frankfurt" in pipeline.scrapers
        assert "automechanika_frankfurt" in pipeline.normalizers

    def test_scraped_source_name_matches_exhibition_not_a_hardcoded_value(self, repo):
        """The bug: raw_source_data.source used to always say
        'shanghai_expo' literally, regardless of which exhibition was
        actually scraped, breaking provenance and incremental-scrape
        tracking for every exhibition except the one registered by
        coincidence under that exact key."""
        from scrapers.shanghai_expo_scraper import ShanghaiExpoScraper

        page1 = """<html><body>
            <div class="exhibitor-result">
                <h3 class="exhibitor-title">Frankfurt Trailer Parts GmbH</h3>
                <div class="exhibitor-country">Germany</div>
                <a class="exhibitor-website-link" href="https://example.de">Site</a>
                <a class="exhibitor-detail-link" href="/x">Profile</a>
            </div>
        </body></html>"""

        class FakeClient:
            def get(self, url):
                class R:
                    status_code = 200
                    text = page1 if "page=1" in url else "<html><body>no results</body></html>"
                    def raise_for_status(self): pass
                return R()

        scraper = ShanghaiExpoScraper(exhibition="automechanika_frankfurt", http_client=FakeClient(), enable_delays=False)
        results = scraper.scrape("trailer axle")

        assert len(results) == 1
        assert results[0].source == "automechanika_frankfurt"
        assert results[0].raw_data["country"] == "Germany"


class TestPipelineGeographicExpansion:

    def test_turkey_source_creates_supplier(self, repo):
        page1 = f"<html><body>{_tim_card_html('Istanbul Axle Co')}</body></html>"
        scrapers = {"turkey_tim": GlobalDirectoryScraper(directory="turkey_tim", http_client=FakeDirectoryClient([page1]), enable_delays=False)}
        normalizers = {"turkey_tim": GlobalDirectoryNormalizer()}

        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers=scrapers, normalizers=normalizers)
        stats = pipeline.run("trailer axle", sources=["turkey_tim"])

        assert stats["created"] == 1
        supplier = repo.list_suppliers(limit=1)[0]
        assert supplier["canonical_name"] == "Istanbul Axle Co"
        assert supplier["country"] == "Turkey"

    def test_default_pipeline_registers_all_three_geographic_sources(self, repo):
        pipeline = SupplierIntelligencePipeline(repo=repo)
        for directory in DIRECTORY_SOURCES:
            assert directory in pipeline.scrapers
            assert directory in pipeline.normalizers

    def test_multiple_geographic_sources_both_feed_the_pipeline(self, repo):
        page1 = f"<html><body>{_tim_card_html('Istanbul Axle Co')}</body></html>"
        page2 = f"<html><body>{_europages_card_html('Warsaw Fasteners Sp')}</body></html>"

        scrapers = {
            "turkey_tim": GlobalDirectoryScraper(directory="turkey_tim", http_client=FakeDirectoryClient([page1]), enable_delays=False),
            "europages_eastern_europe": GlobalDirectoryScraper(directory="europages_eastern_europe", http_client=FakeDirectoryClient([page2]), enable_delays=False),
        }
        normalizers = {
            "turkey_tim": GlobalDirectoryNormalizer(),
            "europages_eastern_europe": GlobalDirectoryNormalizer(),
        }
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers=scrapers, normalizers=normalizers)

        stats = pipeline.run("trailer axle", sources=["turkey_tim", "europages_eastern_europe"])
        assert stats["scraped"] == 2
        assert stats["created"] == 2  # different companies, different countries — no false merge

        countries = {s["country"] for s in repo.list_suppliers(limit=10)}
        assert countries == {"Turkey", "Poland"}
