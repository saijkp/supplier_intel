"""
tests/test_brief_parser.py

Tests for sourcing/brief_parser.py -- turning one free-text sourcing
brief into a StructuredBrief. Uses a fake LLMClient (records the prompt
it was called with, returns a scripted complete_json() result), same
pattern as tests/test_narrative_generator.py -- llm/client.py's own
tests already cover the retry/parsing logic this depends on.
"""

from __future__ import annotations

import pytest

from sourcing.brief_parser import BriefParser, BriefParsingError, MAX_TARGET_COUNT


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


class TestBriefParserSuccess:

    def test_full_brief_is_extracted(self):
        client = FakeLLMClient(response={
            "product": "winch",
            "application": "off-road trailer recovery",
            "key_specifications": ["12V", "5000lb capacity"],
            "countries": ["China", "India"],
            "required_capabilities": ["iso 9001"],
            "target_count": 15,
            "annual_volume": "5,000 pcs/year",
            "preferred_payment_terms": "30 day",
        })
        parser = BriefParser(llm_client=client)

        brief = parser.parse("find 15 genuine winch manufacturers for off-road trailer recovery, "
                              "12V 5000lb capacity, ISO 9001, prioritise China then India, "
                              "annual volume 5000pcs, 30 day payment terms")

        assert brief.product == "winch"
        assert brief.application == "off-road trailer recovery"
        assert brief.key_specifications == ["12V", "5000lb capacity"]
        assert brief.countries == ["China", "India"]
        assert brief.required_capabilities == ["iso 9001"]
        assert brief.unmapped_terms == []
        assert brief.target_count == 15
        assert brief.annual_volume == "5,000 pcs/year"
        assert brief.preferred_payment_terms == "30 day"
        assert client.last_user_prompt == (
            "find 15 genuine winch manufacturers for off-road trailer recovery, "
            "12V 5000lb capacity, ISO 9001, prioritise China then India, "
            "annual volume 5000pcs, 30 day payment terms"
        )

    def test_minimal_brief_only_needs_a_product(self):
        client = FakeLLMClient(response={"product": "LED marker light"})
        parser = BriefParser(llm_client=client)

        brief = parser.parse("LED marker lights please")

        assert brief.product == "LED marker light"
        assert brief.application is None
        assert brief.key_specifications == []
        assert brief.countries == []
        assert brief.required_capabilities == []
        assert brief.annual_volume is None
        assert brief.preferred_payment_terms is None

    def test_unrecognised_capability_term_is_kept_separately_not_dropped(self):
        """An unmapped term must never silently vanish -- see
        StructuredBrief.unmapped_terms's own docstring: it's for
        display, and a filter must never reject every candidate over
        one phrase the controlled vocabulary doesn't recognise."""
        client = FakeLLMClient(response={
            "product": "winch",
            "required_capabilities": ["iso 9001", "some bespoke certification nobody has heard of"],
        })
        parser = BriefParser(llm_client=client)

        brief = parser.parse("winch manufacturers with ISO 9001 and a made-up cert")

        assert brief.required_capabilities == ["iso 9001"]
        assert brief.unmapped_terms == ["some bespoke certification nobody has heard of"]

    def test_target_count_missing_defaults(self):
        client = FakeLLMClient(response={"product": "winch"})
        parser = BriefParser(llm_client=client)

        brief = parser.parse("find some winch manufacturers")

        assert brief.target_count == 10

    def test_target_count_is_capped_at_max(self):
        client = FakeLLMClient(response={"product": "winch", "target_count": 500})
        parser = BriefParser(llm_client=client)

        brief = parser.parse("find 500 winch manufacturers")

        assert brief.target_count == MAX_TARGET_COUNT

    def test_target_count_zero_or_negative_falls_back_to_default(self):
        client = FakeLLMClient(response={"product": "winch", "target_count": 0})
        parser = BriefParser(llm_client=client)

        brief = parser.parse("find winch manufacturers")

        assert brief.target_count == 10


class TestBriefParserFailure:

    def test_empty_brief_text_raises_without_calling_the_llm(self):
        client = FakeLLMClient(response={"product": "winch"})
        parser = BriefParser(llm_client=client)

        with pytest.raises(BriefParsingError):
            parser.parse("   ")

        assert client.last_user_prompt is None

    def test_llm_returning_non_dict_raises(self):
        client = FakeLLMClient(response=None)  # LLMClient.complete_json returns None on failure
        parser = BriefParser(llm_client=client)

        with pytest.raises(BriefParsingError):
            parser.parse("find some suppliers")

    def test_missing_product_raises(self):
        client = FakeLLMClient(response={"product": None, "target_count": 10})
        parser = BriefParser(llm_client=client)

        with pytest.raises(BriefParsingError):
            parser.parse("find me 10 good suppliers")

    def test_blank_product_raises(self):
        client = FakeLLMClient(response={"product": "   "})
        parser = BriefParser(llm_client=client)

        with pytest.raises(BriefParsingError):
            parser.parse("find me some suppliers")
