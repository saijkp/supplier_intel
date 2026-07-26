"""
tests/test_reports.py

Tests for reports/generator.py — Markdown report rendering and CSV
export.
"""

from __future__ import annotations

import csv

from reports.generator import (
    CSV_COLUMNS,
    export_suppliers_csv,
    generate_markdown_report,
    save_markdown_report,
    suppliers_to_csv_string,
)
from storage.database import initialise_schema
from storage.repository import SupplierRepository


def _make_repo(tmp_path):
    db_path = tmp_path / "test.db"
    initialise_schema(db_path)
    return SupplierRepository(db_path=db_path)


class TestGenerateMarkdownReport:

    def test_empty_database_reports_zero_suppliers(self, tmp_path):
        repo = _make_repo(tmp_path)
        content = generate_markdown_report(repo)
        assert "Suppliers: 0" in content
        assert "No suppliers matched" in content

    def test_report_includes_supplier_details(self, tmp_path):
        repo = _make_repo(tmp_path)
        supplier_id = repo.create_golden_record({
            "canonical_name": "Shenzhen LED Masters Co Ltd",
            "country": "China", "city": "Shenzhen",
            "is_manufacturer": True, "uscc_verified": True,
            "iso_9001": True, "e_mark_certified": True,
            "confirmed_shipments_uk": 12,
            "contact_name": "Li Wei", "primary_email": "sales@ledmasters.com",
        })
        repo.update_scores(supplier_id, {
            "composite_score": 82, "recommendation": "recommended",
            "verification_score": 90, "export_score": 80, "platform_score": 70, "contact_score": 100,
        })

        content = generate_markdown_report(repo)

        assert "Shenzhen LED Masters Co Ltd" in content
        assert "recommended" in content
        assert "82/100" in content
        assert "China, Shenzhen" in content
        assert "Manufacturer:** Yes" in content
        assert "ISO 9001" in content
        assert "E-mark" in content
        assert "Li Wei" in content
        assert "sales@ledmasters.com" in content

    def test_report_respects_recommendation_filter(self, tmp_path):
        repo = _make_repo(tmp_path)
        id_a = repo.create_golden_record({"canonical_name": "A Co"})
        id_b = repo.create_golden_record({"canonical_name": "B Co"})
        repo.update_scores(id_a, {"composite_score": 90, "recommendation": "recommended"})
        repo.update_scores(id_b, {"composite_score": 10, "recommendation": "avoid"})

        content = generate_markdown_report(repo, recommendation="recommended")
        assert "A Co" in content
        assert "B Co" not in content

    def test_report_respects_min_score_filter(self, tmp_path):
        repo = _make_repo(tmp_path)
        id_a = repo.create_golden_record({"canonical_name": "High Co"})
        id_b = repo.create_golden_record({"canonical_name": "Low Co"})
        repo.update_scores(id_a, {"composite_score": 90})
        repo.update_scores(id_b, {"composite_score": 5})

        content = generate_markdown_report(repo, min_score=50)
        assert "High Co" in content
        assert "Low Co" not in content

    def test_report_shows_query_when_given(self, tmp_path):
        repo = _make_repo(tmp_path)
        content = generate_markdown_report(repo, query="LED marker light")
        assert 'Search: "LED marker light"' in content

    def test_unknown_manufacturer_status_rendered(self, tmp_path):
        repo = _make_repo(tmp_path)
        repo.create_golden_record({"canonical_name": "Unknown Status Co"})
        content = generate_markdown_report(repo)
        assert "Manufacturer:** Unknown" in content

    def test_confirmed_trader_rendered_as_no(self, tmp_path):
        repo = _make_repo(tmp_path)
        repo.create_golden_record({"canonical_name": "Trader Co", "is_manufacturer": False})
        content = generate_markdown_report(repo)
        assert "Manufacturer:** No" in content

    def test_manufacturer_confidence_and_signals_rendered(self, tmp_path):
        repo = _make_repo(tmp_path)
        repo.create_golden_record({
            "canonical_name": "Foo Co", "is_manufacturer": True,
            "manufacturer_confidence": 85,
            "manufacturer_signals": ["Registered business scope explicitly includes production/manufacturing"],
        })
        content = generate_markdown_report(repo)
        assert "confidence: 85/100" in content
        assert "Manufacturer evidence:" in content
        assert "explicitly includes production/manufacturing" in content


class TestSaveMarkdownReport:

    def test_save_writes_file(self, tmp_path):
        repo = _make_repo(tmp_path)
        repo.create_golden_record({"canonical_name": "Foo Co"})

        output_path = tmp_path / "reports" / "suppliers.md"
        result_path = save_markdown_report(repo, output_path)

        assert result_path == output_path
        assert output_path.exists()
        assert "Foo Co" in output_path.read_text(encoding="utf-8")


class TestCSVExport:

    def test_suppliers_to_csv_string_includes_header_and_rows(self, tmp_path):
        repo = _make_repo(tmp_path)
        repo.create_golden_record({
            "canonical_name": "Foo Co", "country": "China", "is_manufacturer": True,
        })
        suppliers = repo.list_suppliers()

        csv_text = suppliers_to_csv_string(suppliers)
        reader = csv.DictReader(csv_text.splitlines())
        rows = list(reader)

        assert reader.fieldnames == CSV_COLUMNS
        assert len(rows) == 1
        assert rows[0]["canonical_name"] == "Foo Co"
        assert rows[0]["country"] == "China"

    def test_export_suppliers_csv_writes_file(self, tmp_path):
        repo = _make_repo(tmp_path)
        repo.create_golden_record({"canonical_name": "Foo Co", "country": "China"})
        repo.create_golden_record({"canonical_name": "Bar Co", "country": "India"})

        output_path = tmp_path / "exports" / "suppliers.csv"
        result_path = export_suppliers_csv(repo, output_path)

        assert result_path.exists()
        with open(result_path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 2
        names = {row["canonical_name"] for row in rows}
        assert names == {"Foo Co", "Bar Co"}

    def test_export_respects_filters(self, tmp_path):
        repo = _make_repo(tmp_path)
        id_a = repo.create_golden_record({"canonical_name": "High Co"})
        id_b = repo.create_golden_record({"canonical_name": "Low Co"})
        repo.update_scores(id_a, {"composite_score": 90, "recommendation": "recommended"})
        repo.update_scores(id_b, {"composite_score": 5, "recommendation": "avoid"})

        output_path = tmp_path / "filtered.csv"
        export_suppliers_csv(repo, output_path, recommendation="recommended")

        with open(output_path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["canonical_name"] == "High Co"

    def test_csv_columns_are_a_curated_subset(self):
        # Sanity check that this stays a deliberately curated list, not
        # every column in the suppliers table.
        assert "canonical_name" in CSV_COLUMNS
        assert "id" in CSV_COLUMNS
        assert len(CSV_COLUMNS) < 30

    def test_manufacturer_signals_flattened_to_readable_string(self, tmp_path):
        repo = _make_repo(tmp_path)
        repo.create_golden_record({
            "canonical_name": "Foo Co",
            "manufacturer_confidence": 90,
            "manufacturer_signals": ["Signal one", "Signal two"],
        })
        csv_text = suppliers_to_csv_string(repo.list_suppliers())
        reader = csv.DictReader(csv_text.splitlines())
        row = next(reader)

        assert row["manufacturer_confidence"] == "90"
        assert "Signal one" in row["manufacturer_signals"]
        assert "Signal two" in row["manufacturer_signals"]
        assert "[" not in row["manufacturer_signals"]  # not a raw Python list repr
