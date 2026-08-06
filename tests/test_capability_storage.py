"""
tests/test_capability_storage.py

Tests for the v5 schema migration (supplier_capabilities table,
suppliers.capability_extracted_at) and the corresponding
storage.repository methods.
"""

from __future__ import annotations

import sqlite3

import pytest

from storage.database import SCHEMA_VERSION, get_schema_version, initialise_schema
from storage.repository import SupplierRepository


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test_suppliers.db"
    initialise_schema(path)
    return path


@pytest.fixture()
def repo(db_path):
    return SupplierRepository(db_path=db_path)


def _make_supplier(repo, **overrides):
    data = {"canonical_name": "Acme Trailer Parts", "country": "China", "domain": "acme.example.com"}
    data.update(overrides)
    return repo.create_golden_record(data)


def _finding(**overrides):
    base = dict(
        reported_term="rotomoulding", canonical_term="rotational moulding", category="process",
        relationship="in_house", confidence=0.9, evidence="we operate...", source_url="https://acme.example.com",
    )
    base.update(overrides)
    return base


class TestSchemaVersion:

    def test_current_schema_version(self):
        assert SCHEMA_VERSION == 14

    def test_fresh_database_has_supplier_capabilities_table(self, db_path):
        conn = sqlite3.connect(str(db_path))
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "supplier_capabilities" in tables

    def test_fresh_database_has_capability_extracted_at_column(self, db_path):
        conn = sqlite3.connect(str(db_path))
        columns = {row[1] for row in conn.execute("PRAGMA table_info(suppliers)").fetchall()}
        conn.close()
        assert "capability_extracted_at" in columns

    def test_pre_v5_database_migrates_cleanly(self, tmp_path):
        """A database created before this table existed must gain it
        via the incremental migration path, not just the fresh-install
        schema — the two must never silently diverge."""
        from storage.database import SCHEMA_SQL, MIGRATIONS

        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(SCHEMA_SQL)
        for v in (1, 2, 3, 4):
            conn.execute(
                "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                (v, "legacy"),
            )
        conn.commit()
        conn.close()

        initialise_schema(db_path)
        assert get_schema_version(db_path) == SCHEMA_VERSION
        assert 5 in MIGRATIONS


class TestAddCapabilityFinding:

    def test_insert_returns_a_row_id(self, repo):
        supplier_id = _make_supplier(repo)
        row_id = repo.add_capability_finding(supplier_id, _finding())
        assert row_id is not None

    def test_duplicate_finding_is_idempotent(self, repo):
        """Same (supplier, reported_term, relationship) inserted twice
        must not create a second row — re-running extraction against
        an unchanged page must be a no-op."""
        supplier_id = _make_supplier(repo)
        first = repo.add_capability_finding(supplier_id, _finding())
        second = repo.add_capability_finding(supplier_id, _finding())
        assert first is not None
        assert second is None  # INSERT OR IGNORE — no new row
        assert len(repo.get_capabilities(supplier_id)) == 1

    def test_different_relationship_is_a_distinct_finding(self, repo):
        supplier_id = _make_supplier(repo)
        repo.add_capability_finding(supplier_id, _finding(relationship="in_house"))
        repo.add_capability_finding(supplier_id, _finding(relationship="subcontracted"))
        assert len(repo.get_capabilities(supplier_id)) == 2

    def test_unmapped_finding_with_null_canonical_term_is_still_idempotent(self, repo):
        """The specific bug this schema had to avoid: SQLite never
        treats two NULLs as equal for UNIQUE, so if canonical_term were
        part of the key, two identical unmapped findings would
        duplicate. It must not."""
        supplier_id = _make_supplier(repo)
        unmapped = _finding(canonical_term=None, category=None, reported_term="hydroforming")
        first = repo.add_capability_finding(supplier_id, unmapped)
        second = repo.add_capability_finding(supplier_id, unmapped)
        assert first is not None
        assert second is None
        assert len(repo.get_capabilities(supplier_id)) == 1


