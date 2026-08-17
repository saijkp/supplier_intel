"""
tests/test_tracker_exporter.py

Tests for batch/tracker_exporter.py -- the Group 3 tracker-format
export. Exercises against a real (temp-file) database via
storage.database.initialise_schema, same fixture pattern
tests/test_ai_platform_repository.py and tests/test_search_suppliers_full.py
already use, since this module's whole job is joining together several
real repository read paths (field_provenance, supplier_phone_numbers,
supplier_capabilities, supplier_reputation_snippets) -- faking all of
those would just re-implement the same joins twice.
"""

from __future__ import annotations

import csv
import io

import pytest

from batch.tracker_exporter import (
    EVIDENCE_COLUMNS,
    PASTE_RANGE_COLUMNS,
    REMOVED_CANDIDATES_COLUMNS,
    build_removed_candidates_export,
    build_tracker_export,
)
from storage.database import initialise_schema


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    initialise_schema(path)
    return path


@pytest.fixture()
def repo(db_path):
    from storage.repository import SupplierRepository
    return SupplierRepository(db_path=db_path)


def _rows(csv_text: str):
    return list(csv.DictReader(io.StringIO(csv_text)))


class TestPasteRangeShape:

    def test_header_matches_the_real_tracker_column_order_exactly(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})
        csv_text = build_tracker_export([supplier_id], repo)
        header = csv_text.splitlines()[0].split(",")
        assert header[:len(PASTE_RANGE_COLUMNS)] == list(PASTE_RANGE_COLUMNS)

    def test_evidence_columns_follow_immediately_after_the_paste_range(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})
        csv_text = build_tracker_export([supplier_id], repo)
        header = csv_text.splitlines()[0].split(",")
        assert header[len(PASTE_RANGE_COLUMNS):] == list(EVIDENCE_COLUMNS)

    def test_a_through_qualified_are_always_pending_never_a_verdict(self, repo):
        """"Pending" is an explicit not-yet-reviewed placeholder, never
        a real verdict -- distinguishes "not yet reviewed" from "forgot
        to check" for the buyer auditing the file."""
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme Co", "domain": "acme.com",
            "address": "1 Main St", "primary_phone": "+123", "primary_email": "a@acme.com",
            "factory_location": "Foshan, China",
        })
        row = _rows(build_tracker_export([supplier_id], repo))[0]
        for col in (
            "A - Website Deep-Dive", "B - Certifications", "C - Factory Authenticity",
            "D - Reviews & Ratings", "Qualified",
        ):
            assert row[col] == "Pending"

    def test_notes_and_date_reviewed_are_always_blank(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme Co", "domain": "acme.com",
            "address": "1 Main St", "primary_phone": "+123", "primary_email": "a@acme.com",
            "factory_location": "Foshan, China",
        })
        row = _rows(build_tracker_export([supplier_id], repo))[0]
        for col in ("Notes / Difficulties", "Date Reviewed"):
            assert row[col] == ""

    def test_no_column_is_1_indexed_sequential(self, repo):
        a = repo.create_golden_record({"canonical_name": "A Co", "domain": "a.com"})
        b = repo.create_golden_record({"canonical_name": "B Co", "domain": "b.com"})
        rows = _rows(build_tracker_export([a, b], repo))
        assert [r["No."] for r in rows] == ["1", "2"]

    def test_a_supplier_id_with_no_matching_row_is_skipped_not_raised(self, repo):
        real = repo.create_golden_record({"canonical_name": "Real Co", "domain": "real.com"})
        rows = _rows(build_tracker_export([real, 999999], repo))
        assert len(rows) == 1
        assert rows[0]["Supplier Name"] == "Real Co"


