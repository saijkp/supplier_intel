"""
tests/test_uk_trade_gap.py

Tests for the UK trade-data/scoring fix:
  1. BaseNormalizer.infer_export_flags_from_markets (self-reported
     export-destination signal, shared across normalizers)
  2. AlibabaNormalizer wiring that signal in via 'main markets'
  3. SupplierScorer._export_score's rebalanced credit for self-reported
     vs confirmed export data
  4. scrapers.global_trade_scraper.GlobalTradeScraper (Volza-backed,
     mocked HTTP) and its pipeline wiring
"""

from __future__ import annotations

import httpx
import pytest

from normalizers.alibaba_normalizer import AlibabaNormalizer
from normalizers.base_normalizer import BaseNormalizer
from normalizers.trade_normalizer import TradeNormalizer
from scrapers.base_scraper import ScraperResult
from scrapers.global_trade_scraper import GlobalTradeScraper, TRADE_PROVIDERS
from verification.scorer import SupplierScorer
from storage.database import initialise_schema
from storage.repository import SupplierRepository
from pipeline.orchestrator import SupplierIntelligencePipeline


# ═════════════════════════════════════════════════════════════
# BaseNormalizer.infer_export_flags_from_markets
# ═════════════════════════════════════════════════════════════

class _ConcreteNormalizer(BaseNormalizer):
    def normalise(self, raw_data):
        return {"canonical_name": raw_data.get("name", "")}


class TestInferExportFlagsFromMarkets:

    def test_uk_market_detected(self):
        flags = _ConcreteNormalizer.infer_export_flags_from_markets(["United Kingdom", "Domestic Market"])
        assert flags["exports_to_uk"] is True
        assert "exports_to_eu" not in flags

    def test_eu_market_detected(self):
        flags = _ConcreteNormalizer.infer_export_flags_from_markets(["Western Europe"])
        assert flags["exports_to_eu"] is True

    def test_us_market_detected(self):
        flags = _ConcreteNormalizer.infer_export_flags_from_markets(["North America"])
        assert flags["exports_to_us"] is True

    def test_multiple_markets_all_detected(self):
        flags = _ConcreteNormalizer.infer_export_flags_from_markets(
            ["United Kingdom", "Western Europe", "North America", "Southeast Asia"]
        )
        assert flags["exports_to_uk"] is True
        assert flags["exports_to_eu"] is True
        assert flags["exports_to_us"] is True

    def test_active_export_countries_always_recorded(self):
        flags = _ConcreteNormalizer.infer_export_flags_from_markets(["Southeast Asia"])
        assert flags["active_export_countries"] == ["Southeast Asia"]
        assert "exports_to_uk" not in flags

    def test_empty_input_returns_empty_dict(self):
        assert _ConcreteNormalizer.infer_export_flags_from_markets([]) == {}
        assert _ConcreteNormalizer.infer_export_flags_from_markets(None) == {}

    def test_case_insensitive(self):
        flags = _ConcreteNormalizer.infer_export_flags_from_markets(["united kingdom"])
        assert flags["exports_to_uk"] is True


# ═════════════════════════════════════════════════════════════
# AlibabaNormalizer: main_markets wiring
# ═════════════════════════════════════════════════════════════

class TestAlibabaNormalizerMainMarkets:

    def test_main_markets_sets_export_flags(self):
        normalizer = AlibabaNormalizer()
        result = normalizer.normalise({
            "companyName": "Foo Co",
            "mainMarkets": ["United Kingdom", "Western Europe"],
        })
        assert result["exports_to_uk"] is True
        assert result["exports_to_eu"] is True
        assert result["active_export_countries"] == ["United Kingdom", "Western Europe"]

    def test_no_main_markets_omits_flags(self):
        normalizer = AlibabaNormalizer()
        result = normalizer.normalise({"companyName": "Foo Co"})
        assert "exports_to_uk" not in result
        assert "active_export_countries" not in result

    def test_main_markets_alias_variants(self):
        normalizer = AlibabaNormalizer()
        result = normalizer.normalise({"companyName": "Foo Co", "exportMarkets": "United Kingdom, North America"})
        assert result["exports_to_uk"] is True
        assert result["exports_to_us"] is True

    def test_output_compatible_with_repository(self, tmp_path):
        db_path = tmp_path / "test.db"
        initialise_schema(db_path)
        repo = SupplierRepository(db_path=db_path)
        normalizer = AlibabaNormalizer()

        supplier_data = normalizer.normalise({
            "companyName": "Foo Co", "mainMarkets": ["United Kingdom"],
        })
        supplier_id = repo.create_golden_record(supplier_data)
        supplier = repo.get_supplier(supplier_id)
        assert supplier["exports_to_uk"] == 1


