"""
tests/test_phase1.py

Phase 1 test suite: schema creation + repository CRUD operations.
Every test uses a fresh temp-file SQLite DB (via the `db_path` fixture)
so tests never touch the real data/suppliers.db and can run in any order.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from storage.database import (
    initialise_schema,
    get_schema_version,
    table_counts,
    connection_scope,
    SCHEMA_VERSION,
)
from storage.repository import SupplierRepository


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────

@pytest.fixture()
def db_path(tmp_path):
    """A fresh, initialised database file for each test."""
    path = tmp_path / "test_suppliers.db"
    initialise_schema(path)
    return path


@pytest.fixture()
def repo(db_path):
    return SupplierRepository(db_path=db_path)


# ─────────────────────────────────────────────────────────────
# Schema tests
# ─────────────────────────────────────────────────────────────

class TestSchemaCreation:

    def test_initialise_schema_creates_all_tables(self, db_path):
        expected_tables = {
            "suppliers", "raw_source_data", "shipment_records",
            "dedup_candidates", "search_log", "schema_migrations",
        }
        with connection_scope(db_path) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        actual_tables = {row["name"] for row in rows}
        assert expected_tables.issubset(actual_tables)

    def test_schema_version_recorded(self, db_path):
        assert get_schema_version(db_path) == SCHEMA_VERSION

    def test_initialise_schema_is_idempotent(self, db_path):
        # Calling it again should not raise or duplicate migration rows
        initialise_schema(db_path)
        initialise_schema(db_path)
        with connection_scope(db_path) as conn:
            rows = conn.execute(
                "SELECT COUNT(*) AS n FROM schema_migrations WHERE version = ?",
                (SCHEMA_VERSION,),
            ).fetchone()
        assert rows["n"] == 1

    def test_table_counts_all_zero_on_fresh_db(self, db_path):
        counts = table_counts(db_path)
        assert counts["suppliers"] == 0
        assert counts["raw_source_data"] == 0
        assert counts["shipment_records"] == 0
        assert counts["dedup_candidates"] == 0
        assert counts["search_log"] == 0
        assert counts["schema_migrations"] == 25  # v1-v10 as noted previously + v11 AI discovery/collection/verification platform + v12 Sourcing Agent + v13 Apollo contacts + v14 Procurement Decision Engine foundation + v15 Procurement Decision Engine Phase 3 + v16 scoring engine rewrite + v17 self_asserted_score + v18 CSV batch upload (batch_upload_rows/field_provenance) + v19 batch_upload_rows.name_extraction_note + v20 supplier_phone_numbers/contact_source_pages + v21 factory_location/candidate_facility_photo_urls + v22 supplier_reputation_snippets + v23 companies_house verification + v24 catalogue-depth evidence + v25 reverse-image-search evidence import

    def test_v11_columns_and_tables_exist_on_fresh_db(self, db_path):
        with connection_scope(db_path) as conn:
            supplier_cols = {row["name"] for row in conn.execute("PRAGMA table_info(suppliers)").fetchall()}
            table_names = {
                row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
        for col in (
            "ai_confidence_score", "ai_confidence_assessed_at", "ai_summary", "ai_strengths",
            "ai_risks", "ai_suitable_customer_types", "ai_verification_model",
            "discovery_source", "collection_last_run_at", "collection_status",
        ):
            assert col in supplier_cols
        for table in ("verification_history", "supplier_change_log", "collection_runs", "discovery_runs"):
            assert table in table_names

    def test_v11_migration_upgrades_a_real_pre_v11_database(self, tmp_path):
        """The production Railway DB is at v10 right now -- this proves
        initialise_schema() safely ALTERs an existing suppliers table
        (not just a fresh CREATE) to add the v11 columns, which
        test_v11_columns_and_tables_exist_on_fresh_db alone can't prove
        since a fresh DB gets everything from SCHEMA_SQL directly.
        Uses a minimal standalone old-shape suppliers table (not derived
        from the current SCHEMA_SQL) -- SQLite's ALTER TABLE DROP COLUMN
        chokes on the inline SQL comments in the real schema string, and
        the exact historical column set doesn't matter for what this
        test proves (that missing columns get added, not what else is
        already there)."""
        from storage.database import MIGRATIONS

        old_db_path = tmp_path / "pre_v11.db"
        conn = sqlite3.connect(str(old_db_path))
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE suppliers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_name TEXT NOT NULL,
                domain TEXT UNIQUE,
                country TEXT,
                uscc TEXT,
                is_manufacturer BOOLEAN,
                e_mark_certified BOOLEAN NOT NULL DEFAULT 0,
                composite_score INTEGER NOT NULL DEFAULT 0,
                recommendation TEXT NOT NULL DEFAULT 'unverified',
                last_verified TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
            "applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, description TEXT)"
        )
        remaining_cols = {row["name"] for row in conn.execute("PRAGMA table_info(suppliers)").fetchall()}
        assert "ai_confidence_score" not in remaining_cols
        for version in range(1, 11):
            desc = MIGRATIONS.get(version, {}).get("description", f"migration {version}")
            conn.execute("INSERT INTO schema_migrations (version, description) VALUES (?, ?)", (version, desc))
        conn.commit()
        conn.close()

        initialise_schema(old_db_path)

        assert get_schema_version(old_db_path) == SCHEMA_VERSION
        with connection_scope(old_db_path) as conn:
            supplier_cols = {row["name"] for row in conn.execute("PRAGMA table_info(suppliers)").fetchall()}
            table_names = {
                row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
        assert "ai_confidence_score" in supplier_cols
        assert "collection_status" in supplier_cols
        for table in ("verification_history", "supplier_change_log", "collection_runs", "discovery_runs"):
            assert table in table_names

    def test_foreign_keys_enforced(self, db_path):
        with connection_scope(db_path) as conn:
            fk_status = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk_status == 1

    def test_suppliers_uscc_unique_constraint(self, db_path):
        with connection_scope(db_path) as conn:
            conn.execute(
                "INSERT INTO suppliers (canonical_name, uscc) VALUES (?, ?)",
                ("Foo Trading Co", "91440101MA5ABCDE12"),
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO suppliers (canonical_name, uscc) VALUES (?, ?)",
                    ("Bar Trading Co", "91440101MA5ABCDE12"),
                )

    def test_suppliers_domain_unique_constraint(self, db_path):
        with connection_scope(db_path) as conn:
            conn.execute(
                "INSERT INTO suppliers (canonical_name, domain) VALUES (?, ?)",
                ("Foo Trading Co", "footrading.com"),
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO suppliers (canonical_name, domain) VALUES (?, ?)",
                    ("Bar Trading Co", "footrading.com"),
                )

    def test_recommendation_check_constraint(self, db_path):
        with connection_scope(db_path) as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO suppliers (canonical_name, recommendation) VALUES (?, ?)",
                    ("Foo Trading Co", "not_a_valid_value"),
                )


# ─────────────────────────────────────────────────────────────
# Repository: raw_source_data
# ─────────────────────────────────────────────────────────────

class TestRawSourceData:

    def test_save_and_get_raw(self, repo):
        raw_id = repo.save_raw(
            source="alibaba",
            source_id="ALI-12345",
            raw_data={"company_name": "Guangzhou ABC Electronics", "years": 5},
        )
        assert raw_id > 0

        raw = repo.get_raw(raw_id)
        assert raw["source"] == "alibaba"
        assert raw["source_id"] == "ALI-12345"
        assert raw["processing_status"] == "pending"
        assert raw["raw_json"]["company_name"] == "Guangzhou ABC Electronics"

    def test_mark_raw_processed(self, repo):
        raw_id = repo.save_raw(source="hktdc", raw_data={"company_name": "Test Co"})
        supplier_id = repo.create_golden_record({"canonical_name": "Test Co"})
        repo.mark_raw_processed(raw_id, golden_record_id=supplier_id, status="processed")

        raw = repo.get_raw(raw_id)
        assert raw["processing_status"] == "processed"
        assert raw["golden_record_id"] == supplier_id

    def test_get_pending_raw_filters_by_status(self, repo):
        raw_id_1 = repo.save_raw(source="alibaba", raw_data={"a": 1})
        raw_id_2 = repo.save_raw(source="alibaba", raw_data={"a": 2})
        repo.mark_raw_processed(raw_id_1, status="processed")

        pending = repo.get_pending_raw()
        pending_ids = {r["id"] for r in pending}
        assert raw_id_2 in pending_ids
        assert raw_id_1 not in pending_ids

    def test_get_pending_raw_filters_by_source(self, repo):
        repo.save_raw(source="alibaba", raw_data={"a": 1})
        repo.save_raw(source="hktdc", raw_data={"a": 2})

        alibaba_pending = repo.get_pending_raw(source="alibaba")
        assert all(r["source"] == "alibaba" for r in alibaba_pending)
        assert len(alibaba_pending) == 1


# ─────────────────────────────────────────────────────────────
# Repository: suppliers (create / find / merge)
# ─────────────────────────────────────────────────────────────

class TestSupplierGoldenRecords:

    def test_create_golden_record_requires_canonical_name(self, repo):
        with pytest.raises(ValueError):
            repo.create_golden_record({"country": "China"})

    def test_create_and_get_golden_record(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Guangzhou ABC Electronics Co Ltd",
            "country": "China",
            "city": "Guangzhou",
            "domain": "abc-electronics.com",
            "uscc": "91440101MA5ABCDE12",
            "primary_categories": ["LED Lighting", "Fasteners"],
            "is_manufacturer": True,
            "e_mark_certified": True,
        })
        assert supplier_id > 0

        supplier = repo.get_supplier(supplier_id)
        assert supplier["canonical_name"] == "Guangzhou ABC Electronics Co Ltd"
        assert supplier["country"] == "China"
        assert supplier["primary_categories"] == ["LED Lighting", "Fasteners"]
        assert supplier["is_manufacturer"] == 1
        assert supplier["source_count"] == 1
        assert supplier["recommendation"] == "unverified"  # DB default

    def test_find_by_uscc(self, repo):
        repo.create_golden_record({
            "canonical_name": "Foo Co", "uscc": "91440101MA5ABCDE12",
        })
        found = repo.find_by_uscc("91440101MA5ABCDE12")
        assert found is not None
        assert found["canonical_name"] == "Foo Co"

        assert repo.find_by_uscc("nonexistent-uscc") is None
        assert repo.find_by_uscc("") is None

    def test_find_by_domain(self, repo):
        repo.create_golden_record({
            "canonical_name": "Foo Co", "domain": "foo.com",
        })
        found = repo.find_by_domain("foo.com")
        assert found is not None
        assert found["canonical_name"] == "Foo Co"
        assert repo.find_by_domain("nope.com") is None

    def test_find_by_country(self, repo):
        repo.create_golden_record({"canonical_name": "China Co 1", "country": "China"})
        repo.create_golden_record({"canonical_name": "China Co 2", "country": "China"})
        repo.create_golden_record({"canonical_name": "India Co", "country": "India"})

        china_suppliers = repo.find_by_country("China")
        assert len(china_suppliers) == 2
        names = {s["canonical_name"] for s in china_suppliers}
        assert names == {"China Co 1", "China Co 2"}

    def test_find_by_country_empty_returns_all(self, repo):
        repo.create_golden_record({"canonical_name": "A", "country": "China"})
        repo.create_golden_record({"canonical_name": "B", "country": "India"})
        assert len(repo.find_by_country("")) == 2

    def test_merge_into_golden_fills_empty_scalar_fields(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Foo Co", "country": "China",
        })
        repo.merge_into_golden(supplier_id, {
            "canonical_name": "Foo Co",
            "city": "Shenzhen",
            "primary_email": "sales@foo.com",
        })
        supplier = repo.get_supplier(supplier_id)
        assert supplier["city"] == "Shenzhen"
        assert supplier["primary_email"] == "sales@foo.com"
        assert supplier["source_count"] == 2

    def test_merge_into_golden_does_not_clobber_existing_value(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Foo Co", "city": "Guangzhou",
        })
        repo.merge_into_golden(supplier_id, {
            "canonical_name": "Foo Co", "city": "Shenzhen",
        })
        supplier = repo.get_supplier(supplier_id)
        # Existing value wins — new source doesn't overwrite confirmed data
        assert supplier["city"] == "Guangzhou"

    def test_merge_into_golden_unions_json_array_fields(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Foo Co",
            "primary_categories": ["LED Lighting"],
        })
        repo.merge_into_golden(supplier_id, {
            "canonical_name": "Foo Co",
            "primary_categories": ["Fasteners", "LED Lighting"],
        })
        supplier = repo.get_supplier(supplier_id)
        assert set(supplier["primary_categories"]) == {"LED Lighting", "Fasteners"}

    def test_merge_into_golden_raises_on_missing_supplier(self, repo):
        with pytest.raises(ValueError):
            repo.merge_into_golden(999999, {"canonical_name": "Ghost Co"})

    def test_update_supplier_fields(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Foo Co"})
        repo.update_supplier_fields(supplier_id, {"flagged": True, "flag_reason": "Duplicate ABN"})
        supplier = repo.get_supplier(supplier_id)
        assert supplier["flagged"] == 1
        assert supplier["flag_reason"] == "Duplicate ABN"

    def test_update_supplier_fields_with_history_records_a_real_diff(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Foo Co", "primary_phone": "+44 20 1234 5678"})
        changes = repo.update_supplier_fields_with_history(
            supplier_id, {"primary_phone": "+44 20 9999 0000"},
            changed_by="verification_service", change_reason="reverify found a different number",
        )
        assert changes == [("primary_phone", "+44 20 1234 5678", "+44 20 9999 0000")]

        log = repo.get_supplier_change_log(supplier_id)
        assert len(log) == 1
        assert log[0]["field_name"] == "primary_phone"
        assert log[0]["old_value"] == "+44 20 1234 5678"
        assert log[0]["new_value"] == "+44 20 9999 0000"
        assert log[0]["changed_by"] == "verification_service"

    def test_update_supplier_fields_with_history_records_nothing_when_value_unchanged(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Foo Co", "primary_phone": "+44 20 1234 5678"})
        changes = repo.update_supplier_fields_with_history(
            supplier_id, {"primary_phone": "+44 20 1234 5678"}, changed_by="verification_service",
        )
        assert changes == []
        assert repo.get_supplier_change_log(supplier_id) == []

    def test_update_supplier_fields_with_history_raises_on_missing_supplier(self, repo):
        with pytest.raises(ValueError):
            repo.update_supplier_fields_with_history(999999, {"primary_phone": "x"}, changed_by="manual")

    def test_delete_supplier(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Foo Co"})
        repo.delete_supplier(supplier_id)
        assert repo.get_supplier(supplier_id) is None

    def test_list_suppliers_filters_by_recommendation(self, repo):
        id_a = repo.create_golden_record({"canonical_name": "A Co"})
        id_b = repo.create_golden_record({"canonical_name": "B Co"})
        repo.update_scores(id_a, {"composite_score": 80, "recommendation": "recommended"})
        repo.update_scores(id_b, {"composite_score": 20, "recommendation": "avoid"})

        recommended = repo.list_suppliers(recommendation="recommended")
        assert len(recommended) == 1
        assert recommended[0]["canonical_name"] == "A Co"

    def test_list_suppliers_filters_by_min_score(self, repo):
        id_a = repo.create_golden_record({"canonical_name": "A Co"})
        id_b = repo.create_golden_record({"canonical_name": "B Co"})
        repo.update_scores(id_a, {"composite_score": 90})
        repo.update_scores(id_b, {"composite_score": 10})

        high_scorers = repo.list_suppliers(min_composite_score=50)
        assert len(high_scorers) == 1
        assert high_scorers[0]["canonical_name"] == "A Co"

    def test_search_suppliers_by_keyword(self, repo):
        repo.create_golden_record({
            "canonical_name": "Guangzhou LED Masters Co",
            "product_keywords": ["LED marker light", "trailer light"],
        })
        repo.create_golden_record({
            "canonical_name": "Shenzhen Fastener Works",
            "product_keywords": ["hex bolt", "nut"],
        })

        results = repo.search_suppliers("LED")
        names = {r["canonical_name"] for r in results}
        assert "Guangzhou LED Masters Co" in names
        assert "Shenzhen Fastener Works" not in names

    def test_search_suppliers_excludes_flagged_records(self, repo):
        """A human-flagged supplier (e.g. ruled out as a broker/network,
        not a single factory) must never resurface here just because a
        later keyword search happens to match it again."""
        matching = repo.create_golden_record({
            "canonical_name": "Guangzhou LED Masters Co",
            "product_keywords": ["LED marker light"],
        })
        flagged = repo.create_golden_record({
            "canonical_name": "Flagged LED Co",
            "product_keywords": ["LED marker light"],
        })
        repo.update_supplier_fields(flagged, {"flagged": True, "flag_reason": "broker, not a single factory"})

        results = repo.search_suppliers("LED")

        assert [r["id"] for r in results] == [matching]


# ─────────────────────────────────────────────────────────────
# Repository: verification
# ─────────────────────────────────────────────────────────────

class TestVerification:

    def test_get_unverified_chinese(self, repo):
        china_unverified = repo.create_golden_record({
            "canonical_name": "China Co", "country": "China",
            "uscc": "91440101MA5ABCDE12",
        })
        repo.create_golden_record({
            "canonical_name": "India Co", "country": "India",
            "uscc": "SOME-INDIA-ID",
        })
        china_verified = repo.create_golden_record({
            "canonical_name": "Verified China Co", "country": "China",
            "uscc": "91440101MA5FGHIJ34", "uscc_verified": True,
        })

        unverified = repo.get_unverified_chinese()
        ids = {s["id"] for s in unverified}
        assert china_unverified in ids
        assert china_verified not in ids
        assert all(s["country"] == "China" for s in unverified)

    def test_update_verification(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "China Co", "country": "China",
            "uscc": "91440101MA5ABCDE12",
        })
        repo.update_verification(supplier_id, {
            "uscc_verified": True,
            "year_established": 2015,
            "is_manufacturer": True,
        })
        supplier = repo.get_supplier(supplier_id)
        assert supplier["uscc_verified"] == 1
        assert supplier["uscc_verified_at"] is not None
        assert supplier["year_established"] == 2015


# ─────────────────────────────────────────────────────────────
# Repository: scoring
# ─────────────────────────────────────────────────────────────

class TestScoring:

    def test_get_unscored_returns_zero_score_suppliers(self, repo):
        unscored_id = repo.create_golden_record({"canonical_name": "New Co"})
        scored_id = repo.create_golden_record({"canonical_name": "Scored Co"})
        repo.update_scores(scored_id, {"composite_score": 75, "recommendation": "recommended"})

        unscored = repo.get_unscored()
        ids = {s["id"] for s in unscored}
        assert unscored_id in ids
        assert scored_id not in ids

    def test_update_scores_writes_all_score_fields(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Foo Co"})
        repo.update_scores(supplier_id, {
            "verification_score": 70,
            "export_score": 60,
            "platform_score": 80,
            "contact_score": 90,
            "composite_score": 72,
            "recommendation": "recommended",
        })
        supplier = repo.get_supplier(supplier_id)
        assert supplier["verification_score"] == 70
        assert supplier["export_score"] == 60
        assert supplier["platform_score"] == 80
        assert supplier["contact_score"] == 90
        assert supplier["composite_score"] == 72
        assert supplier["recommendation"] == "recommended"


# ─────────────────────────────────────────────────────────────
# Repository: dedup review queue
# ─────────────────────────────────────────────────────────────

class TestDedupReviewQueue:

    def test_add_and_get_pending_review_candidates(self, repo):
        id_a = repo.create_golden_record({"canonical_name": "Foo Co A"})
        id_b = repo.create_golden_record({"canonical_name": "Foo Co B"})
        repo.add_to_review_queue(id_a, id_b, match_score=0.81, match_signals={"name_score": 0.81})

        pending = repo.get_pending_review_candidates()
        assert len(pending) == 1
        assert pending[0]["supplier_id_a"] == id_a
        assert pending[0]["supplier_id_b"] == id_b
        assert pending[0]["status"] == "pending"
        assert pending[0]["match_signals"]["name_score"] == 0.81

    def test_resolve_review_candidate(self, repo):
        id_a = repo.create_golden_record({"canonical_name": "Foo Co A"})
        id_b = repo.create_golden_record({"canonical_name": "Foo Co B"})
        candidate_id = repo.add_to_review_queue(id_a, id_b, match_score=0.81)

        repo.resolve_review_candidate(candidate_id, "merged")

        pending = repo.get_pending_review_candidates()
        assert len(pending) == 0

    def test_resolve_review_candidate_rejects_invalid_status(self, repo):
        id_a = repo.create_golden_record({"canonical_name": "A"})
        id_b = repo.create_golden_record({"canonical_name": "B"})
        candidate_id = repo.add_to_review_queue(id_a, id_b, match_score=0.5)
        with pytest.raises(ValueError):
            repo.resolve_review_candidate(candidate_id, "not_a_status")


# ─────────────────────────────────────────────────────────────
# Repository: shipment records
# ─────────────────────────────────────────────────────────────

class TestShipmentRecords:

    def test_add_and_get_shipment_record(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Foo Co"})
        shipment_id = repo.add_shipment_record({
            "supplier_id": supplier_id,
            "source": "importyeti",
            "consignee_name": "Ifor Williams Trailers",
            "consignee_country": "UK",
            "shipment_date": "2026-05-01",
            "hs_code": "8539",
            "product_desc": "LED marker lights",
            "value_usd": 12500.0,
        })
        assert shipment_id > 0

        shipments = repo.get_shipments_for_supplier(supplier_id)
        assert len(shipments) == 1
        assert shipments[0]["consignee_name"] == "Ifor Williams Trailers"
        assert shipments[0]["value_usd"] == 12500.0

    def test_shipment_source_is_not_db_constrained(self, repo, db_path):
        """As of schema migration v4, shipment_records.source has no DB-level
        CHECK constraint — it was originally hardcoded to ('panjiva',
        'importyeti') and broke the first new trade source added ('volza').
        New trade-data providers are added over time (see
        scrapers.global_trade_scraper), so this is deliberately
        unconstrained at the DB level now, matching raw_source_data.source's
        design. config.settings.VALID_SHIPMENT_SOURCES is the reference
        list; it's documentation, not an enforced constraint."""
        supplier_id = repo.create_golden_record({"canonical_name": "Foo Co"})
        with connection_scope(db_path) as conn:
            conn.execute(
                "INSERT INTO shipment_records (supplier_id, source) VALUES (?, ?)",
                (supplier_id, "any_future_provider_name"),
            )
        shipments = repo.get_shipments_for_supplier(supplier_id)
        assert len(shipments) == 1
        assert shipments[0]["source"] == "any_future_provider_name"


