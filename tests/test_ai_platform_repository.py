"""
tests/test_ai_platform_repository.py

Tests for the v11 schema/repository additions backing the AI Discovery/
Collection/Verification platform (verification_ai/, collection/,
discovery/) -- storage.repository.SupplierRepository's new
get_suppliers_needing_collection/get_suppliers_needing_ai_verification/
record_collection_run/get_collection_runs/record_verification_history/
get_verification_history/record_discovery_run methods.
update_supplier_fields_with_history/get_supplier_change_log are covered
in tests/test_phase1.py alongside the rest of the write-path tests.
"""

from __future__ import annotations

from storage.database import initialise_schema
from storage.repository import SupplierRepository


def _make_repo(tmp_path):
    db_path = tmp_path / "test.db"
    initialise_schema(db_path)
    return SupplierRepository(db_path=db_path)


class TestGetSuppliersNeedingCollection:

    def test_domain_less_supplier_is_excluded(self, tmp_path):
        repo = _make_repo(tmp_path)
        repo.create_golden_record({"canonical_name": "No Domain Co"})
        assert repo.get_suppliers_needing_collection() == []

    def test_domain_supplier_never_collected_is_included(self, tmp_path):
        repo = _make_repo(tmp_path)
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        results = repo.get_suppliers_needing_collection()
        assert [s["id"] for s in results] == [supplier_id]

    def test_already_collected_supplier_is_excluded_without_force(self, tmp_path):
        repo = _make_repo(tmp_path)
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        repo.record_collection_run(supplier_id=supplier_id, status="success", pages_visited=3)
        assert repo.get_suppliers_needing_collection() == []

    def test_force_includes_already_collected_suppliers(self, tmp_path):
        repo = _make_repo(tmp_path)
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        repo.record_collection_run(supplier_id=supplier_id, status="success", pages_visited=3)
        results = repo.get_suppliers_needing_collection(force=True)
        assert [s["id"] for s in results] == [supplier_id]

    def test_existing_alibaba_sourced_suppliers_are_immediately_eligible(self, tmp_path):
        """The ~1,400 suppliers already in production all have a domain
        (from Alibaba's own listing) and have never had collection_status
        set -- this is what makes "augment the existing catalogue"
        require zero special-casing."""
        repo = _make_repo(tmp_path)
        supplier_id = repo.create_golden_record({
            "canonical_name": "Legacy Alibaba Co", "domain": "legacyco.en.alibaba.com",
            "alibaba_url": "https://legacyco.en.alibaba.com",
        })
        results = repo.get_suppliers_needing_collection()
        assert [s["id"] for s in results] == [supplier_id]

    def test_newest_supplier_returned_first(self, tmp_path):
        """A bulk 'collect pending' run should reach a supplier just
        discovered before it reaches the oldest row in the database --
        real user-facing confusion otherwise (a bulk pass silently
        processing arbitrary old suppliers, not the ones just found)."""
        repo = _make_repo(tmp_path)
        old_id = repo.create_golden_record({"canonical_name": "Old Co", "domain": "old.example.com"})
        new_id = repo.create_golden_record({"canonical_name": "New Co", "domain": "new.example.com"})
        results = repo.get_suppliers_needing_collection(limit=1)
        assert [s["id"] for s in results] == [new_id]
        assert old_id != new_id


class TestGetSuppliersNeedingAiVerification:

    def test_never_assessed_supplier_is_included(self, tmp_path):
        repo = _make_repo(tmp_path)
        supplier_id = repo.create_golden_record({"canonical_name": "Acme"})
        results = repo.get_suppliers_needing_ai_verification()
        assert [s["id"] for s in results] == [supplier_id]

    def test_already_assessed_supplier_is_excluded_without_force(self, tmp_path):
        repo = _make_repo(tmp_path)
        supplier_id = repo.create_golden_record({"canonical_name": "Acme"})
        repo.update_supplier_fields(supplier_id, {"ai_confidence_assessed_at": "2026-08-03T00:00:00+00:00"})
        assert repo.get_suppliers_needing_ai_verification() == []

    def test_force_includes_already_assessed_suppliers(self, tmp_path):
        repo = _make_repo(tmp_path)
        supplier_id = repo.create_golden_record({"canonical_name": "Acme"})
        repo.update_supplier_fields(supplier_id, {"ai_confidence_assessed_at": "2026-08-03T00:00:00+00:00"})
        results = repo.get_suppliers_needing_ai_verification(force=True)
        assert [s["id"] for s in results] == [supplier_id]

    def test_newest_supplier_returned_first(self, tmp_path):
        repo = _make_repo(tmp_path)
        old_id = repo.create_golden_record({"canonical_name": "Old Co"})
        new_id = repo.create_golden_record({"canonical_name": "New Co"})
        results = repo.get_suppliers_needing_ai_verification(limit=1)
        assert [s["id"] for s in results] == [new_id]
        assert old_id != new_id


