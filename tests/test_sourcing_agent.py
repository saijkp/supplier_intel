"""
tests/test_sourcing_agent.py

Tests for sourcing/sourcing_agent.py -- the discover->collect->verify->
qualify loop. Uses fakes for discovery_service/collection_service/
verification_service/capability_extractor/own_website_scraper/
brief_parser/dossier_generator (same DI/fake pattern as every other
orchestrator test in this codebase), against a REAL temp-file
SupplierRepository so dedup/write behaviour is genuinely exercised, not
just asserted by inspection.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from sourcing.brief_parser import BriefParsingError
from sourcing.schemas import SourcingDossier, StructuredBrief
from sourcing.sourcing_agent import SourcingAgentService
from storage.database import initialise_schema
from storage.repository import SupplierRepository


class FakeDiscoveryService:
    """Creates real supplier rows (via the real repo) so downstream
    get_supplier/verify/collect calls see genuine data -- mirrors how
    the real DiscoveryService's resolve_and_store already works."""

    def __init__(self, repo, suppliers_by_country=None):
        self.repo = repo
        self._suppliers_by_country = suppliers_by_country or {}
        self.calls = []

    def discover(self, product, country=None, max_candidates=20, application=None, key_specifications=None):
        self.calls.append({
            "product": product, "country": country, "max_candidates": max_candidates,
            "application": application, "key_specifications": key_specifications,
        })
        names = self._suppliers_by_country.get(country, [])
        new_ids = []
        for name in names:
            domain = name.lower().replace(" ", "") + ".example.com"
            supplier_id = self.repo.create_golden_record({
                "canonical_name": name, "country": country, "domain": domain,
            })
            new_ids.append(supplier_id)
        return SimpleNamespace(new_supplier_ids=new_ids)


class FakeCollectionService:
    def __init__(self):
        self.calls = []

    def collect(self, supplier_id):
        self.calls.append(supplier_id)
        return {"supplier_id": supplier_id, "status": "success"}


class FakeVerificationService:
    """Records is_manufacturer + a verification_history row (with
    empty evidence by default) so _latest_cross_check_result has
    something real to read back, same as the real VerificationService
    would leave behind."""

    def __init__(self, repo, is_manufacturer_by_id=None, raise_for_id=None):
        self.repo = repo
        self._is_manufacturer_by_id = is_manufacturer_by_id or {}
        self._raise_for_id = raise_for_id
        self.calls = []

    def verify(self, supplier_id):
        self.calls.append(supplier_id)
        if self._raise_for_id == supplier_id:
            raise RuntimeError("verification exploded")
        is_mfr = self._is_manufacturer_by_id.get(supplier_id, True)
        self.repo.update_supplier_fields(supplier_id, {"is_manufacturer": is_mfr})
        self.repo.record_verification_history(
            supplier_id=supplier_id, verification_type="ai_cross_check",
            evidence={"sub_checks": [], "inconsistencies": []},
        )
        return {"supplier_id": supplier_id, "confidence_score": 70}


class FakeCapabilityExtractor:
    def __init__(self, findings=None):
        self._findings = findings or []

    def extract_from_pages(self, pages):
        return self._findings


class FakeOwnWebsiteScraper:
    def __init__(self, success=True, pages=None):
        self._success = success
        self._pages = pages

    def fetch(self, domain):
        pages = self._pages if self._pages is not None else [SimpleNamespace(url=f"https://{domain}", text="some page text")]
        return SimpleNamespace(
            success=self._success, pages=pages,
            error=None if self._success else "fetch failed",
        )


class FakeDossierGenerator:
    def __init__(self, response="default"):
        # "default" sentinel means "return a real dossier"; pass None explicitly to simulate LLM failure
        self._response = response
        self.calls = []

    def generate(self, supplier, brief, cross_check_result, capability_findings=None):
        self.calls.append(supplier["id"])
        if self._response == "default":
            return SourcingDossier(
                oem_odm_capability="In-house ODM confirmed.",
                factory_manufacturing_processes="CNC machining confirmed.",
                engineering_testing_capability="No evidence available to assess this.",
                export_experience="No evidence available to assess this.",
                annual_volume_suitability="No evidence available to assess this.",
                payment_terms_assessment="No evidence available to assess this.",
                verification_status="partially verified",
            )
        return self._response


class FakeBriefParser:
    def __init__(self, brief=None, error=None):
        self._brief = brief
        self._error = error

    def parse(self, brief_text):
        if self._error:
            raise self._error
        return self._brief


class FakeLLMClient:
    """Default response=None means "no address found" -- attempt_
    address_extraction() degrades to "skipped" cleanly on this, same as
    a real LLMClient() with no API key configured (never raises, see
    llm/client.py's own contract) -- so tests that don't care about
    address extraction get a fast, deterministic no-op instead of
    depending on a real network call's failure timing."""
    def __init__(self, response=None):
        self._response = response
        self.calls = []

    def complete_json(self, system_prompt, user_prompt, **kwargs):
        self.calls.append((system_prompt, user_prompt))
        return self._response


