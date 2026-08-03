"""
tests/test_verification_service.py

Tests for verification_ai/verification_service.py -- the orchestrator
wiring CrossChecker -> ConfidenceScorer -> NarrativeGenerator to a real
storage.repository.SupplierRepository. Uses fakes for all four
injectable dependencies (same DI-for-testability pattern as
CollectionService's own tests) -- this is orchestration/repo-recording
coverage, not a re-test of cross_checker/confidence_scorer/
narrative_generator's own internals (those have their own test files).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from storage.database import initialise_schema
from storage.repository import SupplierRepository
from verification_ai.cross_checker import CrossCheckResult, SubCheckResult
from verification_ai.narrative_generator import NarrativeResult
from verification_ai.verification_service import VerificationService


class FakeCrossChecker:
    def __init__(self, result=None):
        self._result = result or CrossCheckResult(
            sub_checks=[SubCheckResult(name="facility_address", verdict=True, detail="confirmed")],
        )
        self.calls = []

    def run_checks(self, supplier, collected_pages=None, capability_findings=None):
        self.calls.append((supplier["id"], collected_pages, capability_findings))
        return self._result


class FakeConfidenceScorer:
    def __init__(self, score=75):
        self._score = score

    def score(self, cross_check_result):
        return self._score


class FakeNarrativeGenerator:
    def __init__(self, result=NarrativeResult(
        summary="A solid manufacturer.", strengths=["Verified address"], risks=[],
        suitable_customer_types=["OEM buyers"], model_used="gpt-4o-mini",
    )):
        self._result = result

    def generate(self, supplier, cross_check_result, confidence_score):
        return self._result


class FakeOwnWebsiteScraper:
    def __init__(self, pages=None, success=True):
        self._pages = pages or []
        self._success = success
        self.calls = []

    def fetch(self, domain):
        self.calls.append(domain)
        return SimpleNamespace(success=self._success, pages=self._pages)


class FakeCollectionService:
    def __init__(self):
        self.calls = []

    def collect(self, supplier_id):
        self.calls.append(supplier_id)
        return {"supplier_id": supplier_id, "status": "success", "pages_visited": 2}


@pytest.fixture()
def repo(tmp_path):
    db_path = tmp_path / "test.db"
    initialise_schema(db_path)
    return SupplierRepository(db_path=db_path)


def _service(repo, **overrides):
    return VerificationService(
        repo=repo,
        cross_checker=overrides.get("cross_checker", FakeCrossChecker()),
        confidence_scorer=overrides.get("confidence_scorer", FakeConfidenceScorer()),
        narrative_generator=overrides.get("narrative_generator", FakeNarrativeGenerator()),
        own_website_scraper=overrides.get("own_website_scraper", FakeOwnWebsiteScraper()),
    )


class TestVerifySingleSupplier:

    def test_writes_confidence_score_and_timestamps(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme"})
        service = _service(repo, confidence_scorer=FakeConfidenceScorer(score=82))

        outcome = service.verify(supplier_id)

        assert outcome["confidence_score"] == 82
        assert outcome["verdict"] == "corroborated"
        supplier = repo.get_supplier(supplier_id)
        assert supplier["ai_confidence_score"] == 82
        assert supplier["ai_confidence_assessed_at"] is not None
        assert supplier["last_verified"] is not None

    def test_writes_narrative_fields_when_generated(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme"})
        service = _service(repo)

        service.verify(supplier_id)

        supplier = repo.get_supplier(supplier_id)
        assert supplier["ai_summary"] == "A solid manufacturer."
        assert supplier["ai_strengths"] == ["Verified address"]
        assert supplier["ai_suitable_customer_types"] == ["OEM buyers"]
        assert supplier["ai_verification_model"] == "gpt-4o-mini"

    def test_narrative_failure_still_writes_confidence_score(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme"})
        service = _service(repo, narrative_generator=FakeNarrativeGenerator(result=None))

        outcome = service.verify(supplier_id)

        assert outcome["narrative_generated"] is False
        supplier = repo.get_supplier(supplier_id)
        assert supplier["ai_confidence_score"] is not None
        assert supplier["ai_summary"] is None

    def test_writes_one_verification_history_row(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme"})
        service = _service(repo, confidence_scorer=FakeConfidenceScorer(score=90))

        service.verify(supplier_id)

        history = repo.get_verification_history(supplier_id)
        assert len(history) == 1
        assert history[0]["verification_type"] == "ai_cross_check"
        assert history[0]["confidence_score"] == 90
        assert history[0]["verdict"] == "corroborated"
        assert history[0]["evidence_json"]["sub_checks"]

    def test_raises_for_unknown_supplier(self, repo):
        service = _service(repo)
        with pytest.raises(ValueError):
            service.verify(999999)

    def test_domain_supplier_fetches_own_site_for_cross_checking(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        scraper = FakeOwnWebsiteScraper(pages=[SimpleNamespace(url="https://acme.example.com", text="hi")])
        cross_checker = FakeCrossChecker()
        service = _service(repo, own_website_scraper=scraper, cross_checker=cross_checker)

        service.verify(supplier_id)

        assert scraper.calls == ["acme.example.com"]
        assert cross_checker.calls[0][1] == scraper._pages  # collected_pages passed through

    def test_domain_less_supplier_skips_own_site_fetch(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme"})
        scraper = FakeOwnWebsiteScraper()
        service = _service(repo, own_website_scraper=scraper)

        service.verify(supplier_id)

        assert scraper.calls == []

    def test_own_site_fetch_failure_does_not_abort_verification(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        scraper = FakeOwnWebsiteScraper(success=False)
        service = _service(repo, own_website_scraper=scraper)

        outcome = service.verify(supplier_id)  # must not raise
        assert outcome["confidence_score"] is not None

    def test_does_not_overwrite_is_manufacturer_fields(self, repo):
        """VerificationService must never write is_manufacturer/
        manufacturer_confidence/manufacturer_signals -- those belong to
        the separately-orchestrated run_manufacturer_assessment_only
        pipeline stage."""
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme", "is_manufacturer": True, "manufacturer_confidence": 88,
        })
        service = _service(repo)

        service.verify(supplier_id)

        supplier = repo.get_supplier(supplier_id)
        assert supplier["is_manufacturer"] == 1
        assert supplier["manufacturer_confidence"] == 88


class TestVerifyPending:

    def test_processes_every_eligible_supplier(self, repo):
        id_a = repo.create_golden_record({"canonical_name": "A Co"})
        id_b = repo.create_golden_record({"canonical_name": "B Co"})
        service = _service(repo)

        stats = service.verify_pending(limit=10)

        assert stats["attempted"] == 2
        assert stats["succeeded"] == 2
        assert stats["failed"] == 0
        assert {repo.get_supplier(id_a)["ai_confidence_score"], repo.get_supplier(id_b)["ai_confidence_score"]} == {75}

    def test_already_assessed_suppliers_are_skipped_without_force(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme"})
        service = _service(repo)
        service.verify(supplier_id)

        stats = service.verify_pending(limit=10)

        assert stats["attempted"] == 0

    def test_force_reprocesses_already_assessed_suppliers(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme"})
        service = _service(repo)
        service.verify(supplier_id)

        stats = service.verify_pending(limit=10, force=True)

        assert stats["attempted"] == 1

    def test_one_failure_does_not_abort_the_batch(self, repo, monkeypatch):
        id_a = repo.create_golden_record({"canonical_name": "A Co"})
        id_b = repo.create_golden_record({"canonical_name": "B Co"})
        service = _service(repo)

        call_count = {"n": 0}
        original = service._verify_one

        def flaky(supplier):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("boom")
            return original(supplier)

        monkeypatch.setattr(service, "_verify_one", flaky)

        stats = service.verify_pending(limit=10)  # must not raise
        assert stats["attempted"] == 2
        assert stats["succeeded"] == 1
        assert stats["failed"] == 1


class TestReverify:

    def test_reverify_collects_then_verifies(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        service = _service(repo)
        fake_collection = FakeCollectionService()

        outcome = service.reverify(supplier_id, collection_service=fake_collection)

        assert fake_collection.calls == [supplier_id]
        assert outcome["collection"]["status"] == "success"
        assert outcome["verification"]["confidence_score"] == 75

    def test_second_reverify_with_unchanged_narrative_only_updates_timestamps(self, repo):
        """Re-running with genuinely identical narrative/confidence
        content should only log the always-refreshing timestamp fields
        as changed, not re-log ai_summary/ai_confidence_score as
        'changed' when they're actually the same value both times."""
        supplier_id = repo.create_golden_record({"canonical_name": "Acme", "domain": "acme.example.com"})
        service = _service(repo)
        fake_collection = FakeCollectionService()

        service.reverify(supplier_id, collection_service=fake_collection)
        log_after_first = repo.get_supplier_change_log(supplier_id)

        service.reverify(supplier_id, collection_service=fake_collection)
        log_after_second = repo.get_supplier_change_log(supplier_id)

        new_entries = log_after_second[: len(log_after_second) - len(log_after_first)]
        new_fields = {entry["field_name"] for entry in new_entries}
        assert "ai_confidence_assessed_at" in new_fields
        assert "last_verified" in new_fields
        assert "ai_confidence_score" not in new_fields  # unchanged (75 both times) -- not re-logged
        assert "ai_summary" not in new_fields  # unchanged -- not re-logged
