"""
tests/test_log_redaction.py

Regression tests for config.settings._RedactApiKeysFilter: every
scraper's HTTP client is built on httpx, and httpx logs every outbound
request URL (including the api_key=/key=/token= query-string
credentials this codebase's scrapers/verifiers send to Apify, SerpAPI,
Google Places, Amap, and Qichacha) at INFO level through its own
"httpx" child logger. Without this filter, a real key would appear in
plain text in application logs.

Each test builds its own isolated logger/handler pair (never touches
the real root logger or config.settings.configure_logging's own
handler) so these tests can't interfere with each other or with
logging output from the rest of the suite.
"""

from __future__ import annotations

import io
import logging

import pytest

from config.settings import _RedactApiKeysFilter


class _FakeHttpxURL:
    """Stands in for httpx.URL: a non-str object whose only string
    representation is via __str__, matching how httpx actually passes
    request.url to its own logger. A filter that only checks
    isinstance(value, str) would silently skip this."""

    def __init__(self, url: str):
        self._url = url

    def __str__(self) -> str:
        return self._url


@pytest.fixture()
def captured_handler():
    """A handler wired to an in-memory stream, with the redaction
    filter attached to the HANDLER -- the placement this filter
    requires (see its own class docstring)."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s | %(message)s"))
    handler.addFilter(_RedactApiKeysFilter())
    return handler, stream


@pytest.fixture()
def isolated_logger(request, captured_handler):
    """A freshly named, non-propagating logger tree per test so runs
    never bleed into each other or the real root logger."""
    handler, stream = captured_handler
    root = logging.getLogger(f"_test_redact_{request.node.name}")
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    root.propagate = False
    return root, stream


class TestRedactsApiKeysFromHttpxStyleLogLines:

    def test_catches_a_fake_key_in_an_httpx_style_log_line(self, isolated_logger):
        """Reproduces httpx's own logging call shape exactly: the URL
        is passed as a non-str object positional arg, not baked into
        the message string, and the message uses %-style placeholders
        (including a %d for the status code) the way httpx's real
        request-logging call does."""
        root, stream = isolated_logger
        httpx_logger = logging.getLogger(f"{root.name}.httpx")
        httpx_logger.setLevel(logging.INFO)

        fake_key = "sk-fake-serpapi-secret-12345"
        url = _FakeHttpxURL(f"https://serpapi.com/search?engine=google&api_key={fake_key}&q=trailer+axle")
        httpx_logger.info('HTTP Request: %s %s "%s %d %s"', "GET", url, "HTTP/1.1", 200, "OK")

        output = stream.getvalue()
        assert fake_key not in output
        assert "api_key=***REDACTED***" in output
        # the rest of the URL survives -- this is redaction, not wholesale suppression
        assert "serpapi.com/search?engine=google" in output
        assert "q=trailer+axle" in output

    def test_redacts_key_and_token_params_too_not_just_api_key(self, isolated_logger):
        root, stream = isolated_logger
        root.info("fetching %s", _FakeHttpxURL("https://example.com/x?key=secret1&token=secret2&other=fine"))

        output = stream.getvalue()
        assert "secret1" not in output
        assert "secret2" not in output
        assert "key=***REDACTED***" in output
        assert "token=***REDACTED***" in output
        assert "other=fine" in output  # unrelated params are left alone

    def test_ordinary_log_messages_pass_through_unchanged(self, isolated_logger):
        """No key-shaped parameter anywhere -- the filter must not
        mangle or suppress a normal log line."""
        root, stream = isolated_logger
        root.info("supplier %s scored %d", "Acme Manufacturing", 87)

        output = stream.getvalue()
        assert "supplier Acme Manufacturing scored 87" in output
        assert "REDACTED" not in output

    def test_percent_style_formatting_of_non_string_args_still_works(self, isolated_logger):
        """The filter redacts record.getMessage() -- the already
        %-formatted final text -- not record.msg/record.args
        separately, specifically so ordinary %d/%.2f formatting of
        real int/float args happens exactly as it always did, before
        the filter's regex ever sees the resulting string."""
        root, stream = isolated_logger
        root.info("processed %d items in %.2f seconds", 42, 1.5)

        output = stream.getvalue()
        assert "processed 42 items in 1.50 seconds" in output

    def test_filter_must_be_on_the_handler_not_the_logger_to_catch_propagated_records(self):
        """The design point _RedactApiKeysFilter's own docstring makes:
        a filter attached to a logger's own .filters list only ever
        runs against records that originate from a direct call on that
        specific logger object -- propagation up the hierarchy does
        not re-check each ancestor logger's filters. Attaching it to
        the wrong place (the logger, not the handler) must actually
        fail to catch a propagated child-logger record, proving the
        distinction is load-bearing and not just documentation."""
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        # deliberately wrong placement, to prove the failure mode is real
        root = logging.getLogger("_test_redact_wrong_placement")
        root.handlers = [handler]
        root.setLevel(logging.INFO)
        root.propagate = False
        root.addFilter(_RedactApiKeysFilter())

        child = logging.getLogger("_test_redact_wrong_placement.httpx")
        child.setLevel(logging.INFO)
        fake_key = "sk-fake-should-have-leaked-99999"
        child.info("GET https://serpapi.com/search?api_key=%s", fake_key)

        output = stream.getvalue()
        assert fake_key in output  # confirms logger-level placement genuinely doesn't work

    def test_filter_on_the_handler_catches_the_same_propagated_record(self, isolated_logger):
        """The correct counterpart to the previous test: the exact same
        propagation scenario, filter attached to the handler as
        configure_logging actually does it, key is caught."""
        root, stream = isolated_logger
        child = logging.getLogger(f"{root.name}.httpx")
        child.setLevel(logging.INFO)
        fake_key = "sk-fake-should-be-redacted-99999"
        child.info("GET https://serpapi.com/search?api_key=%s", fake_key)

        output = stream.getvalue()
        assert fake_key not in output
        assert "api_key=***REDACTED***" in output


class TestConfigureLoggingWiresTheFilterIn:

    def test_configure_logging_attaches_the_filter_to_its_handler(self, monkeypatch):
        """configure_logging() itself must actually wire the filter
        onto the handler it creates -- a unit test on the filter class
        alone wouldn't catch a regression where the two got
        disconnected (e.g. the filter defined but never attached)."""
        import config.settings as settings

        root = logging.getLogger()
        original_handlers = list(root.handlers)
        try:
            root.handlers = []  # force configure_logging past its "already configured" guard
            settings.configure_logging()
            assert root.handlers, "configure_logging should have created at least one handler"
            assert any(
                isinstance(f, settings._RedactApiKeysFilter)
                for handler in root.handlers
                for f in handler.filters
            )
        finally:
            root.handlers = original_handlers
