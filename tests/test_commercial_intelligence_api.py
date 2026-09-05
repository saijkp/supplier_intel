"""
tests/test_commercial_intelligence_api.py

Tests for the buyer-profile, commercial-compatibility-search, and
procurement-outcome endpoints added to api/app.py. Same TestClient
pattern as tests/test_api.py -- see that file's own fixture docstring
for why dependency_overrides is used instead of env-var monkeypatching,
and why the background-task executor is mocked.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-token-456"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import api.app
    import api.auth

    monkeypatch.setattr(api.auth, "API_ACCESS_TOKEN", TOKEN)

    db_path = tmp_path / "test_commercial_api.db"
    from storage.database import initialise_schema
    from storage.repository import SupplierRepository

    initialise_schema(db_path)
    test_repo = SupplierRepository(db_path=db_path)
    api.app.app.dependency_overrides[api.app.get_repo] = lambda: test_repo
    # dependency_overrides only covers routes' own `Depends(get_repo)` --
    # lifespan's orphaned-job sweep calls get_repo() directly, never
    # going through FastAPI's DI resolution, so it needs its own
    # monkeypatch or it would hit the real default DB_PATH on every
    # test that uses `with TestClient(...)` below.
    monkeypatch.setattr(api.app, "get_repo", lambda: test_repo)

    def fake_run_pipeline_job(job_id, query, options):
        test_repo.mark_pipeline_job_running(job_id)
        test_repo.mark_pipeline_job_completed(job_id, stats={"scraped": 0})

    monkeypatch.setattr(api.app, "run_pipeline_job", fake_run_pipeline_job)

    with TestClient(api.app.app) as test_client:
        test_client.repo = test_repo
        yield test_client

    api.app.app.dependency_overrides.clear()


def auth_headers():
    return {"Authorization": f"Bearer {TOKEN}"}


class TestBuyerProfileCRUD:

    def test_create_returns_201_with_the_created_profile(self, client):
        response = client.post(
            "/buyer-profiles",
            json={"name": "UK OEM Buyer", "destination_country": "United Kingdom",
                  "required_capabilities": ["iso 9001"], "preferred_incoterm": "ddp shipping"},
            headers=auth_headers(),
        )
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "UK OEM Buyer"
        assert body["required_capabilities"] == ["iso 9001"]
        assert body["id"]

    def test_create_requires_auth(self, client):
        response = client.post("/buyer-profiles", json={"name": "Test"})
        assert response.status_code == 401

    def test_duplicate_name_returns_409_not_500(self, client):
        client.post("/buyer-profiles", json={"name": "Dup Profile"}, headers=auth_headers())
        response = client.post("/buyer-profiles", json={"name": "Dup Profile"}, headers=auth_headers())
        assert response.status_code == 409

    def test_get_by_id(self, client):
        create = client.post("/buyer-profiles", json={"name": "Gettable"}, headers=auth_headers())
        profile_id = create.json()["id"]
        response = client.get(f"/buyer-profiles/{profile_id}", headers=auth_headers())
        assert response.status_code == 200
        assert response.json()["name"] == "Gettable"

    def test_get_nonexistent_returns_404(self, client):
        response = client.get("/buyer-profiles/999999", headers=auth_headers())
        assert response.status_code == 404

    def test_list_returns_all_created_profiles(self, client):
        client.post("/buyer-profiles", json={"name": "Profile A"}, headers=auth_headers())
        client.post("/buyer-profiles", json={"name": "Profile B"}, headers=auth_headers())
        response = client.get("/buyer-profiles", headers=auth_headers())
        assert len(response.json()) == 2

    def test_delete_removes_the_profile(self, client):
        create = client.post("/buyer-profiles", json={"name": "To Delete"}, headers=auth_headers())
        profile_id = create.json()["id"]
        delete_response = client.delete(f"/buyer-profiles/{profile_id}", headers=auth_headers())
        assert delete_response.status_code == 204
        get_response = client.get(f"/buyer-profiles/{profile_id}", headers=auth_headers())
        assert get_response.status_code == 404

    def test_delete_nonexistent_returns_404(self, client):
        response = client.delete("/buyer-profiles/999999", headers=auth_headers())
        assert response.status_code == 404


class TestCommercialCompatibilitySearch:

    def test_search_against_a_profile_returns_both_scores(self, client):
        supplier_id = client.repo.create_golden_record({
            "canonical_name": "Acme Co", "country": "United Kingdom", "domain": "acme.example.com",
            "product_keywords": ["wheel hub"],
        })
        client.repo.update_supplier_fields(supplier_id, {"is_manufacturer": True})
        create = client.post(
            "/buyer-profiles", json={"name": "Search Profile", "destination_country": "United Kingdom"},
            headers=auth_headers(),
        )
        profile_id = create.json()["id"]

        response = client.get(f"/buyer-profiles/{profile_id}/search", headers=auth_headers())
        assert response.status_code == 200
        results = response.json()
        assert len(results) == 1
        assert "composite_score" in results[0]
        assert "commercial_compatibility_score" in results[0]

    def test_search_against_nonexistent_profile_returns_404(self, client):
        response = client.get("/buyer-profiles/999999/search", headers=auth_headers())
        assert response.status_code == 404

    def test_search_excludes_suppliers_missing_a_required_capability(self, client):
        client.repo.create_golden_record({
            "canonical_name": "No Cert Co", "country": "United Kingdom", "domain": "nocert.example.com",
        })
        create = client.post(
            "/buyer-profiles",
            json={"name": "Cert Required Profile", "required_capabilities": ["iso 9001"]},
            headers=auth_headers(),
        )
        profile_id = create.json()["id"]
        response = client.get(f"/buyer-profiles/{profile_id}/search", headers=auth_headers())
        assert response.json() == []


class TestProcurementOutcomes:

    def test_record_outcome_returns_201(self, client):
        supplier_id = client.repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        response = client.post(
            f"/suppliers/{supplier_id}/procurement-outcomes",
            json={"outcome": "rfq_submitted"}, headers=auth_headers(),
        )
        assert response.status_code == 201
        assert response.json()["outcome"] == "rfq_submitted"

    def test_record_outcome_for_nonexistent_supplier_returns_404(self, client):
        response = client.post(
            "/suppliers/999999/procurement-outcomes", json={"outcome": "rfq_submitted"}, headers=auth_headers(),
        )
        assert response.status_code == 404

    def test_get_outcomes_returns_recorded_history(self, client):
        supplier_id = client.repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        client.post(f"/suppliers/{supplier_id}/procurement-outcomes", json={"outcome": "nda_signed"}, headers=auth_headers())
        client.post(f"/suppliers/{supplier_id}/procurement-outcomes", json={"outcome": "quoted"}, headers=auth_headers())

        response = client.get(f"/suppliers/{supplier_id}/procurement-outcomes", headers=auth_headers())
        assert response.status_code == 200
        assert len(response.json()) == 2

    def test_get_outcomes_for_nonexistent_supplier_returns_404(self, client):
        response = client.get("/suppliers/999999/procurement-outcomes", headers=auth_headers())
        assert response.status_code == 404

    def test_outcome_linked_to_a_buyer_profile_is_recorded(self, client):
        supplier_id = client.repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        profile_response = client.post("/buyer-profiles", json={"name": "Linked Profile"}, headers=auth_headers())
        profile_id = profile_response.json()["id"]

        response = client.post(
            f"/suppliers/{supplier_id}/procurement-outcomes",
            json={"outcome": "quoted", "buyer_profile_id": profile_id, "notes": "Good initial response"},
            headers=auth_headers(),
        )
        assert response.json()["buyer_profile_id"] == profile_id
        assert response.json()["notes"] == "Good initial response"
