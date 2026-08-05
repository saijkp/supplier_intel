"""
tests/test_dossier_generator.py

Tests for sourcing/dossier_generator.py -- the detailed procurement
checklist assessment. Uses a fake LLMClient, same pattern as
tests/test_narrative_generator.py.
"""

from __future__ import annotations

from sourcing.dossier_generator import SourcingDossierGenerator
from sourcing.schemas import StructuredBrief
from verification_ai.cross_checker import CrossCheckResult, SubCheckResult


class FakeLLMClient:
    def __init__(self, response=None):
        self.text_model = "gpt-4o-mini"
        self._response = response
        self.last_system_prompt = None
        self.last_user_prompt = None

    def complete_json(self, system_prompt, user_prompt, **kwargs):
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt
        return self._response


def _brief(**overrides):
    defaults = dict(product="winch", target_count=10)
    defaults.update(overrides)
    return StructuredBrief(**defaults)


def _full_dossier_response():
    return {
        "oem_odm_capability": "Own-website evidence shows in-house ODM design services.",
        "factory_manufacturing_processes": "CNC machining and welding confirmed in-house.",
        "engineering_testing_capability": "No in-house testing lab evidence found.",
        "export_experience": "No export-history evidence on file.",
        "annual_volume_suitability": "No factory size evidence to assess the buyer's stated volume.",
        "payment_terms_assessment": "Supplier offers DDP; matches the buyer's stated preference.",
    }


def _cross_check_result(sub_checks=None, inconsistencies=None):
    return CrossCheckResult(
        sub_checks=sub_checks or [],
        inconsistencies=inconsistencies or [],
    )


class TestSourcingDossierGenerator:

    def test_successful_response_is_parsed(self):
        client = FakeLLMClient(response=_full_dossier_response())
        generator = SourcingDossierGenerator(llm_client=client)
        cross_result = _cross_check_result(sub_checks=[
            SubCheckResult(name="facility_address", verdict=True, detail="Address confirmed"),
        ])

        dossier = generator.generate({"canonical_name": "Acme Winch Co"}, _brief(), cross_result)

        assert dossier.oem_odm_capability == "Own-website evidence shows in-house ODM design services."
        assert dossier.payment_terms_assessment == "Supplier offers DDP; matches the buyer's stated preference."

    def test_returns_none_on_llm_failure(self):
        client = FakeLLMClient(response=None)
        generator = SourcingDossierGenerator(llm_client=client)

        dossier = generator.generate({"canonical_name": "Acme"}, _brief(), _cross_check_result())

        assert dossier is None

    def test_returns_none_when_response_has_no_usable_content(self):
        client = FakeLLMClient(response={
            "oem_odm_capability": "", "factory_manufacturing_processes": None,
        })
        generator = SourcingDossierGenerator(llm_client=client)

        dossier = generator.generate({"canonical_name": "Acme"}, _brief(), _cross_check_result())

        assert dossier is None

    def test_missing_individual_field_falls_back_to_a_stated_gap_not_a_crash(self):
        client = FakeLLMClient(response={"oem_odm_capability": "In-house ODM confirmed."})
        generator = SourcingDossierGenerator(llm_client=client)

        dossier = generator.generate({"canonical_name": "Acme"}, _brief(), _cross_check_result())

        assert dossier.oem_odm_capability == "In-house ODM confirmed."
        assert dossier.export_experience == "No evidence available to assess this."


class TestVerificationStatus:

    def test_no_signals_is_unverified(self):
        client = FakeLLMClient(response=_full_dossier_response())
        generator = SourcingDossierGenerator(llm_client=client)
        cross_result = _cross_check_result(sub_checks=[
            SubCheckResult(name="linkedin_presence", verdict=None, detail="no check run"),
        ])

        dossier = generator.generate({"canonical_name": "Acme"}, _brief(), cross_result)

        assert dossier.verification_status == "unverified"

    def test_few_signals_is_partially_verified(self):
        client = FakeLLMClient(response=_full_dossier_response())
        generator = SourcingDossierGenerator(llm_client=client)
        cross_result = _cross_check_result(sub_checks=[
            SubCheckResult(name="facility_address", verdict=True, detail="confirmed"),
            SubCheckResult(name="phone_format", verdict=True, detail="plausible"),
        ])

        dossier = generator.generate({"canonical_name": "Acme"}, _brief(), cross_result)

        assert dossier.verification_status == "partially verified"

    def test_several_signals_with_no_inconsistencies_is_verified(self):
        client = FakeLLMClient(response=_full_dossier_response())
        generator = SourcingDossierGenerator(llm_client=client)
        cross_result = _cross_check_result(sub_checks=[
            SubCheckResult(name="facility_address", verdict=True, detail="confirmed"),
            SubCheckResult(name="phone_format", verdict=True, detail="plausible"),
            SubCheckResult(name="own_site_name_match", verdict=True, detail="matches"),
        ])

        dossier = generator.generate({"canonical_name": "Acme"}, _brief(), cross_result)

        assert dossier.verification_status == "verified"

    def test_several_signals_but_an_inconsistency_is_only_partially_verified(self):
        client = FakeLLMClient(response=_full_dossier_response())
        generator = SourcingDossierGenerator(llm_client=client)
        cross_result = _cross_check_result(
            sub_checks=[
                SubCheckResult(name="facility_address", verdict=True, detail="confirmed"),
                SubCheckResult(name="phone_format", verdict=False, detail="implausible"),
                SubCheckResult(name="own_site_name_match", verdict=True, detail="matches"),
            ],
            inconsistencies=["Phone number does not look valid for claimed country"],
        )

        dossier = generator.generate({"canonical_name": "Acme"}, _brief(), cross_result)

        assert dossier.verification_status == "partially verified"

    def test_verification_status_is_not_taken_from_the_llm_response(self):
        """Deterministic by design -- even if the LLM tried to answer
        it, the field isn't even in the requested schema/read from the
        response, so a stray 'verification_status' key must be
        ignored."""
        client = FakeLLMClient(response={**_full_dossier_response(), "verification_status": "verified"})
        generator = SourcingDossierGenerator(llm_client=client)
        cross_result = _cross_check_result(sub_checks=[])  # no signals -> should be 'unverified'

        dossier = generator.generate({"canonical_name": "Acme"}, _brief(), cross_result)

        assert dossier.verification_status == "unverified"


class TestEvidenceTextBuildsCorrectly:

    def test_evidence_text_includes_brief_and_capability_findings(self):
        client = FakeLLMClient(response=_full_dossier_response())
        generator = SourcingDossierGenerator(llm_client=client)
        brief = _brief(
            application="off-road trailer recovery", annual_volume="5,000 pcs/year",
            preferred_payment_terms="30 day",
        )
        supplier = {
            "canonical_name": "Acme Winch Co", "country": "China",
            "payment_terms_offered": ["30 day"], "incoterms_supported": ["ddp shipping"],
        }
        capability_findings = [{
            "canonical_term": "cnc machining", "relationship": "in_house",
            "confidence": 0.9, "evidence": "We operate 12 CNC machines in-house.",
        }]

        generator.generate(supplier, brief, _cross_check_result(), capability_findings)

        prompt = client.last_user_prompt
        assert "off-road trailer recovery" in prompt
        assert "5,000 pcs/year" in prompt
        assert "30 day" in prompt
        assert "cnc machining" in prompt
        assert "We operate 12 CNC machines in-house." in prompt
        assert "ddp shipping" in prompt
