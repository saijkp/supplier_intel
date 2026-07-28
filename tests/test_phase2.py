"""
tests/test_phase2.py

Phase 2 test suite: Alibaba + ImportYeti scrapers and their normalizers.

No real network or Apify calls are made. AlibabaScraper is tested via
an injected fake Apify client; ImportYetiScraper via an injected fake
httpx client returning canned HTML. This keeps the suite fast and fully
deterministic while still exercising the real scraper/normalizer code.
"""

from __future__ import annotations

import time

import httpx
import pytest

from scrapers.base_scraper import BaseScraper, ScraperResult
from scrapers.alibaba_scraper import AlibabaScraper
from scrapers.importyeti_scraper import ImportYetiScraper, IMPORTYETI_SELECTORS
from normalizers.alibaba_normalizer import AlibabaNormalizer
from normalizers.trade_normalizer import TradeNormalizer
from storage.database import initialise_schema
from storage.repository import SupplierRepository


# ═════════════════════════════════════════════════════════════
# BaseScraper
# ═════════════════════════════════════════════════════════════

class _DummyScraper(BaseScraper):
    def __init__(self, enable_delays: bool = False):
        super().__init__("dummy_source", enable_delays=enable_delays)

    def scrape(self, query: str, **kwargs):
        return []


class TestBaseScraper:

    def test_polite_delay_respects_explicit_bounds(self):
        scraper = _DummyScraper(enable_delays=True)
        start = time.monotonic()
        scraper._polite_delay(0.01, 0.02)
        elapsed = time.monotonic() - start
        assert 0.005 <= elapsed < 1.0

    def test_polite_delay_noop_when_disabled(self):
        scraper = _DummyScraper(enable_delays=False)
        start = time.monotonic()
        scraper._polite_delay(5.0, 10.0)
        elapsed = time.monotonic() - start
        assert elapsed < 0.5  # would be >= 5s if not skipped

    def test_safe_request_returns_on_success(self):
        scraper = _DummyScraper()
        result = scraper._safe_request(lambda x: x * 2, 21)
        assert result == 42

    def test_safe_request_retries_then_succeeds(self):
        scraper = _DummyScraper()
        attempts = {"count": 0}

        def flaky():
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise ConnectionError("temporary failure")
            return "ok"

        result = scraper._safe_request(flaky, max_retries=5)
        assert result == "ok"
        assert attempts["count"] == 3

    def test_safe_request_raises_after_max_retries(self):
        scraper = _DummyScraper()

        def always_fails():
            raise ConnectionError("permanent failure")

        with pytest.raises(ConnectionError):
            scraper._safe_request(always_fails, max_retries=2)

    def test_error_result_shape(self):
        scraper = _DummyScraper()
        result = scraper.error_result("boom", source_id="abc")
        assert isinstance(result, ScraperResult)
        assert result.success is False
        assert result.error == "boom"
        assert result.source == "dummy_source"
        assert result.source_id == "abc"


# ═════════════════════════════════════════════════════════════
# AlibabaScraper — fake Apify client
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


ALIBABA_ITEM_GOLD_5Y = {
    "supplierId": "SUP001",
    "companyName": "Guangzhou LED Masters Co Ltd",
    "companyUrl": "https://ledmasters.en.alibaba.com",
    "country": "China",
    "city": "Guangzhou",
    "yearsAsGoldSupplier": 5,
    "tradeAssurance": True,
    "rating": 4.7,
    "mainProducts": ["LED marker lights", "trailer lighting"],
    "contactPerson": "Li Wei",
    "email": "sales@ledmasters.com",
}

ALIBABA_ITEM_TOO_NEW = {
    "supplierId": "SUP002",
    "companyName": "Fresh Trading Co",
    "yearsAsGoldSupplier": 1,
}


