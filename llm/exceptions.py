"""
llm/exceptions.py

LLMClient never lets these escape to a caller -- every public method on
LLMClient catches its own failures internally and returns None (see
llm/client.py's own docstring for why). These exist for LLMClient's
internal retry/no-retry classification, not as a public error-handling
contract callers are expected to catch.
"""

from __future__ import annotations


class LLMError(Exception):
    """Base class for every error LLMClient raises internally."""


class LLMTransientError(LLMError):
    """A retryable failure -- rate limit, timeout, or connection error.
    LLMClient retries these with backoff before giving up."""


class LLMConfigError(LLMError):
    """A non-retryable failure -- bad API key, malformed request. Retrying
    would just fail identically, so LLMClient fails fast on these."""
