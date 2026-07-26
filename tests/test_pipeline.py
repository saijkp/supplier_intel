"""
tests/test_pipeline.py

Tests for the end-to-end orchestrator: scrape -> save raw -> normalise
-> dedup/merge -> verify -> score. Scrapers are replaced with fakes
(implementing the same BaseScraper contract) so no network or Apify
credentials are needed — the real normalizers, matcher, scorer, and
repository are exercised as-is.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import List

import pytest

from scrapers.base_scraper import BaseScraper, ScraperResult
from normalizers.alibaba_normalizer import AlibabaNormalizer
from normalizers.hktdc_normalizer import HKTDCNormalizer
from normalizers.trade_normalizer import TradeNormalizer
from pipeline.orchestrator import SupplierIntelligencePipeline
from storage.database import initialise_schema
from storage.repository import SupplierRepository
from verification.qichacha import QichachaVerifier


class FakeScraper(BaseScraper):
    """Returns a fixed list of ScraperResults regardless of query, or
    raises if `raise_error` is set — used to exercise the pipeline's
    fault-isolation without needing a real flaky network call."""

    def __init__(self, source_name: str, results: List[ScraperResult] = None, raise_error: bool = False):
        super().__init__(source_name, enable_delays=False)
        self._results = results or []
        self._raise_error = raise_error

    def scrape(self, query: str, **kwargs) -> List[ScraperResult]:
        if self._raise_error:
            raise RuntimeError("scraper exploded")
        return self._results


@pytest.fixture()
def repo(tmp_path):
    db_path = tmp_path / "test.db"
    initialise_schema(db_path)
    return SupplierRepository(db_path=db_path)


ALIBABA_RAW = {
    "supplierId": "SUP001",
    "companyName": "Shenzhen LED Masters Co Ltd",
    "companyUrl": "https://ledmasters.en.alibaba.com",
    "country": "China",
    "city": "Shenzhen",
    "yearsAsGoldSupplier": 5,
    "tradeAssurance": True,
    "rating": 4.7,
}

HKTDC_RAW_SAME_COMPANY = {
    "company_name": "LED Masters (Shenzhen)",
    "country": "China",
}

HKTDC_RAW_NO_NAME = {"country": "China"}


class TestPipelineFullRun:

    def test_scrape_dedup_and_score_across_two_sources(self, repo):
        scrapers = {
            "alibaba": FakeScraper("alibaba", [
                ScraperResult(source="alibaba", source_id="SUP001", raw_data=ALIBABA_RAW, success=True),
            ]),
            "hktdc": FakeScraper("hktdc", [
                ScraperResult(source="hktdc", source_id="", raw_data=HKTDC_RAW_SAME_COMPANY, success=True),
            ]),
        }
        normalizers = {"alibaba": AlibabaNormalizer(), "hktdc": HKTDCNormalizer()}

        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers=scrapers, normalizers=normalizers)
        stats = pipeline.run("LED marker light", sources=["alibaba", "hktdc"])

        assert stats["scraped"] == 2
        assert stats["normalised"] == 2
        assert stats["created"] == 1
        assert stats["merged"] == 1
        assert stats["scored"] == 1  # one golden record after merge

        suppliers = repo.list_suppliers(limit=10)
        assert len(suppliers) == 1
        supplier = suppliers[0]
        assert supplier["canonical_name"] == "Shenzhen LED Masters Co Ltd"
        assert supplier["source_count"] == 2
        assert supplier["composite_score"] is not None
        assert supplier["recommendation"] in ("recommended", "review", "unverified", "avoid")

    def test_unknown_source_is_skipped_without_error(self, repo):
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers={}, normalizers={})
        stats = pipeline.run("widgets", sources=["not_a_real_source"])
        assert stats["scraped"] == 0
        assert stats["scrape_errors"] == 0

    def test_scraper_exception_is_isolated(self, repo):
        scrapers = {"alibaba": FakeScraper("alibaba", raise_error=True)}
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers=scrapers, normalizers={"alibaba": AlibabaNormalizer()})

        stats = pipeline.run("widgets", sources=["alibaba"])
        assert stats["scrape_errors"] == 1
        assert stats["scraped"] == 0

    def test_scraper_error_result_is_counted_not_stored(self, repo):
        scrapers = {
            "alibaba": FakeScraper("alibaba", [
                ScraperResult(source="alibaba", source_id="", raw_data={}, success=False, error="actor crashed"),
            ]),
        }
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers=scrapers, normalizers={"alibaba": AlibabaNormalizer()})

        stats = pipeline.run("widgets", sources=["alibaba"])
        assert stats["scrape_errors"] == 1
        assert stats["scraped"] == 0
        assert repo.list_suppliers() == []

    def test_missing_canonical_name_is_skipped(self, repo):
        scrapers = {
            "hktdc": FakeScraper("hktdc", [
                ScraperResult(source="hktdc", source_id="", raw_data=HKTDC_RAW_NO_NAME, success=True),
            ]),
        }
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers=scrapers, normalizers={"hktdc": HKTDCNormalizer()})

        stats = pipeline.run("widgets", sources=["hktdc"])
        assert stats["scraped"] == 1
        assert stats["normalised"] == 1
        assert stats["skipped_no_name"] == 1
        assert stats["created"] == 0
        assert repo.list_suppliers() == []

    def test_missing_normalizer_leaves_raw_unprocessed(self, repo):
        scrapers = {
            "alibaba": FakeScraper("alibaba", [
                ScraperResult(source="alibaba", source_id="SUP001", raw_data=ALIBABA_RAW, success=True),
            ]),
        }
        # Deliberately no normalizer registered for 'alibaba'
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers=scrapers, normalizers={})
        stats = pipeline.run("widgets", sources=["alibaba"])

        assert stats["scraped"] == 1
        assert stats["normalised"] == 0
        pending = repo.get_pending_raw()
        assert len(pending) == 1

    def test_no_verification_flag_skips_stage(self, repo, monkeypatch):
        monkeypatch.setattr("verification.qichacha.QICHACHA_API_KEY", "fake")
        monkeypatch.setattr("verification.qichacha.QICHACHA_SECRET_KEY", "fake")
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers={}, normalizers={})
        stats = pipeline.run("widgets", sources=[], run_verification=False)
        assert stats["verified"] == 0

    def test_no_score_flag_skips_scoring(self, repo):
        scrapers = {
            "hktdc": FakeScraper("hktdc", [
                ScraperResult(source="hktdc", source_id="", raw_data=HKTDC_RAW_SAME_COMPANY, success=True),
            ]),
        }
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers=scrapers, normalizers={"hktdc": HKTDCNormalizer()})
        stats = pipeline.run("widgets", sources=["hktdc"], run_scoring=False)

        assert stats["created"] == 1
        assert stats["scored"] == 0
        supplier = repo.list_suppliers(limit=1)[0]
        assert supplier["composite_score"] == 0  # never scored


TRADE_RAW = {
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


class TestPipelineTradeSources:

    def test_importyeti_creates_shipment_record_linked_to_supplier(self, repo):
        scrapers = {
            "importyeti": FakeScraper("importyeti", [
                ScraperResult(source="importyeti", source_id="", raw_data=TRADE_RAW, success=True),
            ]),
        }
        normalizers = {"importyeti": TradeNormalizer()}
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers=scrapers, normalizers=normalizers)

        stats = pipeline.run("LED marker light", sources=["importyeti"])

        assert stats["created"] == 1
        assert stats["shipment_records"] == 1

        supplier = repo.list_suppliers(limit=1)[0]
        assert supplier["canonical_name"] == "Shenzhen LED Masters Co Ltd"
        assert supplier["exports_to_uk"] == 1

        shipments = repo.get_shipments_for_supplier(supplier["id"])
        assert len(shipments) == 1
        assert shipments[0]["value_usd"] == 12500.0
        assert shipments[0]["consignee_name"] == "Ifor Williams Trailers"


class TestPipelineVerificationStage:

    def test_verification_skipped_without_credentials(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Foo Co", "country": "China", "uscc": "91440101MA5ABCDE1M",
        })
        qichacha = QichachaVerifier(app_key=None, app_secret=None)
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers={}, normalizers={}, qichacha=qichacha)

        stats = pipeline.run_verification_only()
        assert stats["verified"] == 0
        assert repo.get_supplier(supplier_id)["uscc_verified"] == 0

    def test_verification_runs_with_injected_client(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Foo Co", "country": "China", "uscc": "91440101MA5ABCDE1M",
        })

        class FakeHttpClient:
            def get(self, url, params=None, headers=None):
                class R:
                    def raise_for_status(self):
                        pass

                    def json(self):
                        return {
                            "Status": "200",
                            "Result": {"Name": "Foo Co", "CreditCode": "91440101MA5ABCDE1M"},
                        }
                return R()

        qichacha = QichachaVerifier(app_key="k", app_secret="s", http_client=FakeHttpClient())
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers={}, normalizers={}, qichacha=qichacha)

        stats = pipeline.run_verification_only()
        assert stats["verified"] == 1
        assert repo.get_supplier(supplier_id)["uscc_verified"] == 1

    def test_verification_failure_for_one_supplier_does_not_abort_others(self, repo):
        ok_id = repo.create_golden_record({
            "canonical_name": "OK Co", "country": "China", "uscc": "91440101MA5ABCDE1M",
        })

        class FlakyClient:
            def get(self, url, params=None, headers=None):
                raise ConnectionError("network blip")

        qichacha = QichachaVerifier(app_key="k", app_secret="s", http_client=FlakyClient())
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers={}, normalizers={}, qichacha=qichacha)

        stats = pipeline.run_verification_only()  # should not raise
        assert stats["verified"] == 0
        assert repo.get_supplier(ok_id)["uscc_verified"] == 0


class TestPipelineScoringStage:

    def test_run_scoring_only_scores_unscored_suppliers(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Foo Co", "confirmed_shipments_uk": 5})
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers={}, normalizers={})

        stats = pipeline.run_scoring_only()
        assert stats["scored"] == 1
        assert repo.get_supplier(supplier_id)["composite_score"] > 0


class TestPipelineCertificates:

    def test_check_certificates_flags_expired_and_malformed(self, repo):
        expired_id = repo.create_golden_record({
            "canonical_name": "Expired Co", "iso_9001": True,
            "iso_9001_expiry": (date.today() - timedelta(days=5)).isoformat(),
        })
        repo.create_golden_record({
            "canonical_name": "Valid Co", "iso_9001": True,
            "iso_9001_expiry": (date.today() + timedelta(days=800)).isoformat(),
        })
        bad_e_mark_id = repo.create_golden_record({
            "canonical_name": "Bad Mark Co", "e_mark_certified": True,
            "e_mark_numbers": ["not-a-real-mark"],
        })

        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers={}, normalizers={})
        result = pipeline.check_certificates()

        recheck_ids = {s["id"] for s in result["iso_9001_needing_recheck"]}
        malformed_ids = {s["id"] for s in result["malformed_e_mark"]}

        assert expired_id in recheck_ids
        assert bad_e_mark_id in malformed_ids