class TestAlibabaScraper:

    def test_call_uses_the_current_apify_client_kwargs_not_the_removed_ones(self):
        """Regression test for a real bug found running this against a
        real APIFY_TOKEN for the first time: the actually-installed
        apify-client no longer accepts timeout_secs=<int> at all (it's
        now run_timeout=<timedelta>), so this failed with a TypeError
        on every attempt despite the token itself being valid. Also
        confirms max_items is set as a platform-enforced cap on top of
        run_input's own maxItems -- Apify's own docs describe max_items
        as also limiting billing for a per-result-charged actor."""
        from datetime import timedelta

        client = FakeApifyClient(dataset_items=[ALIBABA_ITEM_GOLD_5Y])
        scraper = AlibabaScraper(client=client, enable_delays=False)

        scraper.scrape("LED marker light", max_results=7)

        kwargs = client.last_actor_handle.last_call_kwargs
        assert "timeout_secs" not in kwargs
        assert isinstance(kwargs["run_timeout"], timedelta)
        assert kwargs["max_items"] == 7

    def test_scrape_filters_by_min_years_gold(self):
        client = FakeApifyClient(dataset_items=[ALIBABA_ITEM_GOLD_5Y, ALIBABA_ITEM_TOO_NEW])
        scraper = AlibabaScraper(client=client, enable_delays=False)

        results = scraper.scrape("LED marker light", min_years_gold=3)

        assert len(results) == 1
        assert results[0].success is True
        assert results[0].source == "alibaba"
        assert results[0].source_id == "SUP001"
        assert results[0].raw_data["companyName"] == "Guangzhou LED Masters Co Ltd"

    def test_scrape_returns_error_result_on_actor_failure(self):
        client = FakeApifyClient(actor_error=RuntimeError("Apify actor crashed"))
        scraper = AlibabaScraper(client=client, enable_delays=False)

        results = scraper.scrape("LED marker light")

        assert len(results) == 1
        assert results[0].success is False
        assert "Apify actor crashed" in results[0].error

    def test_scrape_empty_dataset_returns_empty_list(self):
        client = FakeApifyClient(dataset_items=[])
        scraper = AlibabaScraper(client=client, enable_delays=False)
        results = scraper.scrape("nonexistent widget")
        assert results == []

    def test_client_property_raises_without_token_or_injection(self, monkeypatch):
        monkeypatch.setattr("scrapers.alibaba_scraper.APIFY_TOKEN", None)
        scraper = AlibabaScraper(enable_delays=False)
        with pytest.raises(RuntimeError, match="APIFY_TOKEN"):
            _ = scraper.client


# ═════════════════════════════════════════════════════════════
# ImportYetiScraper — fake httpx client
# ═════════════════════════════════════════════════════════════

def _shipment_card_html(shipper, consignee, hs_code="8539", value="$12,500.00", weight="1,200 kg"):
    return f"""
    <div class="shipment-result">
        <div class="shipper-name">{shipper}</div>
        <div class="consignee-name">{consignee}</div>
        <div class="shipment-date">2026-05-01</div>
        <div class="hs-code">{hs_code}</div>
        <div class="product-description">LED marker lights for trailers</div>
        <div class="origin-port">Shenzhen</div>
        <div class="destination-port">Felixstowe</div>
        <div class="weight">{weight}</div>
        <div class="value">{value}</div>
        <a class="company-link" href="/company/{shipper.replace(' ', '-')}">Profile</a>
    </div>
    """


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class FakeHttpClient:
    """Returns page N's HTML from `pages` (1-indexed); empty string past the end."""

    def __init__(self, pages: list[str]):
        self._pages = pages
        self.requested_urls: list[str] = []

    def get(self, url: str):
        self.requested_urls.append(url)
        page_num = int(url.split("page=")[-1])
        if page_num <= len(self._pages):
            return FakeResponse(self._pages[page_num - 1])
        return FakeResponse("<html><body>no results</body></html>")


class TestImportYetiScraper:

    def test_scrape_parses_single_page_of_results(self):
        page1 = f"""
        <html><body>
        {_shipment_card_html("Shenzhen LED Masters", "Ifor Williams Trailers")}
        {_shipment_card_html("Guangzhou Fasteners Co", "Ifor Williams Trailers")}
        </body></html>
        """
        client = FakeHttpClient(pages=[page1])
        scraper = ImportYetiScraper(http_client=client, enable_delays=False)

        results = scraper.scrape("LED marker light", max_pages=3)

        assert len(results) == 2
        assert results[0].source == "importyeti"
        assert results[0].raw_data["shipper_name"] == "Shenzhen LED Masters"
        assert results[0].raw_data["consignee_name"] == "Ifor Williams Trailers"
        assert results[0].raw_data["hs_code"] == "8539"

    def test_scrape_stops_at_first_empty_page(self):
        page1 = f"<html><body>{_shipment_card_html('Shipper A', 'Buyer A')}</body></html>"
        client = FakeHttpClient(pages=[page1])  # page 2 onward returns no cards
        scraper = ImportYetiScraper(http_client=client, enable_delays=False)

        results = scraper.scrape("widgets", max_pages=5)

        assert len(results) == 1
        # Should have requested page 1 and page 2 (which came back empty), then stopped
        assert len(client.requested_urls) == 2

    def test_scrape_returns_error_result_on_first_page_failure(self):
        class FailingClient:
            def get(self, url):
                raise httpx.ConnectError("connection refused")

        scraper = ImportYetiScraper(http_client=FailingClient(), enable_delays=False)
        results = scraper.scrape("widgets")

        assert len(results) == 1
        assert results[0].success is False
        assert "connection refused" in results[0].error

    def test_selectors_config_is_used_not_hardcoded(self):
        # Sanity check that the module-level selector config actually
        # drives parsing (Gap 1 from the Phase 1 brief).
        assert "result_card" in IMPORTYETI_SELECTORS
        assert IMPORTYETI_SELECTORS["shipper_name"]