class TestGetCapabilities:

    def test_returns_only_this_suppliers_findings(self, repo):
        s1 = _make_supplier(repo, canonical_name="Company One", domain="one.example.com")
        s2 = _make_supplier(repo, canonical_name="Company Two", domain="two.example.com")
        repo.add_capability_finding(s1, _finding())
        repo.add_capability_finding(s2, _finding(reported_term="injection molding"))

        assert len(repo.get_capabilities(s1)) == 1
        assert len(repo.get_capabilities(s2)) == 1

    def test_empty_for_supplier_with_no_findings(self, repo):
        supplier_id = _make_supplier(repo)
        assert repo.get_capabilities(supplier_id) == []


class TestFindSuppliersByCapability:

    def test_finds_supplier_with_matching_in_house_capability(self, repo):
        supplier_id = _make_supplier(repo)
        repo.add_capability_finding(supplier_id, _finding())

        results = repo.find_suppliers_by_capability("rotational moulding")
        assert len(results) == 1
        assert results[0]["id"] == supplier_id

    def test_relationship_filter_excludes_subcontracted_when_requiring_in_house(self, repo):
        supplier_id = _make_supplier(repo)
        repo.add_capability_finding(supplier_id, _finding(relationship="subcontracted"))
        results = repo.find_suppliers_by_capability("rotational moulding", relationship="in_house")
        assert results == []

    def test_relationship_none_matches_either(self, repo):
        supplier_id = _make_supplier(repo)
        repo.add_capability_finding(supplier_id, _finding(relationship="subcontracted"))
        results = repo.find_suppliers_by_capability("rotational moulding", relationship=None)
        assert len(results) == 1

    def test_minimum_confidence_filters_weak_evidence(self, repo):
        supplier_id = _make_supplier(repo)
        repo.add_capability_finding(supplier_id, _finding(confidence=0.3))
        assert repo.find_suppliers_by_capability("rotational moulding", min_confidence=0.5) == []
        assert len(repo.find_suppliers_by_capability("rotational moulding", min_confidence=0.2)) == 1

    def test_no_match_returns_empty_list(self, repo):
        _make_supplier(repo)
        assert repo.find_suppliers_by_capability("injection moulding") == []

    def test_returns_full_supplier_record_not_just_capability_row(self, repo):
        supplier_id = _make_supplier(repo, canonical_name="Acme Trailer Parts")
        repo.add_capability_finding(supplier_id, _finding())
        result = repo.find_suppliers_by_capability("rotational moulding")[0]
        assert result["canonical_name"] == "Acme Trailer Parts"
        assert result["domain"] == "acme.example.com"


class TestGetSuppliersNeedingCapabilityExtraction:

    def test_supplier_with_domain_and_no_prior_extraction_is_returned(self, repo):
        supplier_id = _make_supplier(repo)
        needing = repo.get_suppliers_needing_capability_extraction()
        assert any(s["id"] == supplier_id for s in needing)

    def test_supplier_without_a_domain_is_excluded(self, repo):
        supplier_id = _make_supplier(repo, domain=None, canonical_name="No Domain Co")
        needing = repo.get_suppliers_needing_capability_extraction()
        assert not any(s["id"] == supplier_id for s in needing)

    def test_mark_attempted_excludes_it_from_future_runs(self, repo):
        """The bug this test guards against: without an explicit
        'attempted' marker, a supplier whose website genuinely has zero
        capability findings would be re-attempted on every single run
        forever."""
        supplier_id = _make_supplier(repo)
        assert any(s["id"] == supplier_id for s in repo.get_suppliers_needing_capability_extraction())

        repo.mark_capability_extraction_attempted(supplier_id)

        needing = repo.get_suppliers_needing_capability_extraction()
        assert not any(s["id"] == supplier_id for s in needing)

    def test_force_true_returns_every_supplier_with_a_domain_regardless(self, repo):
        supplier_id = _make_supplier(repo)
        repo.mark_capability_extraction_attempted(supplier_id)

        assert not any(s["id"] == supplier_id for s in repo.get_suppliers_needing_capability_extraction())
        assert any(s["id"] == supplier_id for s in repo.get_suppliers_needing_capability_extraction(force=True))

    def test_mark_attempted_actually_persists_the_timestamp(self, repo):
        """Guards against the exact silent-drop bug caught during
        review: update_supplier_fields silently ignores any column not
        in SUPPLIER_WRITABLE_FIELDS, so this would pass even if the
        write were a no-op unless we check the column directly."""
        supplier_id = _make_supplier(repo)
        repo.mark_capability_extraction_attempted(supplier_id)
        supplier = repo.get_supplier(supplier_id)
        assert supplier["capability_extracted_at"] is not None


