"""
tests/test_monitoring_service.py

Tests for monitoring/monitoring_service.py -- MonitoringService's
enable/disable, single-supplier capture, batch pending pass, and
field-diff logic. No real network: `reachability_classifier` is always
injected as a fake callable, same convention as
discovery/linde_dealer_import.py's own `website_checker` injection.
"""

from __future__ import annotations

import json

import pytest

from monitoring.monitoring_service import (
    MONITORING_COST_DISCLOSURE,
    VALID_SNAPSHOT_FIELDS,
    MonitoringService,
)
from storage.database import initialise_schema
from storage.repository import SupplierRepository


@pytest.fixture()
def repo(tmp_path):
    db_path = tmp_path / "test.db"
    initialise_schema(db_path)
    return SupplierRepository(db_path=db_path)


def _service(repo, reachability="live"):
    return MonitoringService(repo=repo, reachability_classifier=lambda url: reachability)


def _supplier(repo, **overrides):
    fields = {
        "canonical_name": "Acme Co", "domain": "acme.com",
        "primary_email": "sales@acme.com", "primary_phone": "555-0001",
        "companies_house_status": "active",
    }
    fields.update(overrides)
    return repo.create_golden_record(fields)


class TestEnableMonitoring:

    def test_valid_cadence_creates_settings(self, repo):
        supplier_id = _supplier(repo)
        result = _service(repo).enable_monitoring(supplier_id, "monthly")
        assert result["cadence"] == "monthly"
        settings = repo.get_monitoring_settings(supplier_id)
        assert settings["cadence"] == "monthly"
        assert settings["next_check_due_at"] > "2026-01-01"  # a real future date, not blank

    def test_quarterly_due_date_is_further_out_than_monthly(self, repo):
        supplier_id = _supplier(repo)
        service = _service(repo)
        monthly = service.enable_monitoring(supplier_id, "monthly")
        quarterly_supplier = _supplier(repo, canonical_name="Other Co", domain="other.com")
        quarterly = service.enable_monitoring(quarterly_supplier, "quarterly")
        assert quarterly["next_check_due_at"] > monthly["next_check_due_at"]

    def test_invalid_cadence_raises(self, repo):
        supplier_id = _supplier(repo)
        with pytest.raises(ValueError):
            _service(repo).enable_monitoring(supplier_id, "weekly")

    def test_unknown_supplier_raises(self, repo):
        with pytest.raises(ValueError):
            _service(repo).enable_monitoring(999999, "monthly")


class TestDisableMonitoring:

    def test_removes_settings(self, repo):
        supplier_id = _supplier(repo)
        service = _service(repo)
        service.enable_monitoring(supplier_id, "monthly")
        service.disable_monitoring(supplier_id)
        assert repo.get_monitoring_settings(supplier_id) is None