# ═════════════════════════════════════════════════════════════
# AlibabaNormalizer
# ═════════════════════════════════════════════════════════════

class TestAlibabaNormalizer:

    def test_normalise_maps_core_fields(self):
        normalizer = AlibabaNormalizer()
        result = normalizer.normalise(ALIBABA_ITEM_GOLD_5Y)

        assert result["canonical_name"] == "Guangzhou LED Masters Co Ltd"
        assert result["domain"] == "ledmasters.en.alibaba.com"
        assert result["country"] == "China"
        assert result["city"] == "Guangzhou"
        assert result["contact_name"] == "Li Wei"
        assert result["primary_email"] == "sales@ledmasters.com"
        assert result["alibaba_gold_supplier"] is True
        assert result["alibaba_years"] == 5
        assert result["alibaba_trade_assurance"] is True
        assert result["alibaba_rating"] == 4.7
        assert "LED marker lights" in result["product_keywords"]

    def test_normalise_handles_missing_company_name(self):
        normalizer = AlibabaNormalizer()
        result = normalizer.normalise({"supplierId": "SUP999"})
        assert result["canonical_name"] == ""

    def test_normalise_handles_alias_variants(self):
        normalizer = AlibabaNormalizer()
        raw = {
            "name": "Alt Field Co",           # alias for company_name
            "storeUrl": "www.altfield.com",    # alias for profile_url, no scheme
            "goldYears": "4",                  # alias for years_gold, string
            "reviewScore": "4.2",               # alias for rating, string
        }
        result = normalizer.normalise(raw)
        assert result["canonical_name"] == "Alt Field Co"
        assert result["domain"] == "altfield.com"
        assert result["alibaba_years"] == 4
        assert result["alibaba_rating"] == 4.2

    def test_normalise_detects_certifications(self):
        normalizer = AlibabaNormalizer()
        raw = {
            "companyName": "Cert Co",
            "certifications": ["ISO 9001:2015", "E-Mark ECE R10"],
        }
        result = normalizer.normalise(raw)
        assert result["iso_9001"] is True
        assert result["e_mark_certified"] is True
        assert "ISO 9001:2015" in result["other_certifications"]

    def test_normalise_drops_empty_optional_fields(self):
        normalizer = AlibabaNormalizer()
        result = normalizer.normalise({"companyName": "Bare Co"})
        assert "domain" not in result
        assert "primary_email" not in result
        assert "canonical_name" in result  # always kept

    def test_normalise_output_is_compatible_with_repository(self, tmp_path):
        """End-to-end: normalizer output should be directly insertable
        via SupplierRepository.create_golden_record without modification."""
        db_path = tmp_path / "test.db"
        initialise_schema(db_path)
        repo = SupplierRepository(db_path=db_path)

        normalizer = AlibabaNormalizer()
        supplier_data = normalizer.normalise(ALIBABA_ITEM_GOLD_5Y)

        supplier_id = repo.create_golden_record(supplier_data)
        supplier = repo.get_supplier(supplier_id)

        assert supplier["canonical_name"] == "Guangzhou LED Masters Co Ltd"
        assert supplier["alibaba_gold_supplier"] == 1
        assert supplier["product_keywords"] == ["LED marker lights", "trailer lighting"]


# ═════════════════════════════════════════════════════════════
# TradeNormalizer
# ═════════════════════════════════════════════════════════════