class TestSortKeyOrdering:

    def test_more_evidence_sorts_first(self, repo):
        thin = repo.create_golden_record({"canonical_name": "Thin Co", "domain": "thin.com"})
        rich = repo.create_golden_record({
            "canonical_name": "Rich Co", "domain": "rich.com",
            "address": "1 Main St", "primary_phone": "+123",
            "primary_email": "a@rich.com", "factory_location": "Foshan, China",
        })
        rows = _rows(build_tracker_export([thin, rich], repo))
        assert [r["Supplier Name"] for r in rows] == ["Rich Co", "Thin Co"]
        assert rows[0]["Sort Key (helper)"] == "4"
        assert rows[1]["Sort Key (helper)"] == "0"

    def test_ties_break_alphabetically_by_name(self, repo):
        b = repo.create_golden_record({"canonical_name": "Beta Co", "domain": "beta.com"})
        a = repo.create_golden_record({"canonical_name": "Alpha Co", "domain": "alpha.com"})
        rows = _rows(build_tracker_export([b, a], repo))
        assert [r["Supplier Name"] for r in rows] == ["Alpha Co", "Beta Co"]

    def test_checks_remaining_is_always_all_four_on_a_fresh_export(self, repo):
        rich = repo.create_golden_record({
            "canonical_name": "Rich Co", "domain": "rich.com",
            "address": "1 Main St", "primary_phone": "+123",
            "primary_email": "a@rich.com", "factory_location": "Foshan, China",
        })
        row = _rows(build_tracker_export([rich], repo))[0]
        assert row["Checks Remaining"] == "A, B, C, D"


class TestSourceUrlColumns:

    def test_address_and_factory_location_source_urls_come_from_field_provenance(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme Co", "domain": "acme.com",
            "address": "1 Main St", "factory_location": "Foshan, China",
        })
        repo.save_field_provenance(
            supplier_id=supplier_id, field_name="address", value="1 Main St",
            source_url="https://acme.com/contact", raw_snippet="...",
            extraction_method="llm_grounded_extraction", source_tier="own_domain", claim_type="verifiable_fact",
        )
        repo.save_field_provenance(
            supplier_id=supplier_id, field_name="factory_location", value="Foshan, China",
            source_url="https://acme.com/about", raw_snippet="...",
            extraction_method="llm_grounded_extraction", source_tier="own_domain", claim_type="verifiable_fact",
        )
        row = _rows(build_tracker_export([supplier_id], repo))[0]
        assert row["Address Source URL"] == "https://acme.com/contact"
        assert row["Factory Location Source URL"] == "https://acme.com/about"

    def test_no_provenance_leaves_source_url_blank(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com", "address": "1 Main St"})
        row = _rows(build_tracker_export([supplier_id], repo))[0]
        assert row["Address Source URL"] == ""

    def test_phone_and_email_source_use_contact_source_pages(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme Co", "domain": "acme.com",
            "primary_phone": "+123", "primary_email": "a@acme.com",
            "contact_source_pages": ["https://acme.com/contact"],
        })
        row = _rows(build_tracker_export([supplier_id], repo))[0]
        assert row["Phone Source Page(s)"] == "https://acme.com/contact"
        assert row["Email Source Page(s)"] == "https://acme.com/contact"


