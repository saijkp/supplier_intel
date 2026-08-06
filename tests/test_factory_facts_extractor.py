"""
tests/test_factory_facts_extractor.py

Tests for verification/factory_facts_extractor.py -- production
lines/machinery/factory-ownership extraction from a supplier's own
website pages. Uses a fake LLMClient (records the prompt, returns a
scripted complete_json() result), same pattern as
tests/test_narrative_generator.py -- llm/client.py's own tests already
cover the retry/parsing logic this depends on.
"""

from __future__ import annotations

from types import SimpleNamespace

from verification.factory_facts_extractor import FactoryFactsExtractor


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


def _page(url="https://acme.example.com/factory", text="We operate three production lines."):
    return SimpleNamespace(url=url, text=text)


class TestFactoryFactsExtractor:

    def test_successful_response_is_parsed(self):
        client = FakeLLMClient(response={
            "production_lines_notes": "Three injection-moulding production lines described.",
            "machinery_notes": "Named CNC machining centres and a robotic welding cell.",
            "factory_ownership": "owned",
        })
        extractor = FactoryFactsExtractor(llm_client=client)

        result = extractor.extract_from_pages([_page()])

        assert result.production_lines_notes == "Three injection-moulding production lines described."
        assert result.machinery_notes == "Named CNC machining centres and a robotic welding cell."
        assert result.factory_ownership == "owned"
        assert result.model_used == "gpt-4o-mini"

    def test_invalid_ownership_value_is_soft_corrected_to_unclear(self):
        client = FakeLLMClient(response={
            "production_lines_notes": "Some notes.",
            "machinery_notes": "Some notes.",
            "factory_ownership": "probably owned, hard to say",  # not one of the 4 allowed values
        })
        extractor = FactoryFactsExtractor(llm_client=client)

        result = extractor.extract_from_pages([_page()])

        assert result.factory_ownership == "unclear"

    def test_missing_ownership_value_defaults_to_unclear(self):
        client = FakeLLMClient(response={
            "production_lines_notes": "Some notes.", "machinery_notes": "Some notes.",
        })
        extractor = FactoryFactsExtractor(llm_client=client)
        result = extractor.extract_from_pages([_page()])
        assert result.factory_ownership == "unclear"

    def test_ownership_value_is_case_and_whitespace_normalised(self):
        client = FakeLLMClient(response={
            "production_lines_notes": "Some notes.", "machinery_notes": "Some notes.",
            "factory_ownership": "  LEASED  ",
        })
        extractor = FactoryFactsExtractor(llm_client=client)
        result = extractor.extract_from_pages([_page()])
        assert result.factory_ownership == "leased"

    def test_llm_failure_returns_none(self):
        client = FakeLLMClient(response=None)  # LLMClient itself already returns None on failure
        extractor = FactoryFactsExtractor(llm_client=client)
        result = extractor.extract_from_pages([_page()])
        assert result is None

    def test_non_dict_response_returns_none(self):
        client = FakeLLMClient(response=["not", "a", "dict"])
        extractor = FactoryFactsExtractor(llm_client=client)
        result = extractor.extract_from_pages([_page()])
        assert result is None

    def test_response_with_no_usable_notes_returns_none(self):
        client = FakeLLMClient(response={"production_lines_notes": "", "machinery_notes": None, "factory_ownership": "owned"})
        extractor = FactoryFactsExtractor(llm_client=client)
        result = extractor.extract_from_pages([_page()])
        assert result is None

    def test_no_pages_returns_none_without_calling_the_llm(self):
        client = FakeLLMClient(response={"production_lines_notes": "x", "machinery_notes": "x", "factory_ownership": "owned"})
        extractor = FactoryFactsExtractor(llm_client=client)
        result = extractor.extract_from_pages([])
        assert result is None
        assert client.last_user_prompt is None

    def test_pages_with_only_empty_text_return_none_without_calling_the_llm(self):
        client = FakeLLMClient(response={"production_lines_notes": "x", "machinery_notes": "x", "factory_ownership": "owned"})
        extractor = FactoryFactsExtractor(llm_client=client)
        result = extractor.extract_from_pages([_page(text=""), _page(text="   ")])
        assert result is None
        assert client.last_user_prompt is None

    def test_evidence_text_includes_page_urls_and_text(self):
        client = FakeLLMClient(response={"production_lines_notes": "x", "machinery_notes": "x", "factory_ownership": "owned"})
        extractor = FactoryFactsExtractor(llm_client=client)
        extractor.extract_from_pages([_page(url="https://acme.example.com/factory", text="Three production lines.")])

        assert "https://acme.example.com/factory" in client.last_user_prompt
        assert "Three production lines." in client.last_user_prompt

    def test_missing_one_note_falls_back_to_the_other(self):
        """Only one of the two notes fields needs real content for the
        result to be usable -- the other gets a fallback string, never
        dropped entirely."""
        client = FakeLLMClient(response={
            "production_lines_notes": "Three production lines described.",
            "machinery_notes": "",
            "factory_ownership": "owned",
        })
        extractor = FactoryFactsExtractor(llm_client=client)
        result = extractor.extract_from_pages([_page()])
        assert result is not None
        assert result.machinery_notes == "No evidence available to assess this."

    def test_default_construction_never_requires_a_real_llm_client(self):
        extractor = FactoryFactsExtractor()
        assert extractor.llm_client is not None