class TestCaptureSnapshot:

    def test_captures_all_five_v1_fields(self, repo):
        supplier_id = _supplier(repo)
        values = _service(repo).capture_snapshot(supplier_id)
        assert set(values.keys()) == set(VALID_SNAPSHOT_FIELDS)

    def test_writes_one_snapshot_row_per_field(self, repo):
        supplier_id = _supplier(repo)
        _service(repo).capture_snapshot(supplier_id)
        all_snapshots = repo.get_snapshots(supplier_id)
        assert {s["field_name"] for s in all_snapshots} == set(VALID_SNAPSHOT_FIELDS)

    def test_reads_contact_fields_straight_off_the_supplier_row(self, repo):
        supplier_id = _supplier(repo, primary_email="ceo@acme.com", primary_phone="555-9999")
        values = _service(repo).capture_snapshot(supplier_id)
        assert values["primary_email"] == "ceo@acme.com"
        assert values["primary_phone"] == "555-9999"

    def test_reads_companies_house_status_from_stored_value_not_a_fresh_api_call(self, repo):
        """v1 does NOT re-query Companies House -- it snapshots
        whatever's already on the supplier row."""
        supplier_id = _supplier(repo, companies_house_status="dissolved")
        values = _service(repo).capture_snapshot(supplier_id)
        assert values["companies_house_status"] == "dissolved"

    def test_missing_domain_produces_no_reachability_check(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "No Domain Co"})
        values = _service(repo).capture_snapshot(supplier_id)
        assert values["website_reachability"] is None

    def test_website_reachability_uses_injected_classifier(self, repo):
        supplier_id = _supplier(repo)
        values = _service(repo, reachability="blocked").capture_snapshot(supplier_id)
        assert values["website_reachability"] == "blocked"

    def test_certifications_claimed_reads_only_standard_category_capabilities(self, repo):
        supplier_id = _supplier(repo)
        repo.add_capability_finding(supplier_id, {
            "category": "standard", "canonical_term": "iso 9001",
            "reported_term": "ISO 9001", "relationship": "in_house",
            "confidence": 0.9, "evidence": "ISO 9001 certified since 2015.",
            "source_url": "https://acme.com/about",
        })
        repo.add_capability_finding(supplier_id, {
            "category": "process", "canonical_term": None,
            "reported_term": "laser cutting", "relationship": "in_house",
            "confidence": 0.8, "evidence": "In-house laser cutting.",
            "source_url": "https://acme.com/about",
        })
        values = _service(repo).capture_snapshot(supplier_id)
        assert json.loads(values["certifications_claimed"]) == ["iso 9001"]

    def test_no_certifications_produces_none_not_empty_list(self, repo):
        supplier_id = _supplier(repo)
        values = _service(repo).capture_snapshot(supplier_id)
        assert values["certifications_claimed"] is None

    def test_unknown_supplier_raises(self, repo):
        with pytest.raises(ValueError):
            _service(repo).capture_snapshot(999999)


class TestCaptureSnapshotPending:

    def test_no_suppliers_due_returns_zero_attempted(self, repo):
        result = _service(repo).capture_snapshot_pending()
        assert result["attempted"] == 0
        assert result["status"] == "completed"

    def test_due_supplier_is_checked_and_rescheduled(self, repo):
        supplier_id = _supplier(repo)
        repo.upsert_monitoring_settings(supplier_id=supplier_id, cadence="monthly", next_check_due_at="2020-01-01 00:00:00")

        result = _service(repo).capture_snapshot_pending()
        assert result["attempted"] == 1
        assert result["paid_api_calls"] == 0

        settings = repo.get_monitoring_settings(supplier_id)
        assert settings["next_check_due_at"] > "2026-01-01"  # rescheduled forward
        assert settings["last_checked_at"] is not None

    def test_not_yet_due_supplier_is_skipped(self, repo):
        supplier_id = _supplier(repo)
        repo.upsert_monitoring_settings(supplier_id=supplier_id, cadence="monthly", next_check_due_at="2099-01-01 00:00:00")
        result = _service(repo).capture_snapshot_pending()
        assert result["attempted"] == 0

    def test_limit_caps_how_many_are_processed(self, repo):
        for i in range(3):
            sid = _supplier(repo, canonical_name=f"Co {i}", domain=f"co{i}.example.com")
            repo.upsert_monitoring_settings(supplier_id=sid, cadence="monthly", next_check_due_at="2020-01-01 00:00:00")
        result = _service(repo).capture_snapshot_pending(limit=2)
        assert result["attempted"] == 2
        assert result["total_due"] == 2

    def test_due_supplier_actually_writes_snapshots(self, repo):
        supplier_id = _supplier(repo)
        repo.upsert_monitoring_settings(supplier_id=supplier_id, cadence="monthly", next_check_due_at="2020-01-01 00:00:00")
        _service(repo).capture_snapshot_pending()
        assert len(repo.get_snapshots(supplier_id)) == len(VALID_SNAPSHOT_FIELDS)