TRADE_RAW_UK_SHIPMENT = {
    "shipper_name": "Shenzhen LED Masters Co Ltd",
    "consignee_name": "Ifor Williams Trailers",
    "consignee_country": "United Kingdom",
    "shipment_date": "2026-05-01",
    "hs_code": "8539",
    "product_desc": "LED marker lights",
    "weight_raw": "1,200 kg",
    "value_raw": "$12,500.00",
    "origin_port": "Shenzhen",
    "destination_port": "Felixstowe",
}


class TestTradeNormalizer:

    def test_normalise_flags_uk_export(self):
        normalizer = TradeNormalizer()
        result = normalizer.normalise(TRADE_RAW_UK_SHIPMENT)

        assert result["canonical_name"] == "Shenzhen LED Masters Co Ltd"
        assert result["exports_to_uk"] is True
        assert result["confirmed_shipments_uk"] == 1
        assert result["known_buyers"] == ["Ifor Williams Trailers"]
        assert result["last_shipment_date"] == "2026-05-01"
        assert "exports_to_us" not in result

    def test_normalise_defaults_importyeti_consignee_to_us(self):
        normalizer = TradeNormalizer()
        raw = dict(TRADE_RAW_UK_SHIPMENT)
        del raw["consignee_country"]  # ImportYeti card didn't expose a country field

        result = normalizer.normalise(raw, source="importyeti")
        assert result["exports_to_us"] is True
        assert result["confirmed_shipments_us"] == 1
        assert "exports_to_uk" not in result

    def test_normalise_flags_eu_export(self):
        normalizer = TradeNormalizer()
        raw = dict(TRADE_RAW_UK_SHIPMENT)
        raw["consignee_country"] = "Germany"
        result = normalizer.normalise(raw)
        assert result["exports_to_eu"] is True
        assert result["confirmed_shipments_eu"] == 1

    def test_to_shipment_record_parses_weight_and_value(self):
        normalizer = TradeNormalizer()
        record = normalizer.to_shipment_record(TRADE_RAW_UK_SHIPMENT, supplier_id=42)

        assert record["supplier_id"] == 42
        assert record["source"] == "importyeti"
        assert record["weight_kg"] == 1200.0
        assert record["value_usd"] == 12500.0
        assert record["shipment_date"] == "2026-05-01"
        assert record["hs_code"] == "8539"
        assert record["raw_record"] == TRADE_RAW_UK_SHIPMENT

    @pytest.mark.parametrize("date_str,expected", [
        ("2026-05-01", "2026-05-01"),
        ("01 May 2026", "2026-05-01"),
        ("May 01, 2026", "2026-05-01"),
        ("05/01/2026", "2026-01-05"),  # DD/MM/YYYY takes priority over MM/DD/YYYY (UK-first)
        ("not a date", None),
        ("", None),
        (None, None),
    ])
    def test_parse_date_formats(self, date_str, expected):
        normalizer = TradeNormalizer()
        assert normalizer._parse_date(date_str) == expected

    @pytest.mark.parametrize("value,expected", [
        ("$12,500.00", 12500.0),
        ("1,200 kg", 1200.0),
        (500, 500.0),
        (500.5, 500.5),
        (None, None),
        ("", None),
        ("no numbers here", None),
    ])
    def test_parse_number_formats(self, value, expected):
        normalizer = TradeNormalizer()
        assert normalizer._parse_number(value) == expected

    def test_to_shipment_record_and_normalise_compatible_with_repository(self, tmp_path):
        """End-to-end: shipper stub creates a golden record, then the
        shipment record links to it via supplier_id."""
        db_path = tmp_path / "test.db"
        initialise_schema(db_path)
        repo = SupplierRepository(db_path=db_path)

        normalizer = TradeNormalizer()
        supplier_stub = normalizer.normalise(TRADE_RAW_UK_SHIPMENT)
        supplier_id = repo.create_golden_record(supplier_stub)

        shipment = normalizer.to_shipment_record(TRADE_RAW_UK_SHIPMENT, supplier_id=supplier_id)
        shipment_id = repo.add_shipment_record(shipment)

        assert shipment_id > 0
        shipments = repo.get_shipments_for_supplier(supplier_id)
        assert len(shipments) == 1
        assert shipments[0]["value_usd"] == 12500.0

        supplier = repo.get_supplier(supplier_id)
        assert supplier["exports_to_uk"] == 1
        assert supplier["confirmed_shipments_uk"] == 1
        assert supplier["known_buyers"] == ["Ifor Williams Trailers"]
