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
            # Substantive (not an existence-only check), so it survives
            # narrative_generator.py's evidence-text filtering and still
            # exercises the "NOT CONFIRMED" label below.
            SubCheckResult(name="own_site_name_match", verdict=False, detail="Own site text does not corroborate the claimed name"),
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
        # A substantive signal with no inconsistencies -- an entirely
        # empty CrossCheckResult() now short-circuits before the LLM is
        # ever called (see TestInsufficientEvidence), so this needs at
        # least one real signal to reach evidence-text generation at all.
        result = CrossCheckResult(sub_checks=[
            SubCheckResult(name="facility_address", verdict=True, detail="Address confirmed"),
        ])
        generator.generate({"canonical_name": "Acme"}, result, 50)
        assert "Detected inconsistencies" not in client.last_user_prompt

    def test_default_construction_never_requires_a_real_llm_client(self):
        generator = NarrativeGenerator()
        assert generator.llm_client is not None


class TestInsufficientEvidence:
    """When nothing substantive corroborates a supplier, the narrative
    must say so plainly rather than writing prose from an existence-only
    signal (linkedin_presence/phone_format) -- the exact bug reported:
    a correctly-formatted phone number alone produced commentary like
    "indicating some level of operational legitimacy." """

    def test_completely_empty_cross_check_never_calls_the_llm(self):
        client = FakeLLMClient(response={"summary": "should never be seen"})
        generator = NarrativeGenerator(llm_client=client)

        result = generator.generate({"canonical_name": "Acme"}, CrossCheckResult(), 50)

        assert result.summary == "Insufficient evidence — not assessed."
        assert result.strengths == []
        assert result.risks == []
        assert result.suitable_customer_types == []
        assert client.last_user_prompt is None  # LLM never called

    def test_phone_format_alone_is_insufficient(self):
        client = FakeLLMClient(response={"summary": "should never be seen"})
        generator = NarrativeGenerator(llm_client=client)
        result = CrossCheckResult(sub_checks=[
            SubCheckResult(name="phone_format", verdict=True, detail="+8613800000000 plausible for region CN: True"),
        ])

        outcome = generator.generate({"canonical_name": "Acme"}, result, 50)

        assert outcome.summary == "Insufficient evidence — not assessed."
        assert client.last_user_prompt is None

    def test_linkedin_presence_alone_is_insufficient(self):
        client = FakeLLMClient(response={"summary": "should never be seen"})
        generator = NarrativeGenerator(llm_client=client)
        result = CrossCheckResult(sub_checks=[
            SubCheckResult(name="linkedin_presence", verdict=True, detail="LinkedIn company page found"),
        ])

        outcome = generator.generate({"canonical_name": "Acme"}, result, 55)

        assert outcome.summary == "Insufficient evidence — not assessed."
        assert client.last_user_prompt is None

    def test_linkedin_and_phone_together_are_still_insufficient(self):
        client = FakeLLMClient(response={"summary": "should never be seen"})
        generator = NarrativeGenerator(llm_client=client)
        result = CrossCheckResult(sub_checks=[
            SubCheckResult(name="linkedin_presence", verdict=True, detail="LinkedIn company page found"),
            SubCheckResult(name="phone_format", verdict=True, detail="plausible"),
        ])

        outcome = generator.generate({"canonical_name": "Acme"}, result, 55)

        assert outcome.summary == "Insufficient evidence — not assessed."
        assert client.last_user_prompt is None

    def test_one_substantive_signal_alongside_existence_checks_still_generates_narrative(self):
        """A real capability/identity signal is enough to narrate, even
        with linkedin_presence/phone_format also present -- those just
        aren't what tips the decision, and aren't shown to the LLM."""
        client = FakeLLMClient(response={
            "summary": "Real narrative.", "strengths": [], "risks": [], "suitable_customer_types": [],
        })
        generator = NarrativeGenerator(llm_client=client)
        result = CrossCheckResult(sub_checks=[
            SubCheckResult(name="facility_address", verdict=True, detail="Address confirmed"),
            SubCheckResult(name="linkedin_presence", verdict=True, detail="LinkedIn company page found"),
            SubCheckResult(name="phone_format", verdict=True, detail="plausible"),
        ])

        outcome = generator.generate({"canonical_name": "Acme"}, result, 60)

        assert outcome.summary == "Real narrative."
        assert client.last_user_prompt is not None

    def test_existence_only_checks_never_appear_in_evidence_text_even_when_narrative_generated(self):
        client = FakeLLMClient(response={
            "summary": "x", "strengths": [], "risks": [], "suitable_customer_types": [],
        })
        generator = NarrativeGenerator(llm_client=client)
        result = CrossCheckResult(sub_checks=[
            SubCheckResult(name="facility_address", verdict=True, detail="Address confirmed"),
            SubCheckResult(name="linkedin_presence", verdict=True, detail="LinkedIn company page found"),
            SubCheckResult(name="phone_format", verdict=False, detail="implausible"),
        ])

        generator.generate({"canonical_name": "Acme"}, result, 60)

        assert "linkedin_presence" not in client.last_user_prompt
        assert "phone_format" not in client.last_user_prompt
        assert "facility_address" in client.last_user_prompt

    def test_a_false_substantive_verdict_still_counts_as_signal_not_insufficient(self):
        """NOT CONFIRMED is real evidence (a risk), not absence of
        evidence -- must still generate a narrative, not short-circuit."""
        client = FakeLLMClient(response={
            "summary": "x", "strengths": [], "risks": [], "suitable_customer_types": [],
        })
        generator = NarrativeGenerator(llm_client=client)
        result = CrossCheckResult(sub_checks=[
            SubCheckResult(name="manufacturer_assessment", verdict=False, detail="Business scope indicates trading"),
        ])

        outcome = generator.generate({"canonical_name": "Acme"}, result, 25)

        assert outcome.summary == "x"
        assert client.last_user_prompt is not None