class TestFacilityPhotosAndStreetView:

    def test_candidate_facility_photo_urls_are_semicolon_joined(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme Co", "domain": "acme.com",
            "candidate_facility_photo_urls": ["https://acme.com/f1.jpg", "https://acme.com/f2.jpg"],
        })
        row = _rows(build_tracker_export([supplier_id], repo))[0]
        assert row["Candidate Facility Photo URLs"] == "https://acme.com/f1.jpg; https://acme.com/f2.jpg"

    def test_street_view_link_is_generated_from_address(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme Co", "domain": "acme.com", "address": "1 Main St, Foshan",
        })
        row = _rows(build_tracker_export([supplier_id], repo))[0]
        assert row["Street View Link"].startswith("https://www.google.com/maps/search/?api=1&query=")
        assert "Foshan" in row["Street View Link"]

    def test_no_address_means_no_street_view_link(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})
        row = _rows(build_tracker_export([supplier_id], repo))[0]
        assert row["Street View Link"] == ""

    def test_linkedin_search_link_is_generated_from_company_name(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Injection Moulding Co", "domain": "acme.com"})
        row = _rows(build_tracker_export([supplier_id], repo))[0]
        link = row["LinkedIn Search Link"]
        assert link.startswith("https://www.google.com/search?q=")
        assert "linkedin.com" in link
        assert "Acme" in link

    def test_no_name_means_no_linkedin_search_link(self, repo):
        """Defensive only -- create_golden_record itself requires a
        canonical_name, so this exercises the empty-string branch
        directly rather than via a real gap in practice."""
        from batch.tracker_exporter import _linkedin_search_link
        assert _linkedin_search_link("") == ""


class TestCertificationsAndReputationSnippets:

    def test_only_standard_category_capabilities_are_surfaced(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})
        repo.add_capability_finding(supplier_id, {
            "reported_term": "ISO 9001", "canonical_term": "iso 9001",
            "category": "standard", "relationship": "asserted", "confidence": 0.9,
            "evidence": "We are ISO 9001 certified.", "source_url": "https://acme.com/about",
        })
        repo.add_capability_finding(supplier_id, {
            "reported_term": "injection moulding", "canonical_term": "injection moulding",
            "category": "process", "relationship": "in_house", "confidence": 0.9,
            "evidence": "We mould in-house.", "source_url": "https://acme.com/about",
        })
        row = _rows(build_tracker_export([supplier_id], repo))[0]
        assert "iso 9001" in row["Certifications Claimed (source + evidence)"]
        assert "injection moulding" not in row["Certifications Claimed (source + evidence)"]
        assert "https://acme.com/about" in row["Certifications Claimed (source + evidence)"]

    def test_no_capabilities_leaves_certifications_column_blank(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})
        row = _rows(build_tracker_export([supplier_id], repo))[0]
        assert row["Certifications Claimed (source + evidence)"] == ""

    def test_reputation_snippets_are_split_by_query_type(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})
        repo.save_reputation_snippets(supplier_id, [
            {"query_type": "scam", "query_text": "Acme Co scam", "title": "t1", "link": "https://x.com/1", "snippet": "no scam reports"},
            {"query_type": "review", "query_text": "Acme Co review", "title": "t2", "link": "https://x.com/2", "snippet": "4.5 stars"},
        ])
        row = _rows(build_tracker_export([supplier_id], repo))[0]
        assert "no scam reports" in row["D-Search: Scam"]
        assert "4.5 stars" in row["D-Search: Review"]
        assert row["D-Search: Factory Tour"] == ""

    def test_no_reputation_search_run_leaves_all_three_columns_blank(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.com"})
        row = _rows(build_tracker_export([supplier_id], repo))[0]
        assert row["D-Search: Scam"] == row["D-Search: Review"] == row["D-Search: Factory Tour"] == ""


class TestRemovedCandidatesExport:

    def test_header_matches(self, repo):
        csv_text = build_removed_candidates_export([], repo)
        header = csv_text.splitlines()[0].split(",")
        assert header == list(REMOVED_CANDIDATES_COLUMNS)

    def test_only_flagged_suppliers_are_included(self, repo):
        good = repo.create_golden_record({"canonical_name": "Good Co", "domain": "good.com"})
        flagged = repo.create_golden_record({"canonical_name": "Broker Co", "domain": "broker.com"})
        repo.update_supplier_fields(flagged, {
            "flagged": True, "flag_reason": "Broker/network model, not a single factory",
        })
        rows = _rows(build_removed_candidates_export([good, flagged], repo))
        assert [r["Company Name"] for r in rows] == ["Broker Co"]
        assert rows[0]["Website"] == "https://broker.com"
        assert rows[0]["Reason"] == "Broker/network model, not a single factory"

    def test_scoped_to_the_given_ids_only(self, repo):
        """A supplier flagged for a DIFFERENT product category's
        sourcing run has no place on THIS export's removed-candidates
        list -- only ids explicitly passed in are ever considered."""
        flagged_elsewhere = repo.create_golden_record({"canonical_name": "Elsewhere Co", "domain": "elsewhere.com"})
        repo.update_supplier_fields(flagged_elsewhere, {"flagged": True, "flag_reason": "not relevant here"})

        rows = _rows(build_removed_candidates_export([], repo))

        assert rows == []

    def test_no_flagged_suppliers_among_given_ids_returns_header_only(self, repo):
        good = repo.create_golden_record({"canonical_name": "Good Co", "domain": "good.com"})
        csv_text = build_removed_candidates_export([good], repo)
        assert _rows(csv_text) == []
