"""
tests/test_input_classifier.py

Tests for discovery/input_classifier.py -- the AI-based company-vs-
product classification backing Find Suppliers' single search box.
Fakes the LLM client entirely (no network/API key needed).
"""

from __future__ import annotations

from discovery.input_classifier import classify_search_input


class FakeLLMClient:
    def __init__(self, response=None, raise_error=None):
        self._response = response
        self._raise_error = raise_error
        self.calls = []

    def complete_json(self, system_prompt, user_prompt, **kwargs):
        self.calls.append((system_prompt, user_prompt, kwargs))
        if self._raise_error:
            raise self._raise_error
        return self._response


class TestClassifySearchInput:

    def test_company_classification_is_returned(self):
        client = FakeLLMClient(response={"kind": "company"})
        assert classify_search_input("Acme Trailer Co", llm_client=client) == "company"
        assert client.calls[0][1] == "Acme Trailer Co"

    def test_product_classification_is_returned(self):
        client = FakeLLMClient(response={"kind": "product"})
        assert classify_search_input("trailer axle", llm_client=client) == "product"

    def test_empty_text_returns_none_without_calling_the_llm(self):
        client = FakeLLMClient(response={"kind": "company"})
        assert classify_search_input("", llm_client=client) is None
        assert classify_search_input("   ", llm_client=client) is None
        assert client.calls == []

    def test_llm_failure_returns_none(self):
        client = FakeLLMClient(response=None)  # LLMClient itself already returns None on failure
        assert classify_search_input("Acme Trailer Co", llm_client=client) is None

    def test_llm_raising_returns_none_not_propagated(self):
        client = FakeLLMClient(raise_error=RuntimeError("boom"))
        assert classify_search_input("Acme Trailer Co", llm_client=client) is None  # must not raise

    def test_non_dict_response_returns_none(self):
        client = FakeLLMClient(response=["not", "a", "dict"])
        assert classify_search_input("Acme Trailer Co", llm_client=client) is None

    def test_invalid_kind_value_returns_none(self):
        """A hallucinated value outside the two allowed kinds must never
        be trusted as-is -- same value-set validation discipline as
        verification/factory_facts_extractor.py's factory_ownership."""
        client = FakeLLMClient(response={"kind": "maybe both?"})
        assert classify_search_input("Acme Trailer Co", llm_client=client) is None

    def test_default_construction_never_requires_a_real_llm_client(self):
        # Constructing without an explicit llm_client must not raise or
        # require OPENAI_API_KEY -- only an actual call would.
        from discovery.input_classifier import classify_search_input as f
        assert f("") is None  # short-circuits on empty text before touching any LLM client