class FakeManufacturerVerifier:
    """Default result has is_manufacturer=None (no signal) -- a no-op
    write (update_manufacturer_verification skips None fields), so
    tests that don't care about manufacturer assessment are unaffected
    by this step running."""
    def __init__(self, result=None):
        self._result = result or {"is_manufacturer": None, "manufacturer_confidence": 50, "manufacturer_signals": []}
        self.calls = []

    def assess(self, supplier):
        self.calls.append(supplier["id"])
        return dict(self._result)


class FakeQichachaClient:
    """app_key/app_secret both None by default -- _assess_manufacturer_
    status skips the Qichacha call entirely unless a test explicitly
    configures credentials, same as the real QichachaVerifier with no
    keys set."""
    def __init__(self, app_key=None, app_secret=None, verification=None):
        self.app_key = app_key
        self.app_secret = app_secret
        self._verification = verification or {}
        self.calls = []

    def verify(self, uscc):
        self.calls.append(uscc)
        return dict(self._verification)


class FakeTradePipeline:
    def __init__(self, raise_error=None, manufacturer_verifier=None, qichacha=None):
        self._raise_error = raise_error
        self.calls = []
        self.manufacturer_verifier = manufacturer_verifier or FakeManufacturerVerifier()
        self.qichacha = qichacha or FakeQichachaClient()

    def run(self, product, **kwargs):
        self.calls.append({"product": product, **kwargs})
        if self._raise_error:
            raise self._raise_error
        return {}


@pytest.fixture()
def repo(tmp_path):
    db_path = tmp_path / "test.db"
    initialise_schema(db_path)
    return SupplierRepository(db_path=db_path)


def _brief(**overrides):
    defaults = dict(product="winch", target_count=1, countries=[])
    defaults.update(overrides)
    return StructuredBrief(**defaults)


def _service(repo, *, suppliers_by_country=None, is_manufacturer_by_id=None,
             brief=None, dossier_response="default", capability_findings=None,
             raise_verify_for_id=None, trade_pipeline=None, parallel_workers=None,
             llm_client=None, manufacturer_verifier=None, qichacha_client=None):
    kwargs = {}
    if parallel_workers is not None:
        kwargs["parallel_workers"] = parallel_workers
    return SourcingAgentService(
        repo=repo,
        discovery_service=FakeDiscoveryService(repo, suppliers_by_country=suppliers_by_country),
        collection_service=FakeCollectionService(),
        verification_service=FakeVerificationService(
            repo, is_manufacturer_by_id=is_manufacturer_by_id, raise_for_id=raise_verify_for_id,
        ),
        brief_parser=FakeBriefParser(brief=brief or _brief()),
        dossier_generator=FakeDossierGenerator(response=dossier_response),
        capability_extractor=FakeCapabilityExtractor(findings=capability_findings),
        own_website_scraper=FakeOwnWebsiteScraper(),
        trade_pipeline=trade_pipeline or FakeTradePipeline(),
        llm_client=llm_client or FakeLLMClient(),
        manufacturer_verifier=manufacturer_verifier,
        qichacha_client=qichacha_client,
        **kwargs,
    )


