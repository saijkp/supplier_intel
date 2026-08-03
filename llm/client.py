"""
llm/client.py

Shared OpenAI wrapper -- deduplicates the ~5-line lazy-OpenAI()-client +
try/except pattern that verification/capability_extractor.py,
verification/factory_photo_verifier.py, and verification/product_matcher.py
each currently reimplement independently (migrating those three call
sites onto this wrapper is a separate, later, low-risk phase -- see the
redesign plan at .claude/plans/deep-wibbling-rivest.md). This exists now
because Discovery Service and Verification Service call an LLM at
meaningfully higher volume than those three existing call sites ever
have, where a transient RateLimitError/APITimeoutError becomes a real,
frequent failure mode -- worth real retry logic, which none of the
existing three call sites have today.

Design mirrors CapabilityExtractor/FactoryPhotoVerifier/ProductMatcher's
own injectable-client, never-raises contract exactly:
- Construction never requires OPENAI_API_KEY, only a real call does.
- Every public method catches its own failures and returns None on
  ultimate failure (after retries are exhausted) -- never raises to the
  caller. A single supplier's bad LLM call must never abort a batch run.
- `client: Optional[Any] = None` is injectable for tests, same pattern
  used throughout verification/.

Retry policy: RateLimitError/APITimeoutError/APIConnectionError are
retried up to `max_retries` times with exponential backoff + jitter --
these are the openai SDK's own transient-failure exception types.
AuthenticationError/BadRequestError are NOT retried -- these are
configuration/request bugs, not transient conditions, and retrying one
would just fail identically `max_retries` times for no benefit.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_TEXT_MODEL = "gpt-4o-mini"
DEFAULT_VISION_MODEL = "gpt-4o"

_DEFAULT_MAX_RETRIES = 2
_DEFAULT_TIMEOUT_SECONDS = 60.0
_BACKOFF_BASE_SECONDS = 1.0


@dataclass
class LLMResult:
    """Wraps a successful completion -- `.text` is always the raw model
    output; callers needing structured data should use
    LLMClient.complete_json() instead, which parses this for you."""
    text: str
    model: str
    raw_response: Any = None


def _strip_json_fence(text: str) -> str:
    """Strips a leading/trailing ```/```json fence, if present -- models
    routinely wrap JSON output in a markdown code fence despite being
    asked not to. Hoisted from
    verification/capability_extractor.py's own _parse_model_json."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    return cleaned.strip()


class LLMClient:

    def __init__(
        self,
        client: Optional[Any] = None,
        text_model: str = DEFAULT_TEXT_MODEL,
        vision_model: str = DEFAULT_VISION_MODEL,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        sleep_fn: Any = time.sleep,
    ):
        self.text_model = text_model
        self.vision_model = vision_model
        self.max_retries = max_retries
        self.timeout = timeout
        self._client = client
        self._sleep = sleep_fn  # injectable so retry-backoff tests don't actually sleep

    @property
    def client(self) -> Any:
        if self._client is None:
            from openai import OpenAI  # imported lazily: optional dep for tests, matches every existing AI call site

            self._client = OpenAI(timeout=self.timeout)  # reads OPENAI_API_KEY from the environment
        return self._client

    def _call_with_retry(self, fn: Any, *, description: str) -> Optional[Any]:
        """Runs `fn()` (a zero-arg callable making the actual OpenAI
        call), retrying transient failures with exponential backoff +
        jitter, up to self.max_retries. Returns None (logged) on
        ultimate failure -- never raises."""
        from openai import (
            APIConnectionError,
            APITimeoutError,
            AuthenticationError,
            BadRequestError,
            RateLimitError,
        )

        attempt = 0
        while True:
            try:
                return fn()
            except (AuthenticationError, BadRequestError) as e:
                logger.error("%s: non-retryable %s: %s", description, type(e).__name__, e)
                return None
            except (RateLimitError, APITimeoutError, APIConnectionError) as e:
                attempt += 1
                if attempt > self.max_retries:
                    logger.error(
                        "%s: giving up after %d attempt(s), last error %s: %s",
                        description, attempt, type(e).__name__, e,
                    )
                    return None
                backoff = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                logger.warning(
                    "%s: attempt %d/%d failed (%s: %s), retrying in %.1fs",
                    description, attempt, self.max_retries + 1, type(e).__name__, e, backoff,
                )
                self._sleep(backoff)
            except Exception as e:
                # Anything else (a parsing bug in a caller's fn, an SDK
                # exception this wrapper doesn't specifically classify,
                # etc.) -- same never-raises contract as every existing
                # AI call site in this codebase.
                logger.error("%s: unexpected error %s: %s", description, type(e).__name__, e)
                return None

    def complete(
        self, system_prompt: str, user_prompt: str, *,
        model: Optional[str] = None, max_tokens: int = 1500, temperature: float = 0,
    ) -> Optional[LLMResult]:
        """Plain text completion. Returns None on any failure (see
        module docstring) -- check logs to distinguish failure modes."""
        target_model = model or self.text_model

        def _do_call() -> LLMResult:
            response = self.client.chat.completions.create(
                model=target_model,
                max_tokens=max_tokens,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            text = response.choices[0].message.content or ""
            return LLMResult(text=text, model=target_model, raw_response=response)

        return self._call_with_retry(_do_call, description=f"LLMClient.complete({target_model})")

    def complete_json(
        self, system_prompt: str, user_prompt: str, *,
        model: Optional[str] = None, max_tokens: int = 1500, temperature: float = 0,
    ) -> Optional[Any]:
        """Same as complete(), but parses the response as JSON (stripping
        a markdown fence if present, same logic
        capability_extractor._parse_model_json already used) and
        returns the parsed list/dict/etc. Returns None if the call
        fails OR if the response isn't valid JSON -- the two aren't
        currently distinguished, matching complete()'s own contract."""
        result = self.complete(
            system_prompt, user_prompt, model=model, max_tokens=max_tokens, temperature=temperature,
        )
        if result is None:
            return None
        try:
            return json.loads(_strip_json_fence(result.text))
        except (json.JSONDecodeError, IndexError) as e:
            logger.error("LLMClient.complete_json: response was not valid JSON: %s", e)
            return None

    def complete_vision(
        self, prompt: str, image_bytes: bytes, media_type: str, *,
        model: Optional[str] = None, max_tokens: int = 500, temperature: float = 0,
    ) -> Optional[LLMResult]:
        """Vision completion for a single image -- same
        data:{media_type};base64,{...} encoding
        verification/factory_photo_verifier.py already builds by hand,
        so migrating that module onto this wrapper later is a direct
        swap, not a reshape."""
        target_model = model or self.vision_model

        def _do_call() -> LLMResult:
            data_url = f"data:{media_type};base64,{base64.b64encode(image_bytes).decode('utf-8')}"
            response = self.client.chat.completions.create(
                model=target_model,
                max_tokens=max_tokens,
                temperature=temperature,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }],
            )
            text = response.choices[0].message.content or ""
            return LLMResult(text=text, model=target_model, raw_response=response)

        return self._call_with_retry(_do_call, description=f"LLMClient.complete_vision({target_model})")