class TestGetSuppliersNeedingReverification:

    def test_never_verified_supplier_is_included(self, tmp_path):
        repo = _make_repo(tmp_path)
        supplier_id = repo.create_golden_record({"canonical_name": "Acme"})
        results = repo.get_suppliers_needing_reverification(older_than_days=30)
        assert [s["id"] for s in results] == [supplier_id]

    def test_recently_verified_supplier_is_excluded(self, tmp_path):
        repo = _make_repo(tmp_path)
        supplier_id = repo.create_golden_record({"canonical_name": "Acme"})
        repo.update_supplier_fields(supplier_id, {"last_verified": "2026-08-01T00:00:00+00:00"})
        results = repo.get_suppliers_needing_reverification(older_than_days=30)
        assert results == []

    def test_stale_verified_supplier_is_included(self, tmp_path):
        repo = _make_repo(tmp_path)
        supplier_id = repo.create_golden_record({"canonical_name": "Acme"})
        repo.update_supplier_fields(supplier_id, {"last_verified": "2020-01-01T00:00:00+00:00"})
        results = repo.get_suppliers_needing_reverification(older_than_days=30)
        assert [s["id"] for s in results] == [supplier_id]

    def test_ordering_never_verified_first_then_oldest(self, tmp_path):
        repo = _make_repo(tmp_path)
        stale_id = repo.create_golden_record({"canonical_name": "Stale Co"})
        repo.update_supplier_fields(stale_id, {"last_verified": "2020-01-01T00:00:00+00:00"})
        never_id = repo.create_golden_record({"canonical_name": "Never Co"})

        results = repo.get_suppliers_needing_reverification(older_than_days=30)

        assert [s["id"] for s in results] == [never_id, stale_id]


class TestCollectionRuns:

    def test_record_collection_run_updates_supplier_summary_fields(self, tmp_path):
        repo = _make_repo(tmp_path)
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        repo.record_collection_run(
            supplier_id=supplier_id, status="success", pages_visited=5,
            artifacts_dir=f"{supplier_id}/20260803", proxy_provider="webshare",
            completed_at="2026-08-03T00:00:00+00:00",
        )
        supplier = repo.get_supplier(supplier_id)
        assert supplier["collection_status"] == "success"
        assert supplier["collection_last_run_at"] == "2026-08-03T00:00:00+00:00"

    def test_get_collection_runs_returns_history_newest_first(self, tmp_path):
        repo = _make_repo(tmp_path)
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        repo.record_collection_run(supplier_id=supplier_id, status="failed", error_message="timeout",
                                    completed_at="2026-08-01T00:00:00+00:00")
        repo.record_collection_run(supplier_id=supplier_id, status="success", pages_visited=4,
                                    completed_at="2026-08-02T00:00:00+00:00")

        runs = repo.get_collection_runs(supplier_id)
        assert len(runs) == 2
        assert runs[0]["status"] == "success"  # most recent first
        assert runs[1]["status"] == "failed"
        assert runs[1]["error_message"] == "timeout"


class TestVerificationHistory:

    def test_record_and_get_verification_history(self, tmp_path):
        repo = _make_repo(tmp_path)
        supplier_id = repo.create_golden_record({"canonical_name": "Acme"})
        repo.record_verification_history(
            supplier_id=supplier_id, verification_type="ai_cross_check",
            confidence_score=78, verdict="corroborated",
            summary="Address and phone match independent sources.",
            evidence=["Google Places match", "LinkedIn page found"],
            model_used="gpt-4o-mini",
        )
        history = repo.get_verification_history(supplier_id)
        assert len(history) == 1
        assert history[0]["confidence_score"] == 78
        assert history[0]["verdict"] == "corroborated"
        assert history[0]["evidence_json"] == ["Google Places match", "LinkedIn page found"]

    def test_a_no_change_reverify_still_writes_a_history_row(self, tmp_path):
        """A run that confirms nothing changed is still a useful audit
        fact -- unlike supplier_change_log (field-diff-only),
        verification_history is one row per RUN, always."""
        repo = _make_repo(tmp_path)
        supplier_id = repo.create_golden_record({"canonical_name": "Acme"})
        repo.record_verification_history(supplier_id=supplier_id, verification_type="ai_cross_check", verdict="unchanged")
        repo.record_verification_history(supplier_id=supplier_id, verification_type="ai_cross_check", verdict="unchanged")
        assert len(repo.get_verification_history(supplier_id)) == 2

    def test_history_scoped_to_one_supplier(self, tmp_path):
        repo = _make_repo(tmp_path)
        id_a = repo.create_golden_record({"canonical_name": "A Co"})
        id_b = repo.create_golden_record({"canonical_name": "B Co"})
        repo.record_verification_history(supplier_id=id_a, verification_type="ai_cross_check")
        repo.record_verification_history(supplier_id=id_b, verification_type="ai_cross_check")
        assert len(repo.get_verification_history(id_a)) == 1
        assert len(repo.get_verification_history(id_b)) == 1


class TestDiscoveryRuns:

    def test_record_discovery_run(self, tmp_path):
        repo = _make_repo(tmp_path)
        run_id = repo.record_discovery_run(
            product_query="trailer axle", category="Axles & Suspension", country="China",
            candidates_found=12, candidates_validated=5, candidates_rejected=6, candidates_duplicate=1,
        )
        assert isinstance(run_id, int)