class TestBasicQualification:

    def test_qualified_manufacturer_is_recorded_with_a_dossier(self, repo):
        service = _service(repo, suppliers_by_country={None: ["Acme Winch Co"]})

        outcome = service.run("find 1 winch manufacturer")

        assert outcome.status == "completed"
        assert len(outcome.qualified_supplier_ids) == 1
        supplier_id = outcome.qualified_supplier_ids[0]
        supplier = repo.get_supplier(supplier_id)
        assert supplier["sourcing_oem_odm_notes"] == "In-house ODM confirmed."
        assert supplier["sourcing_verification_status"] == "partially verified"

        run = repo.get_sourcing_run(outcome.run_id)
        assert run["status"] == "completed"
        assert run["qualified_supplier_ids_json"] == [supplier_id]

    def test_confirmed_trader_is_excluded(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Trading Co", "domain": "acmetrading.example.com"})

        service = SourcingAgentService(
            repo=repo,
            discovery_service=_FixedIdDiscoveryService([supplier_id]),
            collection_service=FakeCollectionService(),
            verification_service=FakeVerificationService(repo, is_manufacturer_by_id={supplier_id: False}),
            brief_parser=FakeBriefParser(brief=_brief()),
            dossier_generator=FakeDossierGenerator(),
            capability_extractor=FakeCapabilityExtractor(),
            own_website_scraper=FakeOwnWebsiteScraper(),
    trade_pipeline=FakeTradePipeline(),
        )

        outcome = service.run("find 1 winch manufacturer")

        assert outcome.qualified_supplier_ids == []
        assert outcome.examined_count == 1


class _FixedIdDiscoveryService:
    """Returns a fixed list of already-created supplier ids on its
    first call, nothing on subsequent calls -- for tests that need to
    control exactly which id gets discovered."""

    def __init__(self, ids):
        self._ids = ids
        self._called = False
        self.calls = []

    def discover(self, product, country=None, max_candidates=20, application=None, key_specifications=None):
        self.calls.append({"product": product, "country": country})
        if self._called:
            return SimpleNamespace(new_supplier_ids=[])
        self._called = True
        return SimpleNamespace(new_supplier_ids=self._ids)


class TestTargetCountAndCeiling:

    def test_stops_examining_once_target_count_reached(self, repo):
        """parallel_workers=1 here specifically to test the exact
        stop-at-target boundary -- with real concurrency (parallel_workers
        > 1), a whole wave is examined before the target-reached check
        runs again, so it can overshoot by up to parallel_workers
        candidates; see test_wave_can_overshoot_target_under_concurrency
        below for that documented, intentional trade-off."""
        service = _service(
            repo, brief=_brief(target_count=1),
            suppliers_by_country={None: ["Co A", "Co B", "Co C"]},
            parallel_workers=1,
        )

        outcome = service.run("find 1 winch manufacturer")

        assert outcome.examined_count == 1
        assert len(outcome.qualified_supplier_ids) == 1

    def test_wave_can_overshoot_target_under_concurrency(self, repo):
        """Documents the intentional Phase 0 trade-off: with
        parallel_workers=3, a whole wave of up to 3 candidates is
        submitted and examined before the target-reached check runs
        again -- so a target of 1 can still examine all 3 candidates in
        a single wave, rather than stopping at exactly 1. This is the
        cost of running candidates concurrently instead of one at a
        time; all 3 still qualify here (all manufacturers), so the
        supplier list itself isn't wrong -- just the examined count."""
        service = _service(
            repo, brief=_brief(target_count=1),
            suppliers_by_country={None: ["Co A", "Co B", "Co C"]},
            parallel_workers=3,
        )

        outcome = service.run("find 1 winch manufacturer")

        assert outcome.examined_count == 3
        assert len(outcome.qualified_supplier_ids) == 3

    def test_ceiling_stops_examination_even_if_target_not_reached(self, repo):
        """target_count=2, max_multiplier=2 -> ceiling=4. None of the 5
        candidates qualify (all confirmed traders), so the run must
        stop at the ceiling (4 examined), never reaching all 5."""
        ids = []

        class AllTraderVerificationService(FakeVerificationService):
            def verify(self, supplier_id):
                self.calls.append(supplier_id)
                self.repo.update_supplier_fields(supplier_id, {"is_manufacturer": False})
                self.repo.record_verification_history(
                    supplier_id=supplier_id, verification_type="ai_cross_check",
                    evidence={"sub_checks": [], "inconsistencies": []},
                )
                return {"supplier_id": supplier_id, "confidence_score": 20}

        service = SourcingAgentService(
            repo=repo,
            discovery_service=FakeDiscoveryService(repo, suppliers_by_country={
                None: ["Co A", "Co B", "Co C", "Co D", "Co E"],
            }),
            collection_service=FakeCollectionService(),
            verification_service=AllTraderVerificationService(repo),
            brief_parser=FakeBriefParser(brief=_brief(target_count=2)),
            dossier_generator=FakeDossierGenerator(),
            capability_extractor=FakeCapabilityExtractor(),
            own_website_scraper=FakeOwnWebsiteScraper(),
    trade_pipeline=FakeTradePipeline(),
        )

        outcome = service.run("find 2 winch manufacturers", max_multiplier=2)

        assert outcome.examined_count == 4
        assert outcome.qualified_supplier_ids == []


class TestConcurrency:

    def test_candidates_within_a_wave_process_concurrently(self, repo):
        """The real point of Phase 0: candidates within one wave run
        CONCURRENTLY, not one at a time. Asserted directly by tracking
        how many verify() calls are simultaneously in flight, rather
        than an aggregate wall-clock ratio -- _process_candidate makes
        several real SQLite writes per candidate (collect, capability
        extraction, verify, dossier), and WAL's single-writer semantics
        add real, environment-dependent serialization overhead on top
        of the artificial sleep, which made a wall-clock-ratio
        assertion flaky. Directly observing overlapping in-flight calls
        is robust to that noise."""

        class SlowVerificationService(FakeVerificationService):
            def __init__(self, repo):
                super().__init__(repo)
                self.active = 0
                self.max_concurrent = 0
                self._lock = threading.Lock()

            def verify(self, supplier_id):
                with self._lock:
                    self.active += 1
                    self.max_concurrent = max(self.max_concurrent, self.active)
                try:
                    time.sleep(0.15)
                    return super().verify(supplier_id)
                finally:
                    with self._lock:
                        self.active -= 1

        slow_verification = SlowVerificationService(repo)
        service = _service(
            repo, brief=_brief(target_count=999),
            suppliers_by_country={None: [f"Co {i}" for i in range(6)]},
            parallel_workers=3,
        )
        service.verification_service = slow_verification

        outcome = service.run("find winch manufacturers")

        assert outcome.examined_count == 6
        assert slow_verification.max_concurrent >= 2  # real overlap, not one-at-a-time

    def test_examined_and_qualified_counts_are_exact_under_concurrency(self, repo):
        """Race-condition regression guard: with a larger candidate list
        and real concurrency, examined must equal exactly the number of
        candidates processed (no double-count, no skip), and qualified
        must equal exactly how many actually qualified -- both
        bookkeeping values are mutated only in the main thread (see
        _process_batch's own docstring), so this must be exact every
        time, not just "close."""
        names = [f"Co {i}" for i in range(12)]
        service = _service(
            repo, brief=_brief(target_count=999),
            suppliers_by_country={None: names},
            parallel_workers=4,
        )

        outcome = service.run("find winch manufacturers")

        assert outcome.examined_count == 12
        assert len(outcome.qualified_supplier_ids) == 12
        assert len(set(outcome.qualified_supplier_ids)) == 12  # no duplicates


class TestCountryPriority:

    def test_countries_are_tried_in_the_briefs_stated_order(self, repo):
        discovery = FakeDiscoveryService(repo, suppliers_by_country={
            "China": ["China Co"], "India": ["India Co"],
        })
        service = SourcingAgentService(
            repo=repo, discovery_service=discovery, collection_service=FakeCollectionService(),
            verification_service=FakeVerificationService(repo),
            brief_parser=FakeBriefParser(brief=_brief(target_count=5, countries=["China", "India"])),
            dossier_generator=FakeDossierGenerator(), capability_extractor=FakeCapabilityExtractor(),
            own_website_scraper=FakeOwnWebsiteScraper(),
    trade_pipeline=FakeTradePipeline(),
        )

        service.run("find winch manufacturers, China then India")

        assert [c["country"] for c in discovery.calls] == ["China", "India"]

    def test_stops_before_trying_later_countries_once_target_reached(self, repo):
        discovery = FakeDiscoveryService(repo, suppliers_by_country={
            "China": ["China Co"], "India": ["India Co"],
        })
        service = SourcingAgentService(
            repo=repo, discovery_service=discovery, collection_service=FakeCollectionService(),
            verification_service=FakeVerificationService(repo),
            brief_parser=FakeBriefParser(brief=_brief(target_count=1, countries=["China", "India"])),
            dossier_generator=FakeDossierGenerator(), capability_extractor=FakeCapabilityExtractor(),
            own_website_scraper=FakeOwnWebsiteScraper(),
    trade_pipeline=FakeTradePipeline(),
        )

        outcome = service.run("find 1 winch manufacturer, China then India")

        assert len(discovery.calls) == 1
        assert discovery.calls[0]["country"] == "China"
        assert outcome.qualified_supplier_ids[0] == repo.search_suppliers("China Co")[0]["id"]


class TestRequiredCapabilitiesFilter:

    def test_supplier_without_required_capability_is_excluded(self, repo):
        service = _service(
            repo, suppliers_by_country={None: ["Acme Winch Co"]},
            brief=_brief(required_capabilities=["iso 9001"]),
            capability_findings=[],  # nothing found on the supplier's own site
        )

        outcome = service.run("find winch manufacturers with ISO 9001")

        assert outcome.qualified_supplier_ids == []
        assert outcome.examined_count == 1

    def test_supplier_with_required_capability_qualifies(self, repo):
        finding = SimpleNamespace(
            reported_term="ISO 9001", canonical_term="iso 9001", category="standard",
            relationship="in_house", confidence=0.9, evidence="We are ISO 9001 certified.",
            source_url="https://acme.example.com",
        )
        service = _service(
            repo, suppliers_by_country={None: ["Acme Winch Co"]},
            brief=_brief(required_capabilities=["iso 9001"]),
            capability_findings=[finding],
        )

        outcome = service.run("find winch manufacturers with ISO 9001")

        assert len(outcome.qualified_supplier_ids) == 1


class TestFaultIsolation:

    def test_one_candidate_raising_does_not_abort_the_run(self, repo):
        discovery = FakeDiscoveryService(repo, suppliers_by_country={None: ["Co A", "Co B"]})
        # We need deterministic ids to target one for failure -- create them ahead via a first discover() call.
        outcome_ids = discovery.discover("winch", country=None, max_candidates=20)
        bad_id = outcome_ids.new_supplier_ids[0]

        service = SourcingAgentService(
            repo=repo, discovery_service=_FixedIdDiscoveryService(outcome_ids.new_supplier_ids),
            collection_service=FakeCollectionService(),
            verification_service=FakeVerificationService(repo, raise_for_id=bad_id),
            brief_parser=FakeBriefParser(brief=_brief(target_count=5)),
            dossier_generator=FakeDossierGenerator(), capability_extractor=FakeCapabilityExtractor(),
            own_website_scraper=FakeOwnWebsiteScraper(),
    trade_pipeline=FakeTradePipeline(),
        )

        outcome = service.run("find winch manufacturers")  # must not raise

        assert outcome.examined_count == 2
        good_id = [i for i in outcome_ids.new_supplier_ids if i != bad_id][0]
        assert outcome.qualified_supplier_ids == [good_id]


class TestBriefParsingFailure:

    def test_records_a_failed_sourcing_run_and_never_calls_discovery(self, repo):
        discovery = FakeDiscoveryService(repo)
        service = SourcingAgentService(
            repo=repo, discovery_service=discovery, collection_service=FakeCollectionService(),
            verification_service=FakeVerificationService(repo),
            brief_parser=FakeBriefParser(error=BriefParsingError("no product could be identified")),
            dossier_generator=FakeDossierGenerator(), capability_extractor=FakeCapabilityExtractor(),
            own_website_scraper=FakeOwnWebsiteScraper(),
    trade_pipeline=FakeTradePipeline(),
        )

        outcome = service.run("blah blah nothing useful")  # must not raise

        assert outcome.status == "failed"
        assert "no product could be identified" in outcome.error
        assert discovery.calls == []
        run = repo.get_sourcing_run(outcome.run_id)
        assert run["status"] == "failed"


class TestProgressCallback:

    def test_on_progress_is_called_with_running_counts(self, repo):
        events = []
        service = _service(repo, suppliers_by_country={None: ["Co A", "Co B"]}, brief=_brief(target_count=5))

        service.run("find winch manufacturers", on_progress=events.append)

        assert len(events) == 2
        assert events[0].examined == 1
        assert events[1].examined == 2
        assert events[1].qualified == 2

    def test_progress_callback_raising_does_not_abort_the_run(self, repo):
        def bad_callback(progress):
            raise RuntimeError("frontend disconnected")

        service = _service(repo, suppliers_by_country={None: ["Co A"]})

        outcome = service.run("find winch manufacturers", on_progress=bad_callback)  # must not raise

        assert outcome.status == "completed"


class TestDossierGenerationFailureStillQualifies:

    def test_supplier_still_qualifies_when_dossier_generation_fails(self, repo):
        """Qualification is based on hard filters (trader exclusion,
        required capabilities), never on whether the LLM successfully
        wrote a narrative -- an LLM outage must not silently drop
        otherwise-qualified suppliers from the results."""
        service = _service(
            repo, suppliers_by_country={None: ["Acme Winch Co"]}, dossier_response=None,
        )

        outcome = service.run("find winch manufacturers")

        assert len(outcome.qualified_supplier_ids) == 1
        supplier = repo.get_supplier(outcome.qualified_supplier_ids[0])
        assert supplier["sourcing_oem_odm_notes"] is None


class TestTradeShipmentEnrichment:

    def test_trade_pipeline_run_once_with_the_brief_product_and_no_paid_sources(self, repo):
        trade_pipeline = FakeTradePipeline()
        service = _service(
            repo, suppliers_by_country={None: ["Acme Winch Co"]},
            brief=_brief(product="winch"), trade_pipeline=trade_pipeline,
        )

        service.run("find winch manufacturers")

        assert len(trade_pipeline.calls) == 1
        call = trade_pipeline.calls[0]
        assert call["product"] == "winch"
        assert call["sources"] == ["importyeti", "volza"]
        assert call["run_verification"] is False
        assert call["run_scoring"] is False

    def test_trade_pipeline_runs_before_discovery(self, repo):
        order = []

        class RecordingTradePipeline(FakeTradePipeline):
            def run(self, product, **kwargs):
                order.append("trade")
                return super().run(product, **kwargs)

        class RecordingDiscoveryService(FakeDiscoveryService):
            def discover(self, product, **kwargs):
                order.append("discover")
                return super().discover(product, **kwargs)

        service = SourcingAgentService(
            repo=repo, discovery_service=RecordingDiscoveryService(repo, suppliers_by_country={None: ["Co A"]}),
            collection_service=FakeCollectionService(), verification_service=FakeVerificationService(repo),
            brief_parser=FakeBriefParser(brief=_brief()), dossier_generator=FakeDossierGenerator(),
            capability_extractor=FakeCapabilityExtractor(), own_website_scraper=FakeOwnWebsiteScraper(),
            trade_pipeline=RecordingTradePipeline(),
        )

        service.run("find winch manufacturers")

        assert order[0] == "trade"

    def test_trade_pipeline_failure_does_not_abort_the_run(self, repo):
        trade_pipeline = FakeTradePipeline(raise_error=RuntimeError("scraper selectors broke"))
        service = _service(
            repo, suppliers_by_country={None: ["Acme Winch Co"]}, trade_pipeline=trade_pipeline,
        )

        outcome = service.run("find winch manufacturers")  # must not raise

        assert outcome.status == "completed"
        assert len(outcome.qualified_supplier_ids) == 1

    def test_no_trade_pipeline_call_when_brief_parsing_fails(self, repo):
        trade_pipeline = FakeTradePipeline()
        service = SourcingAgentService(
            repo=repo, discovery_service=FakeDiscoveryService(repo), collection_service=FakeCollectionService(),
            verification_service=FakeVerificationService(repo),
            brief_parser=FakeBriefParser(error=BriefParsingError("no product could be identified")),
            dossier_generator=FakeDossierGenerator(), capability_extractor=FakeCapabilityExtractor(),
            own_website_scraper=FakeOwnWebsiteScraper(), trade_pipeline=trade_pipeline,
        )

        service.run("blah blah nothing useful")

        assert trade_pipeline.calls == []


def _fresh_iso(days_ago=0):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class TestExistingDatabaseFirst:

    def test_fresh_existing_supplier_skips_collection_and_verification(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme Winch Co", "product_keywords": ["winch"], "is_manufacturer": True,
        })
        repo.update_supplier_fields(supplier_id, {"last_verified": _fresh_iso(days_ago=1)})
        repo.record_verification_history(
            supplier_id=supplier_id, verification_type="ai_cross_check",
            evidence={"sub_checks": [], "inconsistencies": []},
        )
        discovery = FakeDiscoveryService(repo)  # would create nothing new anyway
        collection = FakeCollectionService()
        verification = FakeVerificationService(repo)
        service = SourcingAgentService(
            repo=repo, discovery_service=discovery, collection_service=collection,
            verification_service=verification, brief_parser=FakeBriefParser(brief=_brief(product="winch")),
            dossier_generator=FakeDossierGenerator(), capability_extractor=FakeCapabilityExtractor(),
            own_website_scraper=FakeOwnWebsiteScraper(), trade_pipeline=FakeTradePipeline(),
        )

        outcome = service.run("find winch manufacturers")

        assert outcome.qualified_supplier_ids == [supplier_id]
        assert collection.calls == []
        assert verification.calls == []
        assert discovery.calls == []  # target already met from the existing database alone

    def test_stale_existing_supplier_is_recollected_and_reverified(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme Winch Co", "product_keywords": ["winch"], "domain": "acme.example.com",
        })
        repo.update_supplier_fields(supplier_id, {"last_verified": _fresh_iso(days_ago=90)})
        collection = FakeCollectionService()
        verification = FakeVerificationService(repo)
        service = SourcingAgentService(
            repo=repo, discovery_service=FakeDiscoveryService(repo), collection_service=collection,
            verification_service=verification, brief_parser=FakeBriefParser(brief=_brief(product="winch")),
            dossier_generator=FakeDossierGenerator(), capability_extractor=FakeCapabilityExtractor(),
            own_website_scraper=FakeOwnWebsiteScraper(), trade_pipeline=FakeTradePipeline(),
        )

        service.run("find winch manufacturers")

        assert collection.calls == [supplier_id]
        assert verification.calls == [supplier_id]

    def test_never_verified_existing_supplier_is_treated_as_stale(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Winch Co", "product_keywords": ["winch"]})
        collection = FakeCollectionService()
        service = SourcingAgentService(
            repo=repo, discovery_service=FakeDiscoveryService(repo), collection_service=collection,
            verification_service=FakeVerificationService(repo), brief_parser=FakeBriefParser(brief=_brief(product="winch")),
            dossier_generator=FakeDossierGenerator(), capability_extractor=FakeCapabilityExtractor(),
            own_website_scraper=FakeOwnWebsiteScraper(), trade_pipeline=FakeTradePipeline(),
        )

        service.run("find winch manufacturers")

        assert collection.calls == [supplier_id]

    def test_existing_confirmed_trader_is_excluded_without_refresh(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme Trading Co", "product_keywords": ["winch"], "is_manufacturer": False,
        })
        repo.update_supplier_fields(supplier_id, {"last_verified": _fresh_iso(days_ago=1)})
        service = SourcingAgentService(
            repo=repo, discovery_service=FakeDiscoveryService(repo), collection_service=FakeCollectionService(),
            verification_service=FakeVerificationService(repo), brief_parser=FakeBriefParser(brief=_brief(product="winch")),
            dossier_generator=FakeDossierGenerator(), capability_extractor=FakeCapabilityExtractor(),
            own_website_scraper=FakeOwnWebsiteScraper(), trade_pipeline=FakeTradePipeline(),
        )

        outcome = service.run("find winch manufacturers")

        assert outcome.qualified_supplier_ids == []
        assert outcome.examined_count == 1

    def test_shortfall_falls_back_to_fresh_discovery(self, repo):
        """Only one existing match for a target of 3 -- the other 2
        must come from fresh AI Discovery, and examined/qualified must
        correctly account for candidates from BOTH phases."""
        existing_id = repo.create_golden_record({
            "canonical_name": "Acme Winch Co", "product_keywords": ["winch"], "is_manufacturer": True,
        })
        repo.update_supplier_fields(existing_id, {"last_verified": _fresh_iso(days_ago=1)})
        repo.record_verification_history(
            supplier_id=existing_id, verification_type="ai_cross_check",
            evidence={"sub_checks": [], "inconsistencies": []},
        )
        discovery = FakeDiscoveryService(repo, suppliers_by_country={None: ["New Co A", "New Co B"]})
        service = SourcingAgentService(
            repo=repo, discovery_service=discovery, collection_service=FakeCollectionService(),
            verification_service=FakeVerificationService(repo),
            brief_parser=FakeBriefParser(brief=_brief(product="winch", target_count=3)),
            dossier_generator=FakeDossierGenerator(), capability_extractor=FakeCapabilityExtractor(),
            own_website_scraper=FakeOwnWebsiteScraper(), trade_pipeline=FakeTradePipeline(),
        )

        outcome = service.run("find 3 winch manufacturers")

        assert outcome.examined_count == 3  # 1 existing + 2 freshly discovered
        assert len(outcome.qualified_supplier_ids) == 3
        assert existing_id in outcome.qualified_supplier_ids
        assert len(discovery.calls) == 1

    def test_existing_database_phase_respects_required_capabilities(self, repo):
        repo.create_golden_record({
            "canonical_name": "Acme Winch Co", "product_keywords": ["winch"], "is_manufacturer": True,
        })
        # No ISO 9001 capability finding on file for this supplier -- search_suppliers_full's own
        # relational-division join means it won't even be returned as a candidate.
        service = _service(
            repo, brief=_brief(product="winch", required_capabilities=["iso 9001"]),
            suppliers_by_country={},  # fresh discovery also finds nothing, to isolate phase 1's behaviour
        )

        outcome = service.run("find winch manufacturers with ISO 9001")

        assert outcome.qualified_supplier_ids == []