# ─────────────────────────────────────────────────────────────
# Repository: search log
# ─────────────────────────────────────────────────────────────

class TestSearchLog:

    def test_log_and_retrieve_search(self, repo):
        repo.log_search(
            query="LED marker lights",
            category="LED Lighting",
            sources_used=["alibaba", "hktdc"],
            results_count=42,
        )
        recent = repo.recent_searches()
        assert len(recent) == 1
        assert recent[0]["query"] == "LED marker lights"
        assert recent[0]["sources_used"] == ["alibaba", "hktdc"]
        assert recent[0]["results_count"] == 42

    def test_recent_searches_ordered_newest_first(self, repo):
        repo.log_search(query="first search")
        time.sleep(0.01)
        repo.log_search(query="second search")

        recent = repo.recent_searches()
        assert recent[0]["query"] == "second search"
        assert recent[1]["query"] == "first search"


# ─────────────────────────────────────────────────────────────
# End-to-end smoke test: a mini pipeline run through the repository
# ─────────────────────────────────────────────────────────────

class TestEndToEndSmoke:

    def test_scrape_to_score_flow(self, repo):
        # 1. Raw data lands
        raw_id = repo.save_raw(
            source="alibaba",
            source_id="ALI-999",
            raw_data={"company_name": "Shenzhen LED Masters Co Ltd"},
        )

        # 2. Normalised into a golden record (no match found -> new record)
        assert repo.find_by_domain("ledmasters.com") is None
        supplier_id = repo.create_golden_record({
            "canonical_name": "Shenzhen LED Masters Co Ltd",
            "domain": "ledmasters.com",
            "country": "China",
            "city": "Shenzhen",
            "uscc": "91440300MA5XYZAB12",
            "is_manufacturer": True,
            "e_mark_certified": True,
            "primary_categories": ["LED Lighting"],
        })
        repo.mark_raw_processed(raw_id, golden_record_id=supplier_id)

        # 3. Verification
        repo.update_verification(supplier_id, {"uscc_verified": True})

        # 4. Scoring
        repo.update_scores(supplier_id, {
            "verification_score": 85,
            "export_score": 40,
            "platform_score": 60,
            "contact_score": 50,
            "composite_score": 63,
            "recommendation": "review",
        })

        # 5. Verify final state end-to-end
        supplier = repo.get_supplier(supplier_id)
        assert supplier["uscc_verified"] == 1
        assert supplier["composite_score"] == 63
        assert supplier["recommendation"] == "review"

        raw = repo.get_raw(raw_id)
        assert raw["processing_status"] == "processed"
        assert raw["golden_record_id"] == supplier_id

        counts = table_counts(repo.db_path)
        assert counts["suppliers"] == 1
        assert counts["raw_source_data"] == 1
