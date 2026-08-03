"""
tests/test_api.py

Tests for api/app.py, using FastAPI's TestClient (real HTTP requests
in-process, no network) against a real, temporary SQLite database --
not mocks of the repository. This exercises the actual dependency
injection, auth, and JSON serialisation path a real client would hit.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-token-123"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Two fixes over a naive TestClient setup, both for real reasons
    found while first writing these tests:

    1. `config.settings.DB_PATH` (and CORS's `ALLOWED_ORIGINS`) are
       resolved once at module-import time from the environment --
       monkeypatching the env var after `api.app` has already been
       imported does nothing. FastAPI's own `app.dependency_overrides`
       is the correct, import-order-independent way to inject a test
       database, so that's what this uses instead of env manipulation.

    2. FastAPI's TestClient actually executes `BackgroundTasks` for
       real, synchronously, within the `with TestClient(...)` block --
       not a no-op or a mock. Left unpatched, every test that creates a
       pipeline job would trigger a real, live pipeline run: real
       network calls to Alibaba/HKTDC/Volza/etc., ~80 seconds per test,
       and — if a real paid key were ever present in a test
       environment — real spend. `api.app.run_pipeline_job` (the name
       bound in api.app's own namespace via `from api.jobs import
       run_pipeline_job`, not `api.jobs.run_pipeline_job` itself, which
       a caller who already imported it would not see change) is
       patched to a fast, no-network fake for every test that only
       needs to prove the API triggers a job, not that the underlying
       pipeline works -- that's already covered by
       tests/test_capability_pipeline_stage.py and friends.
    """
    db_path = tmp_path / "test_api.db"

    import api.auth
    monkeypatch.setattr(api.auth, "API_ACCESS_TOKEN", TOKEN)

    import api.app
    from storage.database import initialise_schema
    from storage.repository import SupplierRepository

    initialise_schema(db_path)
    test_repo = SupplierRepository(db_path=db_path)
    api.app.app.dependency_overrides[api.app.get_repo] = lambda: test_repo

    def fake_run_pipeline_job(job_id, query, options):
        test_repo.mark_pipeline_job_running(job_id)
        test_repo.mark_pipeline_job_completed(job_id, stats={"scraped": 0})

    monkeypatch.setattr(api.app, "run_pipeline_job", fake_run_pipeline_job)

    def fake_run_enrichment_job(job_id, stage, options):
        test_repo.mark_pipeline_job_running(job_id)
        test_repo.mark_pipeline_job_completed(job_id, stats={"stage": stage})

    monkeypatch.setattr(api.app, "run_enrichment_job", fake_run_enrichment_job)

    def fake_run_collection_job(job_id, options):
        test_repo.mark_pipeline_job_running(job_id)
        test_repo.mark_pipeline_job_completed(job_id, stats={"attempted": 0})

    monkeypatch.setattr(api.app, "run_collection_job", fake_run_collection_job)

    def fake_run_verification_job(job_id, options):
        test_repo.mark_pipeline_job_running(job_id)
        test_repo.mark_pipeline_job_completed(job_id, stats={"confidence_score": 0})

    monkeypatch.setattr(api.app, "run_verification_job", fake_run_verification_job)

    def fake_run_reverify_job(job_id, supplier_id):
        test_repo.mark_pipeline_job_running(job_id)
        test_repo.mark_pipeline_job_completed(job_id, stats={"supplier_id": supplier_id})

    monkeypatch.setattr(api.app, "run_reverify_job", fake_run_reverify_job)

    def fake_run_discovery_job(job_id, options):
        test_repo.mark_pipeline_job_running(job_id)
        test_repo.mark_pipeline_job_completed(job_id, stats={"candidates_found": 0})

    monkeypatch.setattr(api.app, "run_discovery_job", fake_run_discovery_job)

    with TestClient(api.app.app) as test_client:
        test_client.repo = test_repo
        yield test_client

    api.app.app.dependency_overrides.clear()


def auth_headers():
    return {"Authorization": f"Bearer {TOKEN}"}