class TestManufacturerAssessmentWiring:
    """Root-cause fix for the Source-tab scoring investigation: before
    this, is_manufacturer/business_scope/registered_capital_rmb were
    NEVER populated for a sourced candidate, so cross_checker.py's
    manufacturer_assessment sub-check (25 of confidence_scorer.py's
    ~100 weighted points) could never get real signal."""

    def test_assess_manufacturer_status_persists_a_real_verdict(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Trading Co"})
        verifier = FakeManufacturerVerifier(result={
            "is_manufacturer": False, "manufacturer_confidence": 20,
            "manufacturer_signals": ["RED FLAG: distributor language found"],
        })
        service = _service(repo, manufacturer_verifier=verifier)

        service._assess_manufacturer_status(supplier_id)

        supplier = repo.get_supplier(supplier_id)
        assert not supplier["is_manufacturer"]
        assert verifier.calls == [supplier_id]

    def test_qichacha_called_only_with_a_uscc_and_credentials(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme Co", "uscc": "123456789012345678",
        })
        qichacha = FakeQichachaClient(
            app_key="key", app_secret="secret",
            verification={"uscc_verified": True, "business_scope": "manufacturing of winches"},
        )
        service = _service(repo, qichacha_client=qichacha)

        service._assess_manufacturer_status(supplier_id)

        assert qichacha.calls == ["123456789012345678"]
        assert repo.get_supplier(supplier_id)["business_scope"] == "manufacturing of winches"

    def test_qichacha_skipped_without_a_uscc(self, repo):
        """The real limit found while scoping this fix: SerpAPI/LLM
        discovery (the Source tab's primary path) never produces a
        USCC at all -- only Alibaba/1688 scraping does. This step
        stays a real no-op for that common case, not an error."""
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co"})
        qichacha = FakeQichachaClient(app_key="key", app_secret="secret")
        service = _service(repo, qichacha_client=qichacha)

        service._assess_manufacturer_status(supplier_id)

        assert qichacha.calls == []

    def test_qichacha_skipped_without_credentials(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme Co", "uscc": "123456789012345678",
        })
        qichacha = FakeQichachaClient()  # app_key/app_secret both None
        service = _service(repo, qichacha_client=qichacha)

        service._assess_manufacturer_status(supplier_id)

        assert qichacha.calls == []

    def test_manufacturer_assessment_failure_does_not_raise(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co"})

        class ExplodingVerifier:
            def assess(self, supplier):
                raise RuntimeError("boom")

        service = _service(repo, manufacturer_verifier=ExplodingVerifier())

        service._assess_manufacturer_status(supplier_id)  # must not raise

    def test_qichacha_failure_does_not_block_manufacturer_assessment(self, repo):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Acme Co", "uscc": "123456789012345678",
        })

        class ExplodingQichacha:
            app_key = "key"
            app_secret = "secret"

            def verify(self, uscc):
                raise RuntimeError("boom")

        verifier = FakeManufacturerVerifier(result={"is_manufacturer": True, "manufacturer_confidence": 60})
        service = _service(repo, qichacha_client=ExplodingQichacha(), manufacturer_verifier=verifier)

        service._assess_manufacturer_status(supplier_id)  # must not raise

        assert verifier.calls == [supplier_id]  # assessment still ran despite the Qichacha failure