class TestDiffField:

    def test_fewer_than_two_observations_returns_none(self, repo):
        supplier_id = _supplier(repo)
        repo.save_snapshot(supplier_id=supplier_id, field_name="primary_email", value="a@acme.com")
        assert _service(repo).diff_field(supplier_id, "primary_email") is None

    def test_unchanged_value_returns_none(self, repo):
        supplier_id = _supplier(repo)
        repo.save_snapshot(supplier_id=supplier_id, field_name="primary_email", value="a@acme.com")
        repo.save_snapshot(supplier_id=supplier_id, field_name="primary_email", value="a@acme.com")
        assert _service(repo).diff_field(supplier_id, "primary_email") is None

    def test_changed_value_produces_a_neutral_fact_string(self, repo):
        supplier_id = _supplier(repo)
        repo.save_snapshot(supplier_id=supplier_id, field_name="companies_house_status", value="active")
        repo.save_snapshot(supplier_id=supplier_id, field_name="companies_house_status", value="dissolved")
        diff = _service(repo).diff_field(supplier_id, "companies_house_status")
        assert diff is not None
        assert "active" in diff
        assert "dissolved" in diff

    def test_diff_never_contains_a_severity_word(self, repo):
        """CLAUDE.md standing rule 2, extended to diff output: the
        system never assigns severity, the buyer does."""
        supplier_id = _supplier(repo)
        repo.save_snapshot(supplier_id=supplier_id, field_name="companies_house_status", value="active")
        repo.save_snapshot(supplier_id=supplier_id, field_name="companies_house_status", value="dissolved")
        diff = _service(repo).diff_field(supplier_id, "companies_house_status")
        for banned_word in ("warning", "flag", "risk", "alert", "critical"):
            assert banned_word not in diff.lower()

    def test_certifications_list_reordering_is_not_a_false_diff(self, repo):
        """List-shaped fields compare as sets, not raw JSON string
        equality -- order must never produce a false "changed"."""
        supplier_id = _supplier(repo)
        repo.save_snapshot(supplier_id=supplier_id, field_name="certifications_claimed", value=json.dumps(["ce marking", "iso 9001"]))
        repo.save_snapshot(supplier_id=supplier_id, field_name="certifications_claimed", value=json.dumps(["iso 9001", "ce marking"]))
        assert _service(repo).diff_field(supplier_id, "certifications_claimed") is None

    def test_certifications_actual_change_is_detected(self, repo):
        supplier_id = _supplier(repo)
        repo.save_snapshot(supplier_id=supplier_id, field_name="certifications_claimed", value=json.dumps(["iso 9001"]))
        repo.save_snapshot(supplier_id=supplier_id, field_name="certifications_claimed", value=None)
        diff = _service(repo).diff_field(supplier_id, "certifications_claimed")
        assert diff is not None
        assert "iso 9001" in diff
        assert "(none)" in diff

    def test_invalid_field_name_raises(self, repo):
        supplier_id = _supplier(repo)
        with pytest.raises(ValueError):
            _service(repo).diff_field(supplier_id, "not_a_real_field")


class TestDiffAllFields:

    def test_returns_only_fields_that_actually_changed(self, repo):
        supplier_id = _supplier(repo)
        repo.save_snapshot(supplier_id=supplier_id, field_name="primary_email", value="old@acme.com")
        repo.save_snapshot(supplier_id=supplier_id, field_name="primary_email", value="new@acme.com")
        repo.save_snapshot(supplier_id=supplier_id, field_name="primary_phone", value="555-0001")
        repo.save_snapshot(supplier_id=supplier_id, field_name="primary_phone", value="555-0001")

        diffs = _service(repo).diff_all_fields(supplier_id)
        assert len(diffs) == 1
        assert "Primary email" in diffs[0]

    def test_no_changes_returns_empty_list(self, repo):
        supplier_id = _supplier(repo)
        repo.save_snapshot(supplier_id=supplier_id, field_name="primary_email", value="a@acme.com")
        repo.save_snapshot(supplier_id=supplier_id, field_name="primary_email", value="a@acme.com")
        assert _service(repo).diff_all_fields(supplier_id) == []


class TestCostDisclosure:

    def test_mentions_zero_dollar_and_no_paid_api(self):
        assert "$0" in MONITORING_COST_DISCLOSURE
        assert "no paid" in MONITORING_COST_DISCLOSURE.lower()