class TestEnrichContactDetails:

    def test_fills_primary_email_when_missing(self, repo):
        supplier_id = _make_supplier(repo)
        result = repo.enrich_contact_details(supplier_id, emails=["sales@acme.com"], phones=[])
        assert result["primary_email_set"] is True
        assert repo.get_supplier(supplier_id)["primary_email"] == "sales@acme.com"

    def test_never_overwrites_an_existing_primary_email(self, repo):
        supplier_id = _make_supplier(repo, primary_email="existing@acme.com")
        result = repo.enrich_contact_details(supplier_id, emails=["found@acme.com"], phones=[])
        assert result["primary_email_set"] is False
        assert repo.get_supplier(supplier_id)["primary_email"] == "existing@acme.com"

    def test_existing_primary_email_means_found_email_goes_to_secondary(self, repo):
        supplier_id = _make_supplier(repo, primary_email="existing@acme.com")
        result = repo.enrich_contact_details(supplier_id, emails=["found@acme.com"], phones=[])
        assert result["secondary_emails_added"] == 1
        assert "found@acme.com" in (repo.get_supplier(supplier_id)["secondary_emails"] or [])

    def test_second_and_later_found_emails_go_to_secondary_even_with_no_prior_primary(self, repo):
        supplier_id = _make_supplier(repo)
        result = repo.enrich_contact_details(
            supplier_id, emails=["first@acme.com", "second@acme.com"], phones=[]
        )
        assert result["primary_email_set"] is True
        assert result["secondary_emails_added"] == 1
        supplier = repo.get_supplier(supplier_id)
        assert supplier["primary_email"] == "first@acme.com"
        assert supplier["secondary_emails"] == ["second@acme.com"]

    def test_does_not_duplicate_an_email_already_in_secondary(self, repo):
        supplier_id = _make_supplier(repo, primary_email="existing@acme.com")
        repo.enrich_contact_details(supplier_id, emails=["found@acme.com"], phones=[])
        result = repo.enrich_contact_details(supplier_id, emails=["found@acme.com"], phones=[])
        assert result["secondary_emails_added"] == 0
        assert repo.get_supplier(supplier_id)["secondary_emails"] == ["found@acme.com"]

    def test_fills_primary_phone_when_missing(self, repo):
        supplier_id = _make_supplier(repo)
        result = repo.enrich_contact_details(supplier_id, emails=[], phones=["+8657487654321"])
        assert result["primary_phone_set"] is True
        assert repo.get_supplier(supplier_id)["primary_phone"] == "+8657487654321"

    def test_never_overwrites_an_existing_primary_phone(self, repo):
        supplier_id = _make_supplier(repo, primary_phone="+8600000000")
        result = repo.enrich_contact_details(supplier_id, emails=[], phones=["+8657487654321"])
        assert result["primary_phone_set"] is False
        assert repo.get_supplier(supplier_id)["primary_phone"] == "+8600000000"

    def test_no_emails_or_phones_found_is_a_no_op(self, repo):
        supplier_id = _make_supplier(repo)
        result = repo.enrich_contact_details(supplier_id, emails=[], phones=[])
        assert result == {
            "primary_email_set": False, "secondary_emails_added": 0,
            "primary_phone_set": False, "contact_form_url_set": False,
        }

    def test_nonexistent_supplier_does_not_raise(self, repo):
        result = repo.enrich_contact_details(999999, emails=["x@acme.com"], phones=[])
        assert result["primary_email_set"] is False

    def test_contact_form_url_is_set_when_no_email_exists(self, repo):
        supplier_id = _make_supplier(repo)
        result = repo.enrich_contact_details(
            supplier_id, emails=[], phones=[], contact_form_url="https://acme.example.com/contact",
        )
        assert result["contact_form_url_set"] is True
        assert repo.get_supplier(supplier_id)["contact_form_url"] == "https://acme.example.com/contact"

    def test_contact_form_url_is_not_set_when_an_email_already_exists(self, repo):
        supplier_id = _make_supplier(repo, primary_email="existing@acme.com")
        result = repo.enrich_contact_details(
            supplier_id, emails=[], phones=[], contact_form_url="https://acme.example.com/contact",
        )
        assert result["contact_form_url_set"] is False
        assert repo.get_supplier(supplier_id)["contact_form_url"] is None

    def test_contact_form_url_is_not_set_when_an_email_is_found_in_this_same_call(self, repo):
        """The fallback must never coexist with a real email found in
        the identical call -- email always wins."""
        supplier_id = _make_supplier(repo)
        result = repo.enrich_contact_details(
            supplier_id, emails=["found@acme.com"], phones=[],
            contact_form_url="https://acme.example.com/contact",
        )
        assert result["primary_email_set"] is True
        assert result["contact_form_url_set"] is False

    def test_contact_form_url_is_never_overwritten_once_set(self, repo):
        supplier_id = _make_supplier(repo)
        repo.enrich_contact_details(supplier_id, emails=[], phones=[], contact_form_url="https://a.example.com/contact")
        result = repo.enrich_contact_details(supplier_id, emails=[], phones=[], contact_form_url="https://b.example.com/contact")
        assert result["contact_form_url_set"] is False
        assert repo.get_supplier(supplier_id)["contact_form_url"] == "https://a.example.com/contact"