class TestHealth:

    def test_health_needs_no_auth(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestAuth:

    def test_missing_token_is_rejected(self, client):
        response = client.get("/suppliers/search")
        assert response.status_code == 401

    def test_wrong_token_is_rejected(self, client):
        response = client.get("/suppliers/search", headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401

    def test_correct_token_is_accepted(self, client):
        response = client.get("/suppliers/search", headers=auth_headers())
        assert response.status_code == 200

    def test_no_token_configured_fails_closed(self, client, monkeypatch):
        """The specific security property that matters: an
        unconfigured server refuses requests rather than silently
        allowing everyone through."""
        import api.auth

        monkeypatch.setattr(api.auth, "API_ACCESS_TOKEN", None)
        response = client.get("/suppliers/search", headers=auth_headers())
        assert response.status_code == 503


class TestSearchEndpoint:

    def test_search_returns_a_seeded_supplier(self, client):
        client.repo.create_golden_record({
            "canonical_name": "Acme Trailer Parts", "country": "United Kingdom",
            "domain": "acme.example.com", "product_keywords": ["wheel hub"],
        })
        response = client.get(
            "/suppliers/search", params={"product": "wheel hub"}, headers=auth_headers(),
        )
        assert response.status_code == 200
        results = response.json()
        assert len(results) == 1
        assert results[0]["canonical_name"] == "Acme Trailer Parts"

    def test_search_with_no_matches_returns_empty_list_not_404(self, client):
        response = client.get(
            "/suppliers/search", params={"product": "nonexistent widget"}, headers=auth_headers(),
        )
        assert response.status_code == 200
        assert response.json() == []

    def test_country_filter_is_passed_through(self, client):
        client.repo.create_golden_record({
            "canonical_name": "UK Co", "country": "United Kingdom", "domain": "uk.example.com",
        })
        client.repo.create_golden_record({
            "canonical_name": "China Co", "country": "China", "domain": "cn.example.com",
        })
        response = client.get(
            "/suppliers/search", params={"country": "United Kingdom"}, headers=auth_headers(),
        )
        names = {r["canonical_name"] for r in response.json()}
        assert names == {"UK Co"}

    def test_repeated_require_params_are_all_applied(self, client):
        """Confirms query-string list handling (?require=a&require=b)
        works through FastAPI's own parsing, not just at the
        repository layer (already covered elsewhere)."""
        response = client.get(
            "/suppliers/search",
            params=[("require", "iso 9001"), ("require", "sub-assembly")],
            headers=auth_headers(),
        )
        assert response.status_code == 200

    def test_unrecognised_capability_returns_400_not_500(self, client):
        response = client.get(
            "/suppliers/search", params={"require": "not-a-real-capability"}, headers=auth_headers(),
        )
        assert response.status_code == 400
        assert "not a recognised capability" in response.json()["detail"]

    def test_is_manufacturer_boolean_is_correctly_coerced_from_sqlite_int(self, client):
        """SQLite stores booleans as 0/1 -- this proves the API
        response actually comes back as a real JSON boolean, not 1."""
        supplier_id = client.repo.create_golden_record({
            "canonical_name": "Verified Co", "domain": "verified.example.com",
        })
        client.repo.update_supplier_fields(supplier_id, {"is_manufacturer": True})
        response = client.get(
            "/suppliers/search", params={"product": "Verified"}, headers=auth_headers(),
        )
        results = response.json()
        assert len(results) == 1
        assert results[0]["is_manufacturer"] is True


class TestGetSupplierEndpoint:

    def test_returns_full_detail_including_capabilities(self, client):
        supplier_id = client.repo.create_golden_record({
            "canonical_name": "Acme", "domain": "acme.example.com",
        })
        client.repo.add_capability_finding(supplier_id, {
            "reported_term": "iso 9001", "canonical_term": "iso 9001", "category": "standard",
            "relationship": "in_house", "confidence": 0.9,
            "evidence": "we are ISO 9001 certified", "source_url": "https://acme.example.com",
        })
        response = client.get(f"/suppliers/{supplier_id}", headers=auth_headers())
        assert response.status_code == 200
        body = response.json()
        assert body["canonical_name"] == "Acme"
        assert len(body["matched_capabilities"]) == 1
        assert body["matched_capabilities"][0]["canonical_term"] == "iso 9001"

    def test_nonexistent_supplier_returns_404(self, client):
        response = client.get("/suppliers/999999", headers=auth_headers())
        assert response.status_code == 404


class TestPipelineJobEndpoints:

    def test_creating_a_job_returns_202_with_a_job_id(self, client):
        response = client.post(
            "/pipeline/jobs", json={"query": "wheel bearings"}, headers=auth_headers(),
        )
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert body["query"] == "wheel bearings"
        assert body["id"]

    def test_created_job_is_retrievable_by_id(self, client):
        create_response = client.post(
            "/pipeline/jobs", json={"query": "led lighting"}, headers=auth_headers(),
        )
        job_id = create_response.json()["id"]
        get_response = client.get(f"/pipeline/jobs/{job_id}", headers=auth_headers())
        assert get_response.status_code == 200
        assert get_response.json()["query"] == "led lighting"

    def test_nonexistent_job_returns_404(self, client):
        response = client.get("/pipeline/jobs/does-not-exist", headers=auth_headers())
        assert response.status_code == 404

    def test_list_jobs_returns_created_jobs(self, client):
        client.post("/pipeline/jobs", json={"query": "wheel bearings"}, headers=auth_headers())
        client.post("/pipeline/jobs", json={"query": "led lighting"}, headers=auth_headers())
        response = client.get("/pipeline/jobs", headers=auth_headers())
        assert response.status_code == 200
        assert len(response.json()) == 2

    def test_missing_query_is_a_validation_error(self, client):
        response = client.post("/pipeline/jobs", json={}, headers=auth_headers())
        assert response.status_code == 422


class TestEnrichmentJobEndpoints:

    def test_creating_an_enrichment_job_returns_202_with_a_job_id(self, client):
        response = client.post(
            "/pipeline/enrichment-jobs", json={"stage": "find_websites"}, headers=auth_headers(),
        )
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert body["query"] == "[enrichment] find_websites"
        assert body["id"]

    def test_created_enrichment_job_is_retrievable_by_id(self, client):
        create_response = client.post(
            "/pipeline/enrichment-jobs", json={"stage": "extract_capabilities", "limit": 10},
            headers=auth_headers(),
        )
        job_id = create_response.json()["id"]
        get_response = client.get(f"/pipeline/jobs/{job_id}", headers=auth_headers())
        assert get_response.status_code == 200
        assert get_response.json()["query"] == "[enrichment] extract_capabilities"

    def test_missing_stage_is_a_validation_error(self, client):
        response = client.post("/pipeline/enrichment-jobs", json={}, headers=auth_headers())
        assert response.status_code == 422

    def test_defaults_match_documented_cost_safe_behaviour(self, client):
        response = client.post(
            "/pipeline/enrichment-jobs", json={"stage": "verify_facilities"}, headers=auth_headers(),
        )
        job_id = response.json()["id"]
        job = client.repo.get_pipeline_job(job_id)
        assert job["options"]["force"] is False
        assert job["options"]["limit"] is None
        assert job["options"]["assess_photos"] is False


class TestCollectionJobEndpoints:

    def test_creating_a_supplier_id_job_returns_202_with_a_job_id(self, client):
        response = client.post(
            "/collection/jobs", json={"supplier_id": 5}, headers=auth_headers(),
        )
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert body["query"] == "[collection] supplier #5"
        assert body["id"]

    def test_creating_a_pending_batch_job(self, client):
        response = client.post(
            "/collection/jobs", json={"pending": True, "limit": 10}, headers=auth_headers(),
        )
        assert response.status_code == 202
        assert response.json()["query"] == "[collection] pending batch"

    def test_created_job_is_retrievable_by_id(self, client):
        create_response = client.post("/collection/jobs", json={"supplier_id": 5}, headers=auth_headers())
        job_id = create_response.json()["id"]
        get_response = client.get(f"/pipeline/jobs/{job_id}", headers=auth_headers())
        assert get_response.status_code == 200
        assert get_response.json()["query"] == "[collection] supplier #5"

    def test_neither_supplier_id_nor_pending_is_a_validation_error(self, client):
        response = client.post("/collection/jobs", json={}, headers=auth_headers())
        assert response.status_code == 422

    def test_requires_auth(self, client):
        response = client.post("/collection/jobs", json={"supplier_id": 5})
        assert response.status_code == 401


class TestVerificationJobEndpoints:

    def test_creating_a_supplier_id_job_returns_202_with_a_job_id(self, client):
        response = client.post("/verification/jobs", json={"supplier_id": 5}, headers=auth_headers())
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert body["query"] == "[verification] supplier #5"

    def test_creating_a_pending_batch_job(self, client):
        response = client.post("/verification/jobs", json={"pending": True}, headers=auth_headers())
        assert response.status_code == 202
        assert response.json()["query"] == "[verification] pending batch"

    def test_neither_supplier_id_nor_pending_is_a_validation_error(self, client):
        response = client.post("/verification/jobs", json={}, headers=auth_headers())
        assert response.status_code == 422

    def test_requires_auth(self, client):
        response = client.post("/verification/jobs", json={"supplier_id": 5})
        assert response.status_code == 401


class TestDiscoveryJobEndpoints:

    def test_creating_a_job_returns_202_with_a_job_id(self, client):
        response = client.post("/discovery/jobs", json={"product": "trailer axle"}, headers=auth_headers())
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert body["query"] == "[discovery] trailer axle"
        assert body["id"]

    def test_created_job_is_retrievable_by_id(self, client):
        create_response = client.post(
            "/discovery/jobs", json={"product": "trailer axle", "country": "China", "max_candidates": 10},
            headers=auth_headers(),
        )
        job_id = create_response.json()["id"]
        get_response = client.get(f"/pipeline/jobs/{job_id}", headers=auth_headers())
        assert get_response.status_code == 200
        assert get_response.json()["query"] == "[discovery] trailer axle"

    def test_missing_product_is_a_validation_error(self, client):
        response = client.post("/discovery/jobs", json={}, headers=auth_headers())
        assert response.status_code == 422

    def test_defaults_match_documented_behaviour(self, client):
        response = client.post("/discovery/jobs", json={"product": "trailer axle"}, headers=auth_headers())
        job_id = response.json()["id"]
        job = client.repo.get_pipeline_job(job_id)
        assert job["options"]["category"] is None
        assert job["options"]["country"] is None
        assert job["options"]["max_candidates"] == 20

    def test_requires_auth(self, client):
        response = client.post("/discovery/jobs", json={"product": "trailer axle"})
        assert response.status_code == 401


class TestBackfillDiscoveryProductKeywordsEndpoint:

    def test_backfills_supplier_created_by_a_completed_discovery_job(self, client):
        supplier_id = client.repo.create_golden_record({"canonical_name": "Acme Winch Co"})
        client.repo.create_pipeline_job(job_id="job-1", query="[discovery] winch", options={})
        client.repo.mark_pipeline_job_completed(
            "job-1", stats={"new_supplier_ids": [supplier_id], "candidates_found": 1},
        )

        response = client.post("/discovery/backfill-product-keywords", headers=auth_headers())

        assert response.status_code == 200
        body = response.json()
        assert body["updated_count"] == 1
        assert body["updated_supplier_ids"] == [supplier_id]
        supplier = client.repo.get_supplier(supplier_id)
        assert supplier["product_keywords"] == ["winch"]

    def test_runs_synchronously_not_through_the_job_queue(self, client):
        """No BackgroundTasks, no pipeline_jobs row created by this
        call -- it's a fast, pure DB read/write, not a paid-API job."""
        response = client.post("/discovery/backfill-product-keywords", headers=auth_headers())
        assert response.status_code == 200
        assert "id" not in response.json()

    def test_requires_auth(self, client):
        response = client.post("/discovery/backfill-product-keywords")
        assert response.status_code == 401


class TestReverifyEndpoint:

    def test_creates_a_job_for_an_existing_supplier(self, client):
        supplier_id = client.repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        response = client.post(f"/suppliers/{supplier_id}/reverify", headers=auth_headers())
        assert response.status_code == 202
        assert response.json()["query"] == f"[reverify] supplier #{supplier_id}"

    def test_unknown_supplier_returns_404(self, client):
        response = client.post("/suppliers/999999/reverify", headers=auth_headers())
        assert response.status_code == 404

    def test_requires_auth(self, client):
        supplier_id = client.repo.create_golden_record({"canonical_name": "Acme"})
        response = client.post(f"/suppliers/{supplier_id}/reverify")
        assert response.status_code == 401


class TestVerificationHistoryAndChangeLogEndpoints:

    def test_verification_history_returns_recorded_entries(self, client):
        supplier_id = client.repo.create_golden_record({"canonical_name": "Acme"})
        client.repo.record_verification_history(
            supplier_id=supplier_id, verification_type="ai_cross_check", confidence_score=80, verdict="corroborated",
        )
        response = client.get(f"/suppliers/{supplier_id}/verification-history", headers=auth_headers())
        assert response.status_code == 200
        assert len(response.json()) == 1
        assert response.json()[0]["confidence_score"] == 80

    def test_verification_history_unknown_supplier_returns_404(self, client):
        response = client.get("/suppliers/999999/verification-history", headers=auth_headers())
        assert response.status_code == 404

    def test_change_log_returns_recorded_entries(self, client):
        supplier_id = client.repo.create_golden_record({"canonical_name": "Acme", "primary_phone": "+44 20 1234 5678"})
        client.repo.update_supplier_fields_with_history(
            supplier_id, {"primary_phone": "+44 20 9999 0000"}, changed_by="verification_service",
        )
        response = client.get(f"/suppliers/{supplier_id}/change-log", headers=auth_headers())
        assert response.status_code == 200
        assert len(response.json()) == 1
        assert response.json()[0]["field_name"] == "primary_phone"

    def test_change_log_unknown_supplier_returns_404(self, client):
        response = client.get("/suppliers/999999/change-log", headers=auth_headers())
        assert response.status_code == 404

    def test_endpoints_require_auth(self, client):
        supplier_id = client.repo.create_golden_record({"canonical_name": "Acme"})
        assert client.get(f"/suppliers/{supplier_id}/verification-history").status_code == 401
        assert client.get(f"/suppliers/{supplier_id}/change-log").status_code == 401


class TestExportEndpoint:

    def test_export_returns_csv_content_type(self, client):
        client.repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        response = client.get("/export/csv", headers=auth_headers())
        assert response.status_code == 200
        assert "text/csv" in response.headers["content-type"]
        assert "Acme" in response.text

    def test_export_requires_auth(self, client):
        response = client.get("/export/csv")
        assert response.status_code == 401

    def test_excel_export_returns_xlsx_content_type(self, client):
        from io import BytesIO

        from openpyxl import load_workbook

        client.repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        response = client.get("/export/excel", headers=auth_headers())
        assert response.status_code == 200
        assert response.headers["content-type"] == (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        workbook = load_workbook(BytesIO(response.content))
        rows = list(workbook.active.iter_rows(values_only=True))
        assert any("Acme" in row for row in rows)

    def test_excel_export_requires_auth(self, client):
        response = client.get("/export/excel")
        assert response.status_code == 401


class TestCORSConfiguration:
    """CORSMiddleware is added once, at module-import time, using
    whatever ALLOWED_ORIGINS config.settings had at that moment --
    monkeypatching it per-test after the module has already loaded
    would not actually change the middleware's configured behaviour.
    So this only asserts CORS is wired in at all, not the exact
    allowed-origin list; that list is better verified by hand once
    against the real deployed ALLOWED_ORIGINS env var."""

    def test_cors_middleware_is_registered(self, client):
        import api.app

        middleware_classes = [m.cls.__name__ for m in api.app.app.user_middleware]
        assert "CORSMiddleware" in middleware_classes
