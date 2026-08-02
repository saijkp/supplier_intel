"""
tests/test_scraper_1688.py

Tests for scrapers.scraper_1688.China1688Scraper and
normalizers.china_1688_normalizer.China1688Normalizer. Same fake-Apify-
client pattern already established in tests/test_phase2.py (Alibaba)
and tests/test_phase5.py (IndiaMART) -- each test file keeps its own
small local fakes rather than sharing them across files, matching that
existing convention.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from normalizers.china_1688_normalizer import China1688Normalizer
from scrapers.scraper_1688 import China1688Scraper


_UNSET = object()  # distinguishes "not provided" (use the default Run) from an explicit None


class FakeDataset:
    def __init__(self, items):
        self._items = items

    def iterate_items(self):
        return iter(self._items)


class FakeActorHandle:
    def __init__(self, run_result=_UNSET, error=None):
        self._run_result = SimpleNamespace(default_dataset_id="ds1") if run_result is _UNSET else run_result
        self._error = error
        self.last_call_kwargs = None

    def call(self, run_input=None, **kwargs):
        self.last_call_kwargs = {"run_input": run_input, **kwargs}
        if self._error:
            raise self._error
        return self._run_result


class FakeApifyClient:
    def __init__(self, dataset_items=None, actor_error=None, run_result=_UNSET):
        self._dataset_items = dataset_items or []
        self._actor_error = actor_error
        self._run_result = run_result
        self.last_actor_handle = None

    def actor(self, actor_id):
        self.last_actor_handle = FakeActorHandle(run_result=self._run_result, error=self._actor_error)
        return self.last_actor_handle

    def dataset(self, dataset_id):
        return FakeDataset(self._dataset_items)


# Trimmed from an actual --limit 3 live run against the real actor
# (2026-08-02, query "wheel hub") -- real product, real flat field
# names, just with the bulk fields (full image lists, price tiers,
# etc.) stripped down to what these tests actually check. The FIRST
# version of these fixtures guessed a nested "supplier" object that
# doesn't exist in the real output -- see china_1688_normalizer.py's
# own module docstring for the real bug that shape guess caused
# (a false merge between two different real companies).
PRODUCT_ITEM = {
    "recordType": "product",
    "offerId": "548589810467",
    "url": "https://detail.1688.com/offer/548589810467.html",
    "title": "供应Hub Steering Wheel Adapter P206方向盘连接座底座",
    "titleEn": "Supply Hub Steering Wheel Adapter P206 steering wheel connection seat base",
    "companyName": "瑞安市嘉业汽摩附件有限公司",
    "sellerLoginId": "zjjiaye",
    "supplierUrl": "https://winport.m.1688.com/page/index.html?memberId=zjjiaye",
    "location": "浙江省温州市",
    "province": "浙江省",
    "city": "温州市",
    "merchantSigns": {
        "powerfulMerchant": False,
        "trustPass": True,
        "factory": True,
        "industrySeller": False,
    },
    "merchantTags": [],
    "source": "1688",
}

PRODUCT_ITEM_NO_SUPPLIER_NAME = {
    "offerId": "OFFER002",
    "titleEn": "Unnamed Co Product",
}


class TestChina1688ScraperRequestShape:

    def test_call_uses_current_apify_client_kwargs(self):
        """Same regression coverage as AlibabaScraper: this actor is
        also PAY_PER_EVENT, so max_total_charge_usd must be set or
        Apify aborts the run at $0.00, and run_timeout (not the
        removed timeout_secs) is required by the installed SDK."""
        client = FakeApifyClient(dataset_items=[PRODUCT_ITEM])
        scraper = China1688Scraper(client=client, enable_delays=False)

        scraper.scrape("wheel hub", max_results=5)

        kwargs = client.last_actor_handle.last_call_kwargs
        assert "timeout_secs" not in kwargs
        assert isinstance(kwargs["run_timeout"], timedelta)
        assert isinstance(kwargs["max_total_charge_usd"], Decimal)
        assert kwargs["max_total_charge_usd"] > 0
        assert kwargs["max_items"] == 5

    def test_run_input_uses_search_queries_and_max_products(self):
        client = FakeApifyClient(dataset_items=[])
        scraper = China1688Scraper(client=client, enable_delays=False)

        scraper.scrape("wheel hub", max_results=5)

        run_input = client.last_actor_handle.last_call_kwargs["run_input"]
        assert run_input["searchQueries"] == ["wheel hub"]
        assert run_input["maxProducts"] == 5

    def test_require_super_factory_true_by_default(self):
        client = FakeApifyClient(dataset_items=[])
        scraper = China1688Scraper(client=client, enable_delays=False)

        scraper.scrape("wheel hub")

        run_input = client.last_actor_handle.last_call_kwargs["run_input"]
        assert run_input["merchantType"] == "superFactory"

    def test_require_super_factory_false_widens_to_any(self):
        client = FakeApifyClient(dataset_items=[])
        scraper = China1688Scraper(client=client, enable_delays=False)

        scraper.scrape("wheel hub", require_super_factory=False)

        run_input = client.last_actor_handle.last_call_kwargs["run_input"]
        assert run_input["merchantType"] == "any"


class TestChina1688ScraperResults:

    def test_scrape_returns_results_from_dataset(self):
        client = FakeApifyClient(dataset_items=[PRODUCT_ITEM])
        scraper = China1688Scraper(client=client, enable_delays=False)

        results = scraper.scrape("wheel hub")

        assert len(results) == 1
        assert results[0].success is True
        assert results[0].source == "china_1688"
        assert results[0].source_id == "548589810467"

    def test_scrape_returns_error_result_on_actor_failure(self):
        client = FakeApifyClient(actor_error=RuntimeError("1688 actor crashed"))
        scraper = China1688Scraper(client=client, enable_delays=False)

        results = scraper.scrape("wheel hub")

        assert len(results) == 1
        assert results[0].success is False
        assert "1688 actor crashed" in results[0].error

    def test_none_run_result_is_handled_not_raised(self):
        """Mirrors AlibabaScraper's identical guard: a pay-per-event
        run aborted before completing (e.g. at a $0 cost ceiling) can
        return None instead of a Run object."""
        client = FakeApifyClient(run_result=None)
        scraper = China1688Scraper(client=client, enable_delays=False)

        results = scraper.scrape("wheel hub")

        assert len(results) == 1
        assert results[0].success is False

    def test_client_property_raises_without_token_or_injection(self, monkeypatch):
        monkeypatch.setattr("scrapers.scraper_1688.APIFY_TOKEN", None)
        scraper = China1688Scraper(enable_delays=False)
        with pytest.raises(RuntimeError, match="APIFY_TOKEN"):
            _ = scraper.client


class TestChina1688NormalizerRealShape:
    """Confirmed against real raw_source_data from a live run -- see
    china_1688_normalizer.py's own module docstring for the real bug
    (a false merge between two different companies) an earlier,
    unconfirmed nested-shape guess caused, and why domain/profile_url
    are deliberately never populated from anything this actor returns."""

    def test_normalise_maps_core_fields(self):
        normalizer = China1688Normalizer()
        result = normalizer.normalise(PRODUCT_ITEM)

        assert result["canonical_name"] == "瑞安市嘉业汽摩附件有限公司"
        assert result["country"] == "China"
        assert result["province_state"] == "浙江省"
        assert result["city"] == "温州市"
        assert "Supply Hub Steering Wheel Adapter P206 steering wheel connection seat base" in result["product_keywords"]

    def test_domain_is_never_set_from_this_actors_output(self):
        """The regression test for the actual false-merge bug: neither
        the product detail URL (detail.1688.com, shared by every
        seller) nor the supplier's own 1688 shop URL (winport.m.1688.com,
        also shared) may ever become `domain` -- both are the
        marketplace's own domains, not a distinct company website."""
        normalizer = China1688Normalizer()
        result = normalizer.normalise(PRODUCT_ITEM)
        assert "domain" not in result

    def test_supplier_url_preserved_in_notes_not_as_a_domain(self):
        normalizer = China1688Normalizer()
        result = normalizer.normalise(PRODUCT_ITEM)
        assert "winport.m.1688.com" in result["notes"]

    def test_merchant_signs_captured_as_a_note_not_is_manufacturer(self):
        normalizer = China1688Normalizer()
        result = normalizer.normalise(PRODUCT_ITEM)
        assert "is_manufacturer" not in result
        assert "factory" in result["notes"]
        assert "trustPass" in result["notes"]
        # False signs are omitted, not listed as "powerfulMerchant: False"
        assert "powerfulMerchant" not in result["notes"]

    def test_missing_company_name_handled(self):
        normalizer = China1688Normalizer()
        result = normalizer.normalise(PRODUCT_ITEM_NO_SUPPLIER_NAME)
        assert result["canonical_name"] == ""

    def test_output_compatible_with_repository(self, tmp_path):
        from storage.database import initialise_schema
        from storage.repository import SupplierRepository

        db_path = tmp_path / "test.db"
        initialise_schema(db_path)
        repo = SupplierRepository(db_path=db_path)

        normalizer = China1688Normalizer()
        supplier_data = normalizer.normalise(PRODUCT_ITEM)
        supplier_id = repo.create_golden_record(supplier_data)
        supplier = repo.get_supplier(supplier_id)

        assert supplier["canonical_name"] == "瑞安市嘉业汽摩附件有限公司"
        assert supplier["country"] == "China"

    def test_two_different_real_companies_do_not_false_merge_on_domain(self, tmp_path):
        """The actual end-to-end regression: the exact two real raw
        records that caused this bug live must normalise to two
        DIFFERENT supplier records, not collide on a shared domain."""
        from deduplication.matcher import SupplierMatcher
        from storage.database import initialise_schema
        from storage.repository import SupplierRepository

        other_company_item = {
            "offerId": "947613053810",
            "titleEn": "19 pairs of WHEELNUT-F650 auto parts",
            "companyName": "温州市龙湾永中南牧五金加工厂",
            "sellerLoginId": "南牧五金",
            "supplierUrl": "https://winport.m.1688.com/page/index.html?memberId=b2b-2452594238",
            "province": "浙江省",
            "city": "温州市",
            "merchantSigns": {"factory": True, "trustPass": True},
        }

        db_path = tmp_path / "test.db"
        initialise_schema(db_path)
        repo = SupplierRepository(db_path=db_path)
        matcher = SupplierMatcher(repo)
        normalizer = China1688Normalizer()

        first = normalizer.normalise(PRODUCT_ITEM)
        second = normalizer.normalise(other_company_item)

        matcher.resolve_and_store(first)
        resolution = matcher.resolve_and_store(second)

        assert resolution["action"] == "created"  # NOT "merged"
        suppliers = repo.list_suppliers(limit=10)
        assert len(suppliers) == 2
