"""
tests/test_manufacturer_verification.py

Tests for the manufacturer verification build: the signal-assessment
functions, ManufacturerVerifier's aggregation, FactoryPhotoVerifier
(mocked OpenAI client), the v1->v2 schema migration, Qichacha's
business_scope/registered_capital_rmb parsing, and the pipeline's new
manufacturer-assessment stage.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from verification.manufacturer_verifier import (
    ManufacturerVerifier,
    assess_business_scope,
    assess_certifications,
    assess_registered_capital,
    assess_tenure_consistency,
)
from verification.factory_photo_verifier import FactoryPhotoVerifier
from verification.qichacha import QichachaVerifier
from storage.database import initialise_schema, get_schema_version, connection_scope, MIGRATIONS, SCHEMA_VERSION
from storage.repository import SupplierRepository
from normalizers.alibaba_normalizer import AlibabaNormalizer
from pipeline.orchestrator import SupplierIntelligencePipeline


# ═════════════════════════════════════════════════════════════
# Individual signal assessors
# ═════════════════════════════════════════════════════════════

class TestAssessBusinessScope:

    def test_manufacturing_only_scope(self):
        verdict, explanation = assess_business_scope("生产、加工LED车灯及配件")
        assert verdict is True
        assert "manufacturing" in explanation.lower()

    def test_trading_only_scope(self):
        verdict, explanation = assess_business_scope("批发、零售汽车配件；货物进出口")
        assert verdict is False
        assert "trading" in explanation.lower() or "sales" in explanation.lower()

    def test_mixed_scope_leans_manufacturer(self):
        verdict, _ = assess_business_scope("生产LED车灯；批发零售汽车配件")
        assert verdict is True

    def test_english_manufacturing_scope(self):
        verdict, _ = assess_business_scope("Manufacturing of automotive lighting components")
        assert verdict is True

    def test_no_scope_text(self):
        verdict, explanation = assess_business_scope(None)
        assert verdict is None
        assert "no registered business scope" in explanation.lower()

    def test_unrecognised_scope_text(self):
        verdict, _ = assess_business_scope("General consulting services")
        assert verdict is None


class TestAssessRegisteredCapital:

    def test_low_capital_is_red_flag(self):
        verdict, explanation = assess_registered_capital(100_000)
        assert verdict is False
        assert "unusually low" in explanation.lower()

    def test_high_capital_supports_manufacturer(self):
        verdict, explanation = assess_registered_capital(5_000_000)
        assert verdict is True
        assert "consistent" in explanation.lower()

    def test_none_is_inconclusive(self):
        verdict, explanation = assess_registered_capital(None)
        assert verdict is None

    def test_boundary_value(self):
        # Exactly at threshold should count as sufficient (>= not >)
        verdict, _ = assess_registered_capital(500_000)
        assert verdict is True


class TestAssessTenureConsistency:

    def test_consistent_tenure(self):
        this_year = date.today().year
        verdict, explanation = assess_tenure_consistency(
            alibaba_years=5, year_established=this_year - 5,
            today=date(this_year, 6, 1),
        )
        assert verdict is True

    def test_mismatched_tenure(self):
        this_year = date.today().year
        verdict, explanation = assess_tenure_consistency(
            alibaba_years=8, year_established=this_year - 1,
            today=date(this_year, 6, 1),
        )
        assert verdict is False
        assert "mismatch" in explanation.lower()

    def test_missing_data_is_inconclusive(self):
        assert assess_tenure_consistency(None, 2015)[0] is None
        assert assess_tenure_consistency(5, None)[0] is None
        assert assess_tenure_consistency(None, None)[0] is None


class TestAssessCertifications:

    def test_holds_relevant_certs(self):
        verdict, explanation = assess_certifications({"iso_9001": True, "e_mark_certified": True})
        assert verdict is True
        assert "ISO 9001" in explanation
        assert "E-mark" in explanation

    def test_holds_no_certs(self):
        verdict, _ = assess_certifications({})
        assert verdict is None

    def test_sqlite_integer_booleans_handled(self):
        # Repository reads return 0/1 ints, not True/False
        verdict, _ = assess_certifications({"iso_9001": 1})
        assert verdict is True
        verdict, _ = assess_certifications({"iso_9001": 0})
        assert verdict is None


# ═════════════════════════════════════════════════════════════
# ManufacturerVerifier aggregation
# ═════════════════════════════════════════════════════════════

class TestManufacturerVerifier:

    def test_strong_manufacturer_signals_yield_high_confidence(self):
        verifier = ManufacturerVerifier()
        this_year = date.today().year
        supplier = {
            "business_scope": "生产、制造LED车灯",
            "registered_capital_rmb": 5_000_000,
            "alibaba_years": 5,
            "year_established": this_year - 5,
            "iso_9001": True,
            "e_mark_certified": True,
        }
        result = verifier.assess(supplier)

        assert result["is_manufacturer"] is True
        assert result["manufacturer_confidence"] == 100
        assert not any(s.startswith("RED FLAG") for s in result["manufacturer_signals"])

    def test_strong_trader_signals_yield_low_confidence(self):
        verifier = ManufacturerVerifier()
        supplier = {
            "business_scope": "批发、零售、货物进出口贸易",
            "registered_capital_rmb": 50_000,
        }
        result = verifier.assess(supplier)

        assert result["is_manufacturer"] is False
        assert result["manufacturer_confidence"] <= 30
        assert any(s.startswith("RED FLAG") for s in result["manufacturer_signals"])

    def test_no_data_is_inconclusive_with_neutral_score(self):
        verifier = ManufacturerVerifier()
        result = verifier.assess({})
        assert result["is_manufacturer"] is None
        assert result["manufacturer_confidence"] == 50
        assert result["manufacturer_signals"] == []
        assert "insufficient data" in result["summary"].lower()

    def test_mixed_signals_land_in_middle_band(self):
        verifier = ManufacturerVerifier()
        # Manufacturing scope (strong +40) but tenure mismatch (-15) and
        # low capital (-20): 50 + 40 - 20 - 15 = 55, "review" territory.
        supplier = {
            "business_scope": "生产LED车灯",
            "registered_capital_rmb": 50_000,
            "alibaba_years": 10,
            "year_established": date.today().year - 1,
        }
        result = verifier.assess(supplier)
        assert 30 < result["manufacturer_confidence"] < 70

    def test_score_never_exceeds_bounds(self):
        verifier = ManufacturerVerifier()
        this_year = date.today().year
        supplier = {
            "business_scope": "生产制造加工",
            "registered_capital_rmb": 10_000_000,
            "alibaba_years": 3,
            "year_established": this_year - 3,
            "iso_9001": True, "iatf_16949": True, "e_mark_certified": True,
        }
        result = verifier.assess(supplier)
        assert 0 <= result["manufacturer_confidence"] <= 100

    def test_signals_are_human_readable_strings(self):
        verifier = ManufacturerVerifier()
        result = verifier.assess({"business_scope": "生产LED车灯"})
        assert all(isinstance(s, str) for s in result["manufacturer_signals"])

    def test_output_compatible_with_repository(self, tmp_path):
        db_path = tmp_path / "test.db"
        initialise_schema(db_path)
        repo = SupplierRepository(db_path=db_path)
        verifier = ManufacturerVerifier()

        supplier_id = repo.create_golden_record({
            "canonical_name": "Foo Co",
            "business_scope": "生产、制造LED车灯",
            "registered_capital_rmb": 3_000_000,
        })
        supplier = repo.get_supplier(supplier_id)
        result = verifier.assess(supplier)
        repo.update_manufacturer_verification(supplier_id, result)

        updated = repo.get_supplier(supplier_id)
        assert updated["is_manufacturer"] == 1
        assert updated["manufacturer_confidence"] == result["manufacturer_confidence"]
        assert updated["manufacturer_signals"] == result["manufacturer_signals"]
        assert updated["manufacturer_verified_at"] is not None

    def test_inconclusive_result_does_not_erase_prior_guess(self, tmp_path):
        """A prior normalizer's weaker guess (is_manufacturer=True from
        Alibaba cert text) shouldn't be wiped out by a later inconclusive
        (is_manufacturer=None) ManufacturerVerifier pass."""
        db_path = tmp_path / "test.db"
        initialise_schema(db_path)
        repo = SupplierRepository(db_path=db_path)

        supplier_id = repo.create_golden_record({"canonical_name": "Foo Co", "is_manufacturer": True})
        repo.update_manufacturer_verification(supplier_id, {
            "manufacturer_confidence": 50, "is_manufacturer": None, "manufacturer_signals": [],
        })

        supplier = repo.get_supplier(supplier_id)
        assert supplier["is_manufacturer"] == 1  # untouched
        assert supplier["manufacturer_verified_at"] is not None  # but assessment WAS recorded


# ═════════════════════════════════════════════════════════════
# FactoryPhotoVerifier — fake OpenAI-style client
# ═════════════════════════════════════════════════════════════

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


class TestFactoryPhotoVerifier:

    def test_assess_photo_parses_plausible_verdict(self):
        client = FakeOpenAIClient(response_text="VERDICT: plausible_factory\nREASONING: Real production floor visible.")
        verifier = FactoryPhotoVerifier(client=client)

        result = verifier.assess_photo(b"fakebytes", "image/jpeg", "LED marker lights", "Foo Co")
        assert result["verdict"] == "plausible_factory"
        assert "production floor" in result["reasoning"].lower()

    def test_assess_photo_parses_implausible_verdict(self):
        client = FakeOpenAIClient(response_text="VERDICT: implausible\nREASONING: This is clearly a retail showroom.")
        verifier = FactoryPhotoVerifier(client=client)

        result = verifier.assess_photo(b"fakebytes", "image/jpeg", "LED marker lights")
        assert result["verdict"] == "implausible"

    def test_assess_photo_handles_malformed_response(self):
        client = FakeOpenAIClient(response_text="I'm not sure what this is.")
        verifier = FactoryPhotoVerifier(client=client)

        result = verifier.assess_photo(b"fakebytes", "image/jpeg", "LED marker lights")
        assert result["verdict"] == "uncertain"

    def test_assess_photo_handles_api_error(self):
        client = FakeOpenAIClient(raise_error=RuntimeError("API unavailable"))
        verifier = FactoryPhotoVerifier(client=client)

        result = verifier.assess_photo(b"fakebytes", "image/jpeg", "LED marker lights")
        assert result["verdict"] == "uncertain"
        assert "API unavailable" in result["reasoning"]

    def test_request_includes_image_and_prompt(self):
        client = FakeOpenAIClient(response_text="VERDICT: uncertain\nREASONING: n/a")
        verifier = FactoryPhotoVerifier(client=client)
        verifier.assess_photo(b"fakebytes", "image/png", "hex bolts", "Bolt Co")

        content = client.chat.completions.last_call_kwargs["messages"][0]["content"]
        assert content[0]["type"] == "text"
        assert "hex bolts" in content[0]["text"]
        assert "Bolt Co" in content[0]["text"]
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_assess_photos_rollup_all_plausible(self):
        client = FakeOpenAIClient(response_text="VERDICT: plausible_factory\nREASONING: Looks real.")
        verifier = FactoryPhotoVerifier(client=client)
        photos = [{"image_bytes": b"a", "media_type": "image/jpeg"}, {"image_bytes": b"b", "media_type": "image/jpeg"}]

        result = verifier.assess_photos(photos, "LED marker lights")
        assert result["verdict"] == "plausible_factory"
        assert result["photo_count"] == 2

    def test_assess_photos_rollup_mixed_is_uncertain(self):
        call_count = {"n": 0}

        class AlternatingAPI:
            def create(self, **kwargs):
                call_count["n"] += 1
                text = "VERDICT: plausible_factory\nREASONING: ok" if call_count["n"] == 1 else "VERDICT: implausible\nREASONING: no"
                return FakeCompletion(text)

        client = FakeOpenAIClient()
        client.chat.completions = AlternatingAPI()
        verifier = FactoryPhotoVerifier(client=client)
        photos = [{"image_bytes": b"a", "media_type": "image/jpeg"}, {"image_bytes": b"b", "media_type": "image/jpeg"}]

        result = verifier.assess_photos(photos, "LED marker lights")
        assert result["verdict"] == "uncertain"

    def test_assess_photos_empty_list(self):
        verifier = FactoryPhotoVerifier(client=FakeOpenAIClient())
        result = verifier.assess_photos([], "LED marker lights")
        assert result["verdict"] == "uncertain"
        assert result["photo_count"] == 0

    def test_repository_stores_photo_assessment(self, tmp_path):
        db_path = tmp_path / "test.db"
        initialise_schema(db_path)
        repo = SupplierRepository(db_path=db_path)
        supplier_id = repo.create_golden_record({"canonical_name": "Foo Co"})

        repo.update_factory_photo_assessment(supplier_id, "plausible_factory", reasoning="Real production floor visible.")
        supplier = repo.get_supplier(supplier_id)

        assert supplier["factory_photo_verdict"] == "plausible_factory"
        assert supplier["factory_photo_assessed_at"] is not None
        assert any("production floor" in s.lower() for s in supplier["manufacturer_signals"])


# ═════════════════════════════════════════════════════════════
# Qichacha: business_scope / registered_capital_rmb parsing
# ═════════════════════════════════════════════════════════════

class FakeQichachaResponse:
    def __init__(self, json_data):
        self._json = json_data

    def json(self):
        return self._json

    def raise_for_status(self):
        pass


class FakeQichachaClient:
    def __init__(self, response_json):
        self._response_json = response_json

    def get(self, url, params=None, headers=None):
        return FakeQichachaResponse(self._response_json)


VALID_USCC = "91440101MA5ABCDE1M"


class TestQichachaBusinessScopeAndCapital:

    def test_business_scope_captured(self):
        response = {
            "Status": "200",
            "Result": {"Name": "Foo Co", "CreditCode": VALID_USCC, "BusinessScope": "生产、加工LED车灯"},
        }
        verifier = QichachaVerifier(app_key="k", app_secret="s", http_client=FakeQichachaClient(response))
        result = verifier.verify(VALID_USCC)
        assert result["business_scope"] == "生产、加工LED车灯"

    def test_registered_capital_plain_number(self):
        response = {
            "Status": "200",
            "Result": {"Name": "Foo Co", "CreditCode": VALID_USCC, "RegistCapi": "5000000"},
        }
        verifier = QichachaVerifier(app_key="k", app_secret="s", http_client=FakeQichachaClient(response))
        result = verifier.verify(VALID_USCC)
        assert result["registered_capital_rmb"] == 5_000_000.0

    def test_registered_capital_chinese_wan_notation(self):
        # "500万元人民币" means RMB 5,000,000 (500 * 10,000) — NOT 500.
        response = {
            "Status": "200",
            "Result": {"Name": "Foo Co", "CreditCode": VALID_USCC, "RegistCapi": "500万元人民币"},
        }
        verifier = QichachaVerifier(app_key="k", app_secret="s", http_client=FakeQichachaClient(response))
        result = verifier.verify(VALID_USCC)
        assert result["registered_capital_rmb"] == 5_000_000.0

    def test_registered_capital_missing(self):
        response = {"Status": "200", "Result": {"Name": "Foo Co", "CreditCode": VALID_USCC}}
        verifier = QichachaVerifier(app_key="k", app_secret="s", http_client=FakeQichachaClient(response))
        result = verifier.verify(VALID_USCC)
        assert "registered_capital_rmb" not in result

    def test_qichacha_output_feeds_manufacturer_verifier(self, tmp_path):
        """End-to-end: Qichacha populates business_scope + registered
        capital, then ManufacturerVerifier uses them directly."""
        db_path = tmp_path / "test.db"
        initialise_schema(db_path)
        repo = SupplierRepository(db_path=db_path)
        supplier_id = repo.create_golden_record({
            "canonical_name": "Foo Co", "country": "China", "uscc": VALID_USCC,
        })

        response = {
            "Status": "200",
            "Result": {
                "Name": "Foo Co", "CreditCode": VALID_USCC,
                "BusinessScope": "生产、制造LED车灯及配件",
                "RegistCapi": "1000万元人民币",
            },
        }
        qichacha = QichachaVerifier(app_key="k", app_secret="s", http_client=FakeQichachaClient(response))
        verification = qichacha.verify(VALID_USCC)
        repo.update_verification(supplier_id, verification)

        supplier = repo.get_supplier(supplier_id)
        assert supplier["business_scope"] == "生产、制造LED车灯及配件"
        assert supplier["registered_capital_rmb"] == 10_000_000.0

        result = ManufacturerVerifier().assess(supplier)
        assert result["is_manufacturer"] is True


# ═════════════════════════════════════════════════════════════
# Schema migration v1 -> v2
# ═════════════════════════════════════════════════════════════

class TestSchemaMigration:

    def test_fresh_db_gets_v2_columns_directly(self, tmp_path):
        db_path = tmp_path / "test.db"
        initialise_schema(db_path)
        with connection_scope(db_path) as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(suppliers)").fetchall()}
        assert "business_scope" in columns
        assert "registered_capital_rmb" in columns
        assert "manufacturer_signals" in columns
        assert get_schema_version(db_path) == SCHEMA_VERSION

    def test_upgrading_a_v1_only_database_adds_v2_columns(self, tmp_path):
        """Simulates a database created before this migration existed.
        Every column added in migration v2 is tagged '(v2)' in SCHEMA_SQL
        specifically so this test can reconstruct an accurate pre-v2
        schema by filtering those lines out — a real v1 database has
        every OTHER v1 column already (it's the original full schema),
        so this is a much more faithful simulation than hand-writing a
        stripped-down fake table would be."""
        from storage.database import SCHEMA_SQL

        legacy_schema_sql = "\n".join(
            line for line in SCHEMA_SQL.splitlines() if "(v2)" not in line
        )

        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(legacy_schema_sql)
        conn.execute("INSERT INTO schema_migrations (version, description) VALUES (1, 'legacy v1')")
        conn.execute("INSERT INTO suppliers (canonical_name) VALUES ('Pre-existing Co')")
        conn.commit()
        conn.close()

        # Confirm the v2 columns are genuinely absent before upgrading
        with connection_scope(db_path) as conn:
            columns_before = {row["name"] for row in conn.execute("PRAGMA table_info(suppliers)").fetchall()}
        assert "business_scope" not in columns_before

        # Now bring it up to date
        initialise_schema(db_path)

        with connection_scope(db_path) as conn:
            columns_after = {row["name"] for row in conn.execute("PRAGMA table_info(suppliers)").fetchall()}
            row = conn.execute("SELECT * FROM suppliers WHERE canonical_name = 'Pre-existing Co'").fetchone()

        assert "business_scope" in columns_after
        assert "registered_capital_rmb" in columns_after
        assert "manufacturer_signals" in columns_after
        assert row is not None  # pre-existing data survived the migration
        assert get_schema_version(db_path) == SCHEMA_VERSION

    def test_migration_is_idempotent(self, tmp_path):
        db_path = tmp_path / "test.db"
        initialise_schema(db_path)
        initialise_schema(db_path)  # should not raise (duplicate column, duplicate migration row)
        with connection_scope(db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM schema_migrations WHERE version = 2"
            ).fetchone()["n"]
        assert count == 1

    def test_migrations_registry_has_expected_columns(self):
        assert 2 in MIGRATIONS
        column_names = {col for _, col, _ in MIGRATIONS[2]["columns"]}
        assert "business_scope" in column_names
        assert "registered_capital_rmb" in column_names


class TestConnectionErrorDiagnostics:

    def test_unwritable_parent_gives_actionable_error_message(self, tmp_path, monkeypatch):
        """When sqlite3 fails to open the database file, the error should
        include the actual resolved path and a concrete diagnosis —
        not just sqlite3's own unhelpful 'unable to open database file'."""
        import os
        from storage.database import get_connection

        db_path = tmp_path / "readonly_dir" / "test.db"
        db_path.parent.mkdir()

        # Simulate the "parent exists but isn't writable" branch without
        # relying on OS-specific chmod semantics (unreliable on Windows,
        # which is exactly the platform this diagnosis targets) by
        # patching os.access to report not-writable.
        original_access = os.access

        def fake_access(path, mode):
            if str(path) == str(db_path.parent):
                return False
            return original_access(path, mode)

        monkeypatch.setattr(os, "access", fake_access)

        # Force sqlite3.connect itself to fail, since a real permission
        # denial is what we're simulating.
        import sqlite3 as sqlite3_module
        original_connect = sqlite3_module.connect

        def failing_connect(*args, **kwargs):
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(sqlite3_module, "connect", failing_connect)

        with pytest.raises(sqlite3.OperationalError) as exc_info:
            get_connection(db_path)

        message = str(exc_info.value)
        assert str(db_path.resolve()) in message
        assert "not writable" in message

    def test_missing_parent_after_mkdir_failure_gives_actionable_error(self, tmp_path, monkeypatch):
        """Covers the (rarer) branch where the parent directory still
        doesn't exist by the time sqlite3.connect runs."""
        import sqlite3 as sqlite3_module
        from storage.database import get_connection

        db_path = tmp_path / "never_created" / "test.db"

        # Let mkdir succeed normally, but then remove the directory right
        # before connect() is attempted, to exercise the "does not exist"
        # branch specifically.
        original_connect = sqlite3_module.connect

        def failing_connect(*args, **kwargs):
            db_path.parent.rmdir()
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(sqlite3_module, "connect", failing_connect)

        with pytest.raises(sqlite3.OperationalError) as exc_info:
            get_connection(db_path)

        assert "does not exist" in str(exc_info.value)


# ═════════════════════════════════════════════════════════════
# AlibabaNormalizer: factory photo URL capture
# ═════════════════════════════════════════════════════════════

class TestAlibabaNormalizerFactoryPhotos:

    def test_captures_bare_url_list(self):
        normalizer = AlibabaNormalizer()
        result = normalizer.normalise({
            "companyName": "Foo Co",
            "images": ["https://cdn.example.com/factory1.jpg", "https://cdn.example.com/factory2.jpg"],
        })
        assert result["factory_photo_urls"] == [
            "https://cdn.example.com/factory1.jpg", "https://cdn.example.com/factory2.jpg",
        ]

    def test_captures_object_shaped_photo_list(self):
        normalizer = AlibabaNormalizer()
        result = normalizer.normalise({
            "companyName": "Foo Co",
            "factoryImages": [{"url": "https://cdn.example.com/factory1.jpg"}, {"url": "https://cdn.example.com/factory2.jpg"}],
        })
        assert result["factory_photo_urls"] == [
            "https://cdn.example.com/factory1.jpg", "https://cdn.example.com/factory2.jpg",
        ]

    def test_no_photos_omits_field(self):
        normalizer = AlibabaNormalizer()
        result = normalizer.normalise({"companyName": "Foo Co"})
        assert "factory_photo_urls" not in result


# ═════════════════════════════════════════════════════════════
# Pipeline integration: manufacturer assessment stage
# ═════════════════════════════════════════════════════════════

@pytest.fixture()
def repo(tmp_path):
    db_path = tmp_path / "test.db"
    initialise_schema(db_path)
    return SupplierRepository(db_path=db_path)


class TestPipelineManufacturerStage:

    def test_run_assesses_newly_created_suppliers(self, repo):
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers={}, normalizers={})
        repo.create_golden_record({"canonical_name": "Foo Co", "iso_9001": True})

        stats = pipeline.run("widgets", sources=[])
        assert stats["manufacturer_assessed"] == 1

        supplier = repo.list_suppliers(limit=1)[0]
        assert supplier["manufacturer_verified_at"] is not None

    def test_run_manufacturer_assessment_only_skips_already_assessed(self, repo):
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers={}, normalizers={})
        supplier_id = repo.create_golden_record({"canonical_name": "Foo Co"})

        first_pass = pipeline.run_manufacturer_assessment_only()
        assert first_pass["manufacturer_assessed"] == 1

        second_pass = pipeline.run_manufacturer_assessment_only()
        assert second_pass["manufacturer_assessed"] == 0  # already assessed, not force

    def test_force_reassesses_everything(self, repo):
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers={}, normalizers={})
        repo.create_golden_record({"canonical_name": "Foo Co"})
        pipeline.run_manufacturer_assessment_only()

        forced = pipeline.run_manufacturer_assessment_only(force=True)
        assert forced["manufacturer_assessed"] == 1

    def test_manufacturer_assessment_uses_scorer_downstream(self, repo):
        """A confirmed trader (is_manufacturer=False with high confidence)
        should now correctly trigger the scorer's 'avoid' override, since
        manufacturer_confidence is finally a real computed number."""
        pipeline = SupplierIntelligencePipeline(repo=repo, scrapers={}, normalizers={})
        supplier_id = repo.create_golden_record({
            "canonical_name": "Trader Co",
            "business_scope": "批发、零售、货物进出口贸易",
            "registered_capital_rmb": 50_000,
        })

        pipeline.run("widgets", sources=[])
        supplier = repo.get_supplier(supplier_id)

        assert supplier["is_manufacturer"] == 0
        assert supplier["manufacturer_confidence"] <= 30
        assert supplier["recommendation"] == "avoid"
