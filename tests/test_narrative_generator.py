"""
tests/test_narrative_generator.py

Tests for verification_ai/narrative_generator.py -- the AI-written
company summary/strengths/risks/suitable-customer-types generator.
Uses a fake LLMClient (records the prompt it was called with, returns
a scripted complete_json() result) rather than a real OpenAI call --
llm/client.py's own tests already cover the retry/parsing logic this
depends on.
"""

from __future__ import annotations

from verification_ai.cross_checker import CrossCheckResult, SubCheckResult
from verification_ai.narrative_generator import NarrativeGenerator


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


def _cross_check_result():
    return CrossCheckResult(
        sub_checks=[
            SubCheckResult(name="facility_address", verdict=True, detail="Address confirmed via Google Places"),
            SubCheckResult(name="phone_format", verdict=False, detail="Phone doesn't match claimed country"),
        ],
        inconsistencies=["Phone number does not look valid for claimed country"],
        manufacturer_confidence=80,
        is_manufacturer=True,
    )


class TestNarrativeGenerator:

    def test_successful_response_is_parsed_into_a_narrative_result(self):
        client = FakeLLMClient(response={
            "summary": "A well-corroborated manufacturer with one contact-detail concern.",
            "strengths": ["Address independently verified"],
            "risks": ["Phone number inconsistent with claimed country"],
            "suitable_customer_types": ["Mid-size OEM buyers"],
        })
        generator = NarrativeGenerator(llm_client=client)

        result = generator.generate({"canonical_name": "Acme"}, _cross_check_result(), 65)

        assert result.summary == "A well-corroborated manufacturer with one contact-detail concern."
        assert result.strengths == ["Address independently verified"]
        assert result.risks == ["Phone number inconsistent with claimed country"]
        assert result.suitable_customer_types == ["Mid-size OEM buyers"]
        assert result.model_used == "gpt-4o-mini"

    def test_llm_failure_returns_none(self):
        client = FakeLLMClient(response=None)  # LLMClient itself already returns None on failure
        generator = NarrativeGenerator(llm_client=client)
        result = generator.generate({"canonical_name": "Acme"}, _cross_check_result(), 50)
        assert result is None

    def test_response_missing_summary_returns_none(self):
        client = FakeLLMClient(response={"strengths": [], "risks": [], "suitable_customer_types": []})
        generator = NarrativeGenerator(llm_client=client)
        result = generator.generate({"canonical_name": "Acme"}, _cross_check_result(), 50)
        assert result is None

    def test_non_dict_response_returns_none(self):
        client = FakeLLMClient(response=["not", "a", "dict"])
        generator = NarrativeGenerator(llm_client=client)
        result = generator.generate({"canonical_name": "Acme"}, _cross_check_result(), 50)
        assert result is None

    def test_non_string_list_items_are_filtered_out(self):
        client = FakeLLMClient(response={
            "summary": "Summary text.",
            "strengths": ["real strength", 123, None, ""],
            "risks": [],
            "suitable_customer_types": [],
        })
        generator = NarrativeGenerator(llm_client=client)
        result = generator.generate({"canonical_name": "Acme"}, _cross_check_result(), 50)
        assert result.strengths == ["real strength"]

    def test_evidence_text_includes_confidence_score_and_sub_checks(self):
        client = FakeLLMClient(response={"summary": "x", "strengths": [], "risks": [], "suitable_customer_types": []})
        generator = NarrativeGenerator(llm_client=client)
        generator.generate({"canonical_name": "Acme Co", "country": "China"}, _cross_check_result(), 65)

        assert "Acme Co" in client.last_user_prompt
        assert "65/100" in client.last_user_prompt
        assert "facility_address" in client.last_user_prompt
        assert "CONFIRMED" in client.last_user_prompt
        assert "NOT CONFIRMED" in client.last_user_prompt

    def test_evidence_text_includes_inconsistencies_when_present(self):
        client = FakeLLMClient(response={"summary": "x", "strengths": [], "risks": [], "suitable_customer_types": []})
        generator = NarrativeGenerator(llm_client=client)
        generator.generate({"canonical_name": "Acme"}, _cross_check_result(), 50)
        assert "Detected inconsistencies" in client.last_user_prompt
        assert "does not look valid for claimed country" in client.last_user_prompt

    def test_evidence_text_omits_inconsistencies_section_when_none(self):
        client = FakeLLMClient(response={"summary": "x", "strengths": [], "risks": [], "suitable_customer_types": []})
        generator = NarrativeGenerator(llm_client=client)
        generator.generate({"canonical_name": "Acme"}, CrossCheckResult(), 50)
        assert "Detected inconsistencies" not in client.last_user_prompt

    def test_default_construction_never_requires_a_real_llm_client(self):
        generator = NarrativeGenerator()
        assert generator.llm_client is not None