class TestAddressExtractionWiring:
    """Root-cause fix: sourcing_agent.py never extracted or set
    suppliers.address anywhere, so cross_checker.py's facility_address
    sub-check (25 of confidence_scorer.py's ~100 weighted points) could
    never fire for a sourced candidate. Reuses the pages
    _extract_and_persist_capabilities() already fetched -- no new
    network call."""

    def test_extract_and_persist_capabilities_returns_the_fetched_pages(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.example.com"})
        pages = [SimpleNamespace(url="https://acme.example.com/", text="some page text", footer_text="")]
        service = SourcingAgentService(
            repo=repo, discovery_service=FakeDiscoveryService(repo), collection_service=FakeCollectionService(),
            verification_service=FakeVerificationService(repo), brief_parser=FakeBriefParser(brief=_brief()),
            dossier_generator=FakeDossierGenerator(), capability_extractor=FakeCapabilityExtractor(),
            own_website_scraper=FakeOwnWebsiteScraper(pages=pages), trade_pipeline=FakeTradePipeline(),
            llm_client=FakeLLMClient(),
        )

        result = service._extract_and_persist_capabilities(supplier_id)

        assert result == pages

    def test_extract_and_persist_capabilities_returns_none_on_fetch_failure(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.example.com"})
        service = SourcingAgentService(
            repo=repo, discovery_service=FakeDiscoveryService(repo), collection_service=FakeCollectionService(),
            verification_service=FakeVerificationService(repo), brief_parser=FakeBriefParser(brief=_brief()),
            dossier_generator=FakeDossierGenerator(), capability_extractor=FakeCapabilityExtractor(),
            own_website_scraper=FakeOwnWebsiteScraper(success=False), trade_pipeline=FakeTradePipeline(),
            llm_client=FakeLLMClient(),
        )

        assert service._extract_and_persist_capabilities(supplier_id) is None

    def test_address_extraction_uses_pages_from_capability_extraction(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co", "domain": "acme.example.com"})
        pages = [SimpleNamespace(
            url="https://acme.example.com/contact",
            text="Contact us: Acme Co, 1 Main St, Springfield, IL. We ship worldwide and respond quickly.",
            footer_text="",
        )]
        llm = FakeLLMClient(response={"address": "1 Main St, Springfield, IL"})
        service = SourcingAgentService(
            repo=repo, discovery_service=FakeDiscoveryService(repo), collection_service=FakeCollectionService(),
            verification_service=FakeVerificationService(repo), brief_parser=FakeBriefParser(brief=_brief()),
            dossier_generator=FakeDossierGenerator(), capability_extractor=FakeCapabilityExtractor(),
            own_website_scraper=FakeOwnWebsiteScraper(pages=pages), trade_pipeline=FakeTradePipeline(),
            llm_client=llm,
        )

        fetched_pages = service._extract_and_persist_capabilities(supplier_id)
        service._attempt_address_extraction(supplier_id, fetched_pages)

        assert repo.get_supplier(supplier_id)["address"] == "1 Main St, Springfield, IL"
        log = repo.get_supplier_change_log(supplier_id)
        assert any(entry["field_name"] == "address" and entry["changed_by"] == "sourcing_agent" for entry in log)

    def test_no_pages_skips_address_extraction_cleanly(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co"})
        service = _service(repo)

        service._attempt_address_extraction(supplier_id, [])  # must not raise

        assert repo.get_supplier(supplier_id).get("address") is None

    def test_address_extraction_failure_does_not_raise(self, repo):
        supplier_id = repo.create_golden_record({"canonical_name": "Acme Co"})
        pages = [SimpleNamespace(
            url="https://acme.example.com/contact",
            text="Contact us: Acme Co, 1 Main St, Springfield, IL. We ship worldwide and respond quickly.",
            footer_text="",
        )]

        class ExplodingLLM:
            def complete_json(self, *args, **kwargs):
                raise RuntimeError("boom")

        service = _service(repo, llm_client=ExplodingLLM())

        service._attempt_address_extraction(supplier_id, pages)  # must not raise

        assert repo.get_supplier(supplier_id).get("address") is None

    def test_full_process_candidate_flow_populates_address_and_manufacturer_fields(self, repo):
        """End-to-end: a freshly-discovered candidate going through
        _process_candidate now gets real facility_address and
        manufacturer_assessment signal, not permanently blank."""
        pages = [SimpleNamespace(
            url="https://acmewinchco.example.com/contact",
            text="Contact Acme Winch Co at 1 Main St, Springfield, IL. We manufacture winches in-house.",
            footer_text="",
        )]
        llm = FakeLLMClient(response={"address": "1 Main St, Springfield, IL"})
        verifier = FakeManufacturerVerifier(result={"is_manufacturer": True, "manufacturer_confidence": 75})
        service = _service(
            repo, suppliers_by_country={None: ["Acme Winch Co"]},
            llm_client=llm, manufacturer_verifier=verifier,
        )
        service.own_website_scraper = FakeOwnWebsiteScraper(pages=pages)

        outcome = service.run("find 1 winch manufacturer")

        assert len(outcome.qualified_supplier_ids) == 1
        supplier_id = outcome.qualified_supplier_ids[0]
        assert repo.get_supplier(supplier_id)["address"] == "1 Main St, Springfield, IL"
        assert verifier.calls == [supplier_id]