# ═════════════════════════════════════════════════════════════
# Scorer rebalancing
# ═════════════════════════════════════════════════════════════

class TestExportScoreRebalancing:

    def test_confirmed_uk_shipments_score_higher_than_self_reported(self):
        scorer = SupplierScorer()
        confirmed = scorer._export_score({"confirmed_shipments_uk": 1})
        self_reported = scorer._export_score({"exports_to_uk": True})
        assert confirmed > self_reported
        assert self_reported > 0  # not crushed to zero

    def test_self_reported_alone_is_not_zero(self):
        scorer = SupplierScorer()
        score = scorer._export_score({"exports_to_uk": True})
        assert score == 15

    def test_confirmed_and_self_reported_do_not_stack(self):
        """A supplier with BOTH confirmed_shipments_uk>0 and
        exports_to_uk=True should only get the confirmed credit, not
        both added together."""
        scorer = SupplierScorer()
        both = scorer._export_score({"confirmed_shipments_uk": 1, "exports_to_uk": True})
        confirmed_only = scorer._export_score({"confirmed_shipments_uk": 1})
        assert both == confirmed_only

    def test_eu_and_us_self_reported_credit(self):
        scorer = SupplierScorer()
        assert scorer._export_score({"exports_to_eu": True}) == 8
        assert scorer._export_score({"exports_to_us": True}) == 5

    def test_no_data_at_all_still_zero(self):
        scorer = SupplierScorer()
        assert scorer._export_score({}) == 0

    def test_ceiling_still_100_with_everything_confirmed(self):
        scorer = SupplierScorer()
        from datetime import date
        score = scorer._export_score({
            "confirmed_shipments_uk": 15, "confirmed_shipments_eu": 1, "confirmed_shipments_us": 1,
            "last_shipment_date": date.today().isoformat(),
        })
        assert score == 100

    def test_self_reported_all_three_markets(self):
        scorer = SupplierScorer()
        score = scorer._export_score({"exports_to_uk": True, "exports_to_eu": True, "exports_to_us": True})
        assert score == 15 + 8 + 5


# ═════════════════════════════════════════════════════════════
# GlobalTradeScraper — fake httpx client
# ═════════════════════════════════════════════════════════════

def _volza_card_html(shipper, consignee, country="", hs_code="8539"):
    country_html = f'<div class="destination-country">{country}</div>' if country else ""
    return f"""
    <div class="shipment-row">
        <div class="exporter-name">{shipper}</div>
        <div class="importer-name">{consignee}</div>
        {country_html}
        <div class="shipment-date">2026-06-01</div>
        <div class="hs-code">{hs_code}</div>
        <div class="product-description">LED marker lights</div>
        <div class="origin-port">Shenzhen</div>
        <div class="destination-port">Felixstowe</div>
        <div class="weight">1,000 kg</div>
        <div class="value">$9,500.00</div>
        <a class="exporter-link" href="/company/{shipper.replace(' ', '-')}">Profile</a>
    </div>
    """


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class FakeVolzaClient:
    def __init__(self, pages):
        self._pages = pages
        self.requested_urls = []

    def get(self, url):
        self.requested_urls.append(url)
        page_num = int(url.split("page=")[-1])
        if page_num <= len(self._pages):
            return FakeResponse(self._pages[page_num - 1])
        return FakeResponse("<html><body>no results</body></html>")


class TestGlobalTradeScraper:

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown trade provider"):
            GlobalTradeScraper(provider="not_a_real_provider")

    def test_source_name_matches_provider(self):
        scraper = GlobalTradeScraper(provider="volza", http_client=FakeVolzaClient([]), enable_delays=False)
        assert scraper.source_name == "volza"

    def test_scrape_defaults_consignee_country_when_page_omits_it(self):
        page1 = f"<html><body>{_volza_card_html('Shenzhen LED Masters', 'Ifor Williams Trailers')}</body></html>"
        scraper = GlobalTradeScraper(provider="volza", http_client=FakeVolzaClient([page1]), enable_delays=False)

        results = scraper.scrape("LED marker light", max_pages=2)
        assert len(results) == 1
        assert results[0].raw_data["consignee_country"] == "United Kingdom"

    def test_scrape_prefers_explicit_country_over_default(self):
        page1 = f"<html><body>{_volza_card_html('Shenzhen LED Masters', 'Some German Buyer', country='Germany')}</body></html>"
        scraper = GlobalTradeScraper(provider="volza", http_client=FakeVolzaClient([page1]), enable_delays=False)

        results = scraper.scrape("LED marker light")
        assert results[0].raw_data["consignee_country"] == "Germany"

    def test_scrape_stops_at_empty_page(self):
        page1 = f"<html><body>{_volza_card_html('Shipper A', 'Buyer A')}</body></html>"
        scraper = GlobalTradeScraper(provider="volza", http_client=FakeVolzaClient([page1]), enable_delays=False)
        results = scraper.scrape("widgets", max_pages=5)
        assert len(results) == 1

    def test_scrape_returns_error_result_on_first_page_failure(self):
        class FailingClient:
            def get(self, url):
                raise httpx.ConnectError("connection refused")

        scraper = GlobalTradeScraper(provider="volza", http_client=FailingClient(), enable_delays=False)
        results = scraper.scrape("widgets")
        assert len(results) == 1
        assert results[0].success is False

    def test_trade_providers_config_present(self):
        assert "volza" in TRADE_PROVIDERS
        assert TRADE_PROVIDERS["volza"]["default_consignee_country"] == "United Kingdom"


