"""
tests/test_llm_client.py

Tests for llm/client.py -- the shared OpenAI wrapper backing
Discovery/Verification Service (and, eventually, the 3 existing
bespoke call sites this replaces -- see the module's own docstring).
Uses a fake OpenAI client (same shape as
tests/test_capability_extractor.py's FakeOpenAIClient) so no real
network/API key is needed, plus real openai SDK exception instances
(constructed with minimal httpx.Request/Response objects) to exercise
the retry-vs-fail-fast classification against the actual exception
types LLMClient checks for, not stand-ins.
"""

from __future__ import annotations

import httpx
import openai
import pytest

from llm.client import LLMClient, _strip_json_fence


def _rate_limit_error():
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return openai.RateLimitError("rate limited", response=httpx.Response(429, request=req), body=None)


def _timeout_error():
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return openai.APITimeoutError(request=req)


def _connection_error():
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return openai.APIConnectionError(message="connection error", request=req)


def _auth_error():
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return openai.AuthenticationError("bad api key", response=httpx.Response(401, request=req), body=None)


def _bad_request_error():
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return openai.BadRequestError("malformed request", response=httpx.Response(400, request=req), body=None)


class FakeMessage:
    def __init__(self, text):
        self.content = text


class FakeChoice:
    def __init__(self, text):
        self.message = FakeMessage(text)


class FakeCompletion:
    def __init__(self, text):
        self.choices = [FakeChoice(text)]


class FakeChatCompletionsAPI:
    """`responses` is a list of either a response-text string, or an
    Exception instance to raise -- one entry consumed per .create()
    call, so a test can script "fail twice, then succeed" for the
    retry-path tests."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.call_count = 0
        self.last_call_kwargs = None

    def create(self, **kwargs):
        self.last_call_kwargs = kwargs
        self.call_count += 1
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeCompletion(outcome)


class FakeChatAPI:
    def __init__(self, responses):
        self.completions = FakeChatCompletionsAPI(responses)


class FakeOpenAIClient:
    def __init__(self, responses):
        self.chat = FakeChatAPI(responses)


def _client(responses, **kwargs):
    fake = FakeOpenAIClient(responses)
    return LLMClient(client=fake, sleep_fn=lambda _seconds: None, **kwargs), fake


class TestComplete:

    def test_successful_call_returns_text_and_model(self):
        client, fake = _client(["hello world"])
        result = client.complete("system", "user")
        assert result.text == "hello world"
        assert result.model == client.text_model
        assert fake.chat.completions.call_count == 1

    def test_model_override_is_passed_through(self):
        client, fake = _client(["ok"])
        client.complete("system", "user", model="gpt-4o")
        assert fake.chat.completions.last_call_kwargs["model"] == "gpt-4o"

    def test_construction_never_requires_a_client(self):
        """Matches CapabilityExtractor/FactoryPhotoVerifier's own
        contract exactly -- constructing LLMClient must never touch
        OPENAI_API_KEY or the network."""
        client = LLMClient()
        assert client._client is None


class TestRetryBehaviour:

    def test_rate_limit_error_is_retried_then_succeeds(self):
        client, fake = _client([_rate_limit_error(), "recovered"], max_retries=2)
        result = client.complete("system", "user")
        assert result.text == "recovered"
        assert fake.chat.completions.call_count == 2

    def test_timeout_error_is_retried(self):
        client, fake = _client([_timeout_error(), "recovered"], max_retries=2)
        result = client.complete("system", "user")
        assert result.text == "recovered"

    def test_connection_error_is_retried(self):
        client, fake = _client([_connection_error(), "recovered"], max_retries=2)
        result = client.complete("system", "user")
        assert result.text == "recovered"

    def test_gives_up_after_max_retries_and_returns_none(self):
        client, fake = _client(
            [_rate_limit_error(), _rate_limit_error(), _rate_limit_error()], max_retries=2,
        )
        result = client.complete("system", "user")
        assert result is None
        assert fake.chat.completions.call_count == 3  # 1 initial + 2 retries

    def test_authentication_error_is_not_retried(self):
        client, fake = _client([_auth_error(), "would recover if retried"], max_retries=2)
        result = client.complete("system", "user")
        assert result is None
        assert fake.chat.completions.call_count == 1  # failed fast, no retry attempted

    def test_bad_request_error_is_not_retried(self):
        client, fake = _client([_bad_request_error(), "would recover if retried"], max_retries=2)
        result = client.complete("system", "user")
        assert result is None
        assert fake.chat.completions.call_count == 1

    def test_never_raises_to_the_caller(self):
        """Same never-raises contract as every existing AI call site --
        even an exhausted retry budget must return None, not propagate."""
        client, fake = _client([_connection_error()] * 10, max_retries=2)
        result = client.complete("system", "user")  # must not raise
        assert result is None


class TestCompleteJson:

    def test_parses_a_plain_json_array(self):
        client, fake = _client(['[{"term": "rotomoulding"}]'])
        result = client.complete_json("system", "user")
        assert result == [{"term": "rotomoulding"}]

    def test_strips_a_markdown_json_fence(self):
        client, fake = _client(['```json\n[{"term": "rotomoulding"}]\n```'])
        result = client.complete_json("system", "user")
        assert result == [{"term": "rotomoulding"}]

    def test_strips_a_bare_markdown_fence_without_json_tag(self):
        client, fake = _client(['```\n{"a": 1}\n```'])
        result = client.complete_json("system", "user")
        assert result == {"a": 1}

    def test_invalid_json_returns_none(self):
        client, fake = _client(["this is not json"])
        result = client.complete_json("system", "user")
        assert result is None

    def test_underlying_call_failure_returns_none(self):
        client, fake = _client([_auth_error()])
        result = client.complete_json("system", "user")
        assert result is None


class TestCompleteVision:

    def test_successful_call_builds_a_data_url_and_returns_text(self):
        client, fake = _client(["plausible_factory"])
        result = client.complete_vision("describe this image", b"fake-image-bytes", "image/jpeg")
        assert result.text == "plausible_factory"

        content = fake.chat.completions.last_call_kwargs["messages"][0]["content"]
        assert content[0] == {"type": "text", "text": "describe this image"}
        image_url = content[1]["image_url"]["url"]
        assert image_url.startswith("data:image/jpeg;base64,")

    def test_uses_vision_model_by_default_not_text_model(self):
        client, fake = _client(["ok"], text_model="gpt-4o-mini", vision_model="gpt-4o")
        client.complete_vision("prompt", b"bytes", "image/png")
        assert fake.chat.completions.last_call_kwargs["model"] == "gpt-4o"

    def test_never_raises_on_failure(self):
        client, fake = _client([_connection_error()] * 5, max_retries=1)
        result = client.complete_vision("prompt", b"bytes", "image/png")  # must not raise
        assert result is None


class TestStripJsonFence:

    def test_no_fence_passes_through_unchanged(self):
        assert _strip_json_fence('{"a": 1}') == '{"a": 1}'

    def test_json_tagged_fence_is_stripped(self):
        assert _strip_json_fence('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_bare_fence_is_stripped(self):
        assert _strip_json_fence('```\n{"a": 1}\n```') == '{"a": 1}'