class TestGetSuppliersNeedingWebsiteSearch:

    def test_domain_less_supplier_is_returned(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "No Domain Co", "domain": None})
        needing = repo.get_suppliers_needing_website_search()
        assert any(s["id"] == supplier_id for s in needing)

    def test_supplier_with_a_domain_is_excluded(self, repo):
        supplier_id = _make_supplier(repo)  # has domain="acme.example.com"
        needing = repo.get_suppliers_needing_website_search()
        assert not any(s["id"] == supplier_id for s in needing)

    def test_mark_attempted_excludes_it_from_future_runs(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "No Domain Co", "domain": None})
        assert any(s["id"] == supplier_id for s in repo.get_suppliers_needing_website_search())

        repo.mark_website_search_attempted(supplier_id)

        needing = repo.get_suppliers_needing_website_search()
        assert not any(s["id"] == supplier_id for s in needing)

    def test_force_re_attempts_already_searched_suppliers(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "No Domain Co", "domain": None})
        repo.mark_website_search_attempted(supplier_id)

        assert not any(s["id"] == supplier_id for s in repo.get_suppliers_needing_website_search())
        assert any(s["id"] == supplier_id for s in repo.get_suppliers_needing_website_search(force=True))

    def test_mark_attempted_actually_persists(self, repo):
        """Same class of bug caught earlier for capability_extracted_at
        -- a new timestamp column is only real if it's also in
        SUPPLIER_WRITABLE_FIELDS, or the write silently no-ops."""
        supplier_id = repo.create_golden_record({"canonical_name": "No Domain Co", "domain": None})
        repo.mark_website_search_attempted(supplier_id)
        supplier = repo.get_supplier(supplier_id)
        assert supplier["website_search_attempted_at"] is not None

    def test_finding_a_domain_and_marking_attempted_together_removes_it_from_the_queue(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "No Domain Co", "domain": None})
        repo.update_supplier_fields(supplier_id, {"domain": "found-site.example.com"})
        repo.mark_website_search_attempted(supplier_id)

        assert not any(s["id"] == supplier_id for s in repo.get_suppliers_needing_website_search())
        assert repo.get_supplier(supplier_id)["domain"] == "found-site.example.com"