# ═════════════════════════════════════════════════════════════
# Pipeline integration: volza produces UK shipment records
# ═════════════════════════════════════════════════════════════

@pytest.fixture()
def repo(tmp_path):
    db_path = tmp_path / "test.db"
    initialise_schema(db_path)
    return SupplierRepository(db_path=db_path)


class TestShipmentRecordsMigrationV4:

    def test_v4_migration_preserves_existing_shipment_data_and_drops_check(self, tmp_path):
        """Build a pre-v4 database (shipment_records.source CHECK'd to
        panjiva/importyeti only), insert a row under the old constraint,
        then confirm upgrading preserves that row AND lifts the
        constraint so 'volza' rows can be inserted afterwards."""
        import sqlite3
        from storage.database import SCHEMA_SQL, connection_scope, initialise_schema, get_schema_version

        # Reconstruct the pre-v4 schema: same as current SCHEMA_SQL but
        # with the original restrictive CHECK constraint restored.
        legacy_schema_sql = SCHEMA_SQL.replace(
            "source              TEXT NOT NULL,  -- see config.settings.VALID_SHIPMENT_SOURCES for the reference list "
            "(not DB-enforced — new trade sources are added over time; see migration v4)",
            "source              TEXT NOT NULL CHECK (source IN ('panjiva', 'importyeti')),",
        )

        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(legacy_schema_sql)
        conn.execute("INSERT INTO schema_migrations (version, description) VALUES (1, 'legacy v1')")
        conn.execute("INSERT INTO schema_migrations (version, description) VALUES (2, 'legacy v2')")
        conn.execute("INSERT INTO schema_migrations (version, description) VALUES (3, 'legacy v3')")
        conn.execute("INSERT INTO suppliers (canonical_name) VALUES ('Foo Co')")
        supplier_id = conn.execute("SELECT id FROM suppliers WHERE canonical_name = 'Foo Co'").fetchone()[0]
        conn.execute(
            "INSERT INTO shipment_records (supplier_id, source, consignee_name) VALUES (?, 'importyeti', 'Old Buyer Co')",
            (supplier_id,),
        )
        conn.commit()

        # Confirm the old constraint is genuinely active before upgrading
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO shipment_records (supplier_id, source) VALUES (?, 'volza')",
                (supplier_id,),
            )
        conn.close()

        initialise_schema(db_path)

        with connection_scope(db_path) as conn:
            rows = conn.execute("SELECT * FROM shipment_records").fetchall()
            assert len(rows) == 1
            assert rows[0]["consignee_name"] == "Old Buyer Co"

            # Constraint should now be lifted
            conn.execute(
                "INSERT INTO shipment_records (supplier_id, source) VALUES (?, 'volza')",
                (supplier_id,),
            )

        assert get_schema_version(db_path) == 10  # latest migration at time of writing; bumps as new migrations are added (see storage/database.py MIGRATIONS)


class TestPipelineVolzaIntegration:

    def test_volza_shipment_sets_confirmed_uk_shipments(self, repo):
        page1 = f"<html><body>{_volza_card_html('Shenzhen LED Masters', 'Ifor Williams Trailers')}</body></html>"
        scrapers = {"volza": GlobalTradeScraper(provider="volza", http_client=FakeVolzaClient([page1]), enable_delays=False)}
        normalizers = {"volza": TradeNormalizer()}

        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers=scrapers, normalizers=normalizers)
        stats = pipeline.run("LED marker light", sources=["volza"])

        assert stats["created"] == 1
        assert stats["shipment_records"] == 1

        supplier = repo.list_suppliers(limit=1)[0]
        assert supplier["exports_to_uk"] == 1
        assert supplier["confirmed_shipments_uk"] == 1
        # This is the whole point: composite score should reflect a
        # confirmed UK shipment, not the old structurally-unreachable 0.
        assert supplier["export_score"] >= 40
