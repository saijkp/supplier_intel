"""
tests/test_trade_source_finder.py

Tests for discovery/trade_source_finder.py -- Phase 0.5's lightweight
"is there a real, fetchable trade-association page for this product"
check. No real network: a fake GoogleSearchScraper (same convention as
tests/test_discovery_service.py's FakeGoogleScraper) and a fake httpx
client (same shape as tests/test_website_reachability.py's
FakeHttpxClient).
"""

from __future__ import annotations

from types import SimpleNamespace

from discovery.trade_source_finder import (
    _bot_wall_reason,
    find_candidate_trade_source,
)

_REAL_TEXT = "Welcome to the Widget Manufacturers Association. " * 10  # well over the length floor


def _search_result(link, title="", snippet=""):
    return SimpleNamespace(success=True, raw_data={"link": link, "title": title, "snippet": snippet})


class FakeGoogleScraper:
    def __init__(self, results=None, raise_error=None):
        self._results = results if results is not None else []
        self._raise_error = raise_error
        self.queries = []

    def scrape(self, query, max_results=20, **kwargs):
        self.queries.append(query)
        if self._raise_error:
            raise self._raise_error
        return self._results


class FakeResponse:
    def __init__(self, text="", status_code=200, url=None):
        self.text = text
        self.status_code = status_code
        self.url = url or "https://example.com/"


class FakeHttpxClient:
    """`responses` maps url -> FakeResponse; a url with no entry raises
    (matching a real connection failure), unless it's in `dead_urls`
    (silently skipped, matching a real 404/timeout that isn't an
    exception)."""

    def __init__(self, responses=None, raise_for_url=None):
        self._responses = responses or {}
        self._raise_for_url = raise_for_url
        self.calls = []

    def get(self, url, headers=None):
        self.calls.append(url)
        if self._raise_for_url and url == self._raise_for_url:
            raise RuntimeError("connection failed")
        if url not in self._responses:
            return FakeResponse("", status_code=404, url=url)
        return self._responses[url]


class TestBotWallReason:

    def test_none_for_real_content(self):
        assert _bot_wall_reason(_REAL_TEXT) is None

    def test_detects_cloudflare_just_a_moment(self):
        assert _bot_wall_reason("Just a moment... Enable JavaScript and cookies to continue") is not None

    def test_detects_attention_required(self):
        assert _bot_wall_reason("Attention Required! | Cloudflare") is not None

    def test_case_insensitive(self):
        assert _bot_wall_reason("JUST A MOMENT") is not None

    def test_empty_text_is_none(self):
        assert _bot_wall_reason("") is None


class TestFindCandidateTradeSource:

    def test_real_fetchable_page_is_surfaced(self):
        google = FakeGoogleScraper(results=[
            _search_result("https://real-assoc.example/", title="Widget Assoc", snippet="A trade body"),
        ])
        http = FakeHttpxClient(responses={
            "https://real-assoc.example/": FakeResponse(f"<html><body>{_REAL_TEXT}</body></html>", url="https://real-assoc.example/"),
        })
        result = find_candidate_trade_source("widget", google_scraper=google, http_client=http)
        assert result is not None
        assert result.domain == "real-assoc.example"
        assert result.title == "Widget Assoc"

    def test_search_query_uses_fixed_phrasing(self):
        google = FakeGoogleScraper(results=[])
        find_candidate_trade_source("widget", google_scraper=google, http_client=FakeHttpxClient())
        assert google.queries == ["widget trade association UK"]

    def test_no_search_results_returns_none(self):
        google = FakeGoogleScraper(results=[])
        assert find_candidate_trade_source("widget", google_scraper=google, http_client=FakeHttpxClient()) is None

    def test_search_failure_returns_none_not_raise(self):
        google = FakeGoogleScraper(raise_error=RuntimeError("serpapi down"))
        assert find_candidate_trade_source("widget", google_scraper=google, http_client=FakeHttpxClient()) is None

    def test_non_200_response_is_skipped(self):
        google = FakeGoogleScraper(results=[_search_result("https://dead.example/")])
        http = FakeHttpxClient(responses={"https://dead.example/": FakeResponse("", status_code=404)})
        assert find_candidate_trade_source("widget", google_scraper=google, http_client=http) is None

    def test_fetch_exception_is_skipped_not_raised(self):
        google = FakeGoogleScraper(results=[_search_result("https://boom.example/")])
        http = FakeHttpxClient(raise_for_url="https://boom.example/")
        assert find_candidate_trade_source("widget", google_scraper=google, http_client=http) is None

    def test_parking_page_is_rejected(self):
        google = FakeGoogleScraper(results=[_search_result("https://parked.example/")])
        http = FakeHttpxClient(responses={
            "https://parked.example/": FakeResponse("<html><body>This domain is parked. Buy this domain.</body></html>"),
        })
        assert find_candidate_trade_source("widget", google_scraper=google, http_client=http) is None

    def test_bot_wall_page_is_rejected(self):
        google = FakeGoogleScraper(results=[_search_result("https://walled.example/")])
        http = FakeHttpxClient(responses={
            "https://walled.example/": FakeResponse("<html><body>Just a moment... Enable JavaScript and cookies to continue</body></html>"),
        })
        assert find_candidate_trade_source("widget", google_scraper=google, http_client=http) is None

    def test_too_short_text_is_rejected(self):
        google = FakeGoogleScraper(results=[_search_result("https://empty.example/")])
        http = FakeHttpxClient(responses={"https://empty.example/": FakeResponse("<html><body>Hi</body></html>")})
        assert find_candidate_trade_source("widget", google_scraper=google, http_client=http) is None

    def test_falls_through_to_next_result_after_a_rejection(self):
        google = FakeGoogleScraper(results=[
            _search_result("https://bad.example/", title="Bad"),
            _search_result("https://good.example/", title="Good"),
        ])
        http = FakeHttpxClient(responses={
            "https://bad.example/": FakeResponse("This domain is parked."),
            "https://good.example/": FakeResponse(f"<html><body>{_REAL_TEXT}</body></html>", url="https://good.example/"),
        })
        result = find_candidate_trade_source("widget", google_scraper=google, http_client=http)
        assert result is not None
        assert result.domain == "good.example"

    def test_only_tries_up_to_three_results(self):
        google = FakeGoogleScraper(results=[
            _search_result(f"https://site{i}.example/") for i in range(5)
        ])
        http = FakeHttpxClient()  # every url defaults to 404
        find_candidate_trade_source("widget", google_scraper=google, http_client=http)
        assert len(http.calls) == 3

    def test_result_missing_link_is_skipped(self):
        google = FakeGoogleScraper(results=[SimpleNamespace(success=True, raw_data={"title": "no link"})])
        assert find_candidate_trade_source("widget", google_scraper=google, http_client=FakeHttpxClient()) is None

    def test_unsuccessful_result_is_skipped(self):
        google = FakeGoogleScraper(results=[SimpleNamespace(success=False, raw_data={"link": "https://x.example/"})])
        http = FakeHttpxClient(responses={"https://x.example/": FakeResponse(_REAL_TEXT)})
        result = find_candidate_trade_source("widget", google_scraper=google, http_client=http)
        assert result is None
        assert http.calls == []
