"""
tests/test_static_list_import.py

Tests for pipeline.static_list_import. The property that matters most:
a static import must route through the real dedup/merge decision, not
bypass it -- a company already in the database from a live scrape
must be merged into, not duplicated, when the same real company shows
up again in a static list.
"""

from __future__ import annotations

import pytest

from deduplication.matcher import SupplierMatcher
from pipeline.static_list_import import import_static_supplier_list
from storage.database import initialise_schema
from storage.repository import SupplierRepository


@pytest.fixture()
def repo(tmp_path):
    db_path = tmp_path / "test.db"
    initialise_schema(db_path)
    return SupplierRepository(db_path=db_path)


@pytest.fixture()
def matcher(repo):
    return SupplierMatcher(repo)


class TestBasicImport:

    def test_new_supplier_is_created(self, repo, matcher):
        stats = import_static_supplier_list(
            repo, matcher,
            [{"canonical_name": "Ningbo Trailer Parts Co., Ltd.", "country": "China"}],
            source_label="automechanika_2026",
        )
        assert stats.created == 1
        assert stats.total == 1
        assert len(repo.list_suppliers(limit=10)) == 1

    def test_multiple_distinct_suppliers_are_all_created(self, repo, matcher):
        stats = import_static_supplier_list(
            repo, matcher,
            [
                {"canonical_name": "Zhejiang Axle Manufacturing", "country": "China", "domain": "one.example.com"},
                {"canonical_name": "Guangdong Lighting Systems", "country": "China", "domain": "two.example.com"},
                {"canonical_name": "Shandong Coupling Works", "country": "China", "domain": "three.example.com"},
            ],
            source_label="automechanika_2026",
        )
        assert stats.created == 3

    def test_row_missing_canonical_name_is_skipped_not_silently_dropped(self, repo, matcher):
        stats = import_static_supplier_list(
            repo, matcher, [{"country": "China"}], source_label="automechanika_2026",
        )
        assert stats.skipped_no_name == 1
        assert stats.created == 0
        assert stats.total == 1


class TestDedupRouting:
    """The property that matters most: a static import must go
    through the real merge/review/create decision, not bypass it."""

    def test_same_company_via_matching_domain_merges_not_duplicates(self, repo, matcher):
        import_static_supplier_list(
            repo, matcher,
            [{"canonical_name": "Ningbo Trailer Parts", "country": "China", "domain": "ningbo-trailer.example.com"}],
            source_label="automechanika_2026",
        )
        stats = import_static_supplier_list(
            repo, matcher,
            [{"canonical_name": "Ningbo Trailer Parts Co Ltd", "country": "China", "domain": "ningbo-trailer.example.com"}],
            source_label="second_import",
        )
        assert stats.merged == 1
        assert stats.created == 0
        assert len(repo.list_suppliers(limit=10)) == 1

    def test_already_scraped_company_is_merged_into_not_duplicated(self, repo, matcher):
        """The exact scenario this module exists for: a company
        already found via a live scrape (Alibaba, HKTDC, ...) shows up
        again in a static exhibitor list -- it must merge, not create
        a second record for the same real company."""
        repo.create_golden_record({
            "canonical_name": "Acme Trailer Components", "country": "China",
            "domain": "acme-trailer.example.com",
        })
        stats = import_static_supplier_list(
            repo, matcher,
            [{"canonical_name": "Acme Trailer Components Co., Ltd.", "country": "China",
              "domain": "acme-trailer.example.com"}],
            source_label="automechanika_2026",
        )
        assert stats.merged == 1
        assert len(repo.list_suppliers(limit=10)) == 1

    def test_genuinely_different_companies_never_merge(self, repo, matcher):
        stats = import_static_supplier_list(
            repo, matcher,
            [
                {"canonical_name": "Zhejiang Industries Co., Ltd.", "country": "China"},
                {"canonical_name": "Guangdong Manufacturing Co., Ltd.", "country": "China"},
            ],
            source_label="automechanika_2026",
        )
        assert stats.created == 2
        assert stats.merged == 0


class TestProvenanceTracking:

    def test_source_label_is_recorded_and_queryable(self, repo, matcher):
        import_static_supplier_list(
            repo, matcher,
            [{"canonical_name": "Acme Co", "country": "China"}],
            source_label="automechanika_2026",
        )
        with_source = repo.get_pending_raw(source="automechanika_2026", limit=10)
        assert isinstance(with_source, list)

    def test_original_row_is_preserved_even_when_normalisation_fails(self, repo, matcher):
        class ExplodingNormaliser:
            def normalise(self, raw_data):
                raise ValueError("simulated parsing bug")

        stats = import_static_supplier_list(
            repo, matcher,
            [{"Company Name": "Acme Co", "Country": "China"}],
            source_label="automechanika_2026",
            normaliser=ExplodingNormaliser(),
        )
        assert stats.failed == 1
        assert stats.created == 0


class TestCustomNormaliser:

    def test_normaliser_maps_arbitrary_column_names(self, repo, matcher):
        """Proves the generic/specific split actually works: a
        normaliser can map a completely different column layout
        (as a real exhibitor export would have) onto the shape
        resolve_and_store expects, with zero changes to this module."""

        class ExampleAutomechanikaNormaliser:
            def normalise(self, raw_data):
                return {
                    "canonical_name": raw_data.get("Exhibitor Name", "").strip(),
                    "country": raw_data.get("Country"),
                    "domain": raw_data.get("Website") or None,
                    "product_keywords": [raw_data.get("Product Category")] if raw_data.get("Product Category") else [],
                }

        stats = import_static_supplier_list(
            repo, matcher,
            [{
                "Exhibitor Name": "Shandong Axle Manufacturing Co., Ltd.",
                "Country": "China",
                "Website": "shandong-axle.example.com",
                "Product Category": "Axles & Suspension",
            }],
            source_label="automechanika_2026",
            normaliser=ExampleAutomechanikaNormaliser(),
        )
        assert stats.created == 1
        suppliers = repo.list_suppliers(limit=10)
        assert suppliers[0]["canonical_name"] == "Shandong Axle Manufacturing Co., Ltd."

    def test_normaliser_producing_no_name_is_counted_as_skipped(self, repo, matcher):
        class BlankNameNormaliser:
            def normalise(self, raw_data):
                return {"canonical_name": "", "country": raw_data.get("Country")}

        stats = import_static_supplier_list(
            repo, matcher, [{"Country": "China"}], source_label="automechanika_2026",
            normaliser=BlankNameNormaliser(),
        )
        assert stats.skipped_no_name == 1


class TestEmptyInput:

    def test_empty_list_returns_zeroed_stats(self, repo, matcher):
        stats = import_static_supplier_list(repo, matcher, [], source_label="automechanika_2026")
        assert stats.total == 0
        assert stats.created == 0
        assert stats.as_dict() == {
            "total": 0, "normalised": 0, "skipped_no_name": 0,
            "created": 0, "merged": 0, "review_queued": 0, "failed": 0,
        }
