"""
tests/test_phase5.py

Phase 5 test suite: IndiaMART scraper/normalizer (mocked Apify client)
and the Shanghai/China exhibition scraper/normalizer (mocked HTTP).
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from scrapers.indiamart_scraper import IndiaMartScraper
from normalizers.indiamart_normalizer import IndiaMartNormalizer
from scrapers.shanghai_expo_scraper import ShanghaiExpoScraper, EXHIBITION_SOURCES
from normalizers.expo_normalizer import ExpoNormalizer
from storage.database import initialise_schema
from storage.repository import SupplierRepository


# ═════════════════════════════════════════════════════════════
# IndiaMartScraper — fake Apify client (reusing the Phase 2 pattern)
# ═════════════════════════════════════════════════════════════

class FakeDataset:
    def __init__(self, items):
        self._items = items

    def iterate_items(self):
        return iter(self._items)


class FakeActorHandle:
    def __init__(self, run_result=None, error=None):
        self._run_result = run_result or {"defaultDatasetId": "ds1"}
        self._error = error
        self.last_call_kwargs = None

    def call(self, run_input=None, **kwargs):
        # **kwargs (not a hardcoded run_timeout=/max_items=/timeout_secs=)
        # deliberately, so this fake doesn't go stale again the next time
        # apify-client's own ActorClient.call() signature changes -- it
        # already has once, silently, since this was first written.
        self.last_call_kwargs = {"run_input": run_input, **kwargs}
        if self._error:
            raise self._error
        return self._run_result


class FakeApifyClient:
    def __init__(self, dataset_items=None, actor_error=None):
        self._dataset_items = dataset_items or []
        self._actor_error = actor_error
        self.last_actor_handle = None

    def actor(self, actor_id):
        self.last_actor_handle = FakeActorHandle(error=self._actor_error)
        return self.last_actor_handle

    def dataset(self, dataset_id):
        return FakeDataset(self._dataset_items)


THIS_YEAR = date.today().year

INDIAMART_ITEM_TRUSTSEAL = {
    "sellerId": "IM001",
    "companyName": "Mumbai Fastener Works",
    "sellerUrl": "https://mumbaifastenerworks.indiamart.com",
    "city": "Mumbai",
    "state": "Maharashtra",
    "trustSealVerified": True,
    "memberSince": str(THIS_YEAR - 8),
    "mainProducts": ["hex bolts", "trailer fasteners"],
    "contactPerson": "Raj Patel",
    "email": "sales@mumbaifastenerworks.com",
    "gstNumber": "27AAAAA0000A1Z5",
    "natureOfBusiness": "Manufacturer, Exporter",
}

INDIAMART_ITEM_NO_TRUSTSEAL_NEW = {
    "sellerId": "IM002",
    "companyName": "Fresh Traders Co",
    "trustSealVerified": False,
    "memberSince": str(THIS_YEAR - 1),
}


class TestIndiaMartScraper:

    def test_call_uses_the_current_apify_client_kwargs_not_the_removed_ones(self):
        """See AlibabaScraper's identical regression test for why --
        same bug, same fix, same underlying apify-client SDK."""
        from datetime import timedelta

        client = FakeApifyClient(dataset_items=[INDIAMART_ITEM_TRUSTSEAL])
        scraper = IndiaMartScraper(client=client, enable_delays=False)

        scraper.scrape("hex bolts", max_results=9)

        kwargs = client.last_actor_handle.last_call_kwargs
        assert "timeout_secs" not in kwargs
        assert isinstance(kwargs["run_timeout"], timedelta)
        assert kwargs["max_items"] == 9

    def test_scrape_filters_by_trustseal(self):
        client = FakeApifyClient(dataset_items=[INDIAMART_ITEM_TRUSTSEAL, INDIAMART_ITEM_NO_TRUSTSEAL_NEW])
        scraper = IndiaMartScraper(client=client, enable_delays=False)

        results = scraper.scrape("hex bolts", require_trustseal=True)

        assert len(results) == 1
        assert results[0].raw_data["companyName"] == "Mumbai Fastener Works"

    def test_scrape_filters_by_min_years_registered(self):
        client = FakeApifyClient(dataset_items=[INDIAMART_ITEM_TRUSTSEAL, INDIAMART_ITEM_NO_TRUSTSEAL_NEW])
        scraper = IndiaMartScraper(client=client, enable_delays=False)

        results = scraper.scrape("fasteners", min_years_registered=5)

        assert len(results) == 1
        assert results[0].raw_data["sellerId"] == "IM001"

    def test_scrape_no_filters_returns_all(self):
        client = FakeApifyClient(dataset_items=[INDIAMART_ITEM_TRUSTSEAL, INDIAMART_ITEM_NO_TRUSTSEAL_NEW])
        scraper = IndiaMartScraper(client=client, enable_delays=False)

        results = scraper.scrape("fasteners")
        assert len(results) == 2

    def test_scrape_returns_error_result_on_actor_failure(self):
        client = FakeApifyClient(actor_error=RuntimeError("actor crashed"))
        scraper = IndiaMartScraper(client=client, enable_delays=False)

        results = scraper.scrape("fasteners")
        assert len(results) == 1
        assert results[0].success is False
        assert "actor crashed" in results[0].error

    def test_years_registered_handles_missing_and_malformed(self):
        assert IndiaMartScraper._years_registered({}) is None
        assert IndiaMartScraper._years_registered({"memberSince": "not-a-year"}) is None
        assert IndiaMartScraper._years_registered({"memberSince": str(THIS_YEAR - 3)}) == 3

    def test_client_property_raises_without_token_or_injection(self, monkeypatch):
        monkeypatch.setattr("scrapers.indiamart_scraper.APIFY_TOKEN", None)
        scraper = IndiaMartScraper(enable_delays=False)
        with pytest.raises(RuntimeError, match="APIFY_TOKEN"):
            _ = scraper.client


# ═════════════════════════════════════════════════════════════
# IndiaMartNormalizer
# ═════════════════════════════════════════════════════════════

class TestIndiaMartNormalizer:

    def test_normalise_maps_core_fields(self):
        normalizer = IndiaMartNormalizer()
        result = normalizer.normalise(INDIAMART_ITEM_TRUSTSEAL)

        assert result["canonical_name"] == "Mumbai Fastener Works"
        assert result["domain"] == "mumbaifastenerworks.indiamart.com"
        assert result["country"] == "India"
        assert result["city"] == "Mumbai"
        assert result["province_state"] == "Maharashtra"
        assert result["contact_name"] == "Raj Patel"
        assert result["primary_email"] == "sales@mumbaifastenerworks.com"
        assert result["company_reg_number"] == "27AAAAA0000A1Z5"
        assert "hex bolts" in result["product_keywords"]
        assert result["is_manufacturer"] is True
        assert result["other_certifications"] == ["IndiaMART TrustSEAL Verified"]
        assert result["year_established"] == THIS_YEAR - 8

    def test_normalise_defaults_country_to_india(self):
        normalizer = IndiaMartNormalizer()
        result = normalizer.normalise({"companyName": "Bare Seller Co"})
        assert result["country"] == "India"

    def test_normalise_respects_explicit_country(self):
        normalizer = IndiaMartNormalizer()
        result = normalizer.normalise({"companyName": "Foo Co", "country": "Nepal"})
        assert result["country"] == "Nepal"

    def test_normalise_handles_missing_company_name(self):
        normalizer = IndiaMartNormalizer()
        result = normalizer.normalise({"sellerId": "IM999"})
        assert result["canonical_name"] == ""

    def test_normalise_detects_trader_not_manufacturer(self):
        normalizer = IndiaMartNormalizer()
        result = normalizer.normalise({
            "companyName": "Trader Co", "natureOfBusiness": "Wholesaler and Trader",
        })
        assert result["is_manufacturer"] is False

    def test_normalise_no_trustseal_omits_certifications(self):
        normalizer = IndiaMartNormalizer()
        result = normalizer.normalise({"companyName": "Foo Co", "trustSealVerified": False})
        assert "other_certifications" not in result

    def test_normalise_alias_variants(self):
        normalizer = IndiaMartNormalizer()
        raw = {
            "name": "Alt Field Co",         # alias for company_name
            "profileUrl": "www.altfield.in", # alias for profile_url, no scheme
            "mobile": "+91 9876543210",       # alias for phone
            "gstin": "07AAAAA0000A1Z5",       # alias for gstin
        }
        result = normalizer.normalise(raw)
        assert result["canonical_name"] == "Alt Field Co"
        assert result["domain"] == "altfield.in"
        assert result["primary_phone"] == "+91 9876543210"
        assert result["company_reg_number"] == "07AAAAA0000A1Z5"

    def test_normalise_output_compatible_with_repository(self, tmp_path):
        db_path = tmp_path / "test.db"
        initialise_schema(db_path)
        repo = SupplierRepository(db_path=db_path)

        normalizer = IndiaMartNormalizer()
        supplier_data = normalizer.normalise(INDIAMART_ITEM_TRUSTSEAL)
        supplier_id = repo.create_golden_record(supplier_data)
        supplier = repo.get_supplier(supplier_id)

        assert supplier["canonical_name"] == "Mumbai Fastener Works"
        assert supplier["country"] == "India"
        assert supplier["is_manufacturer"] == 1
        assert supplier["other_certifications"] == ["IndiaMART TrustSEAL Verified"]


# ═════════════════════════════════════════════════════════════
# ShanghaiExpoScraper — fake httpx client
# ═════════════════════════════════════════════════════════════

def _exhibitor_card_html(name, country="China", products=("LED marker lights",), booth="B123"):
    product_tags = "".join(f'<span class="product-tag">{p}</span>' for p in products)
    return f"""
    <div class="exhibitor-card">
        <h4 class="exhibitor-name">{name}</h4>
        <div class="booth-no">{booth}</div>
        <div class="exhibitor-country">{country}</div>
        {product_tags}
        <a class="exhibitor-website" href="https://{name.lower().replace(' ', '')}.com">Site</a>
        <a class="exhibitor-profile" href="/exhibitor/{name.lower().replace(' ', '-')}">Profile</a>
    </div>
    """


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class FakeExpoClient:
    def __init__(self, pages):
        self._pages = pages
        self.requested_urls = []

    def get(self, url):
        self.requested_urls.append(url)
        page_num = int(url.split("page=")[-1])
        if page_num <= len(self._pages):
            return FakeResponse(self._pages[page_num - 1])
        return FakeResponse("<html><body>no results</body></html>")


class TestShanghaiExpoScraper:

    def test_unknown_exhibition_raises(self):
        with pytest.raises(ValueError, match="Unknown exhibition"):
            ShanghaiExpoScraper(exhibition="not_a_real_expo")

    def test_scrape_parses_exhibitor_cards(self):
        page1 = f"""
        <html><body>
        {_exhibitor_card_html("LED Masters China")}
        {_exhibitor_card_html("Fastener Exhibitors Co")}
        </body></html>
        """
        client = FakeExpoClient(pages=[page1])
        scraper = ShanghaiExpoScraper(exhibition="ciape", http_client=client, enable_delays=False)

        results = scraper.scrape("LED lighting", max_pages=3)

        assert len(results) == 2
        assert results[0].source == "ciape"  # bug fix: was hardcoded to "shanghai_expo" regardless of exhibition
        assert results[0].raw_data["company_name"] == "LED Masters China"
        assert results[0].raw_data["exhibition"] == "ciape"
        assert results[0].raw_data["exhibition_name"] == "China International Auto Parts Expo"
        assert results[0].raw_data["booth_number"] == "B123"

    def test_scrape_stops_at_empty_page(self):
        page1 = f"<html><body>{_exhibitor_card_html('Exhibitor A')}</body></html>"
        client = FakeExpoClient(pages=[page1])
        scraper = ShanghaiExpoScraper(http_client=client, enable_delays=False)

        results = scraper.scrape("widgets", max_pages=5)
        assert len(results) == 1
        assert len(client.requested_urls) == 2

    def test_scrape_returns_error_result_on_first_page_failure(self):
        class FailingClient:
            def get(self, url):
                raise httpx.ConnectError("connection refused")

        scraper = ShanghaiExpoScraper(http_client=FailingClient(), enable_delays=False)
        results = scraper.scrape("widgets")
        assert len(results) == 1
        assert results[0].success is False

    def test_second_exhibition_config_selectable(self):
        page1 = """
        <html><body>
        <div class="exhibitor-item">
            <div class="company-name">Auto Shanghai Exhibitor</div>
            <div class="booth">A1</div>
            <div class="country">China</div>
            <span class="category-tag">brake parts</span>
            <a class="website-link" href="https://example.com">Site</a>
            <a class="detail-link" href="/detail/foo">Detail</a>
        </div>
        </body></html>
        """
        client = FakeExpoClient(pages=[page1])
        scraper = ShanghaiExpoScraper(exhibition="auto_shanghai", http_client=client, enable_delays=False)

        results = scraper.scrape("brake parts")
        assert len(results) == 1
        assert results[0].raw_data["company_name"] == "Auto Shanghai Exhibitor"
        assert results[0].raw_data["exhibition"] == "auto_shanghai"

    def test_exhibition_sources_config_not_empty(self):
        assert "ciape" in EXHIBITION_SOURCES
        assert "selectors" in EXHIBITION_SOURCES["ciape"]


# ═════════════════════════════════════════════════════════════
# ExpoNormalizer
# ═════════════════════════════════════════════════════════════

class TestExpoNormalizer:

    def test_normalise_maps_fields_and_builds_notes(self):
        normalizer = ExpoNormalizer()
        raw = {
            "company_name": "LED Masters China",
            "country": "China",
            "products": ["LED marker lights"],
            "website": "https://ledmasterschina.com",
            "booth_number": "B123",
            "exhibition": "ciape",
            "exhibition_name": "China International Auto Parts Expo",
        }
        result = normalizer.normalise(raw)

        assert result["canonical_name"] == "LED Masters China"
        assert result["domain"] == "ledmasterschina.com"
        assert result["country"] == "China"
        assert result["product_keywords"] == ["LED marker lights"]
        assert "China International Auto Parts Expo" in result["notes"]
        assert "B123" in result["notes"]

    def test_normalise_handles_missing_company_name(self):
        normalizer = ExpoNormalizer()
        result = normalizer.normalise({"country": "China"})
        assert result["canonical_name"] == ""

    def test_normalise_drops_empty_fields(self):
        normalizer = ExpoNormalizer()
        result = normalizer.normalise({"company_name": "Bare Co"})
        assert "domain" not in result
        assert "notes" not in result

    def test_normalise_output_compatible_with_repository(self, tmp_path):
        db_path = tmp_path / "test.db"
        initialise_schema(db_path)
        repo = SupplierRepository(db_path=db_path)
        normalizer = ExpoNormalizer()

        supplier_data = normalizer.normalise({
            "company_name": "LED Masters China",
            "country": "China",
            "products": ["LED marker lights"],
            "exhibition_name": "China International Auto Parts Expo",
            "booth_number": "B123",
        })
        supplier_id = repo.create_golden_record(supplier_data)
        supplier = repo.get_supplier(supplier_id)

        assert supplier["canonical_name"] == "LED Masters China"
        assert "China International Auto Parts Expo" in supplier["notes"]
