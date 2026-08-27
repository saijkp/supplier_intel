"""
tests/test_website_reachability.py

Tests for verification/website_reachability.py -- relocated from
tests/test_fabtech_exhibitor_import.py once classify_website_
reachability was promoted to a shared module for monitoring/
monitoring_service.py to reuse alongside discovery/
fabtech_exhibitor_import.py. No real network: an injectable
http_client (fake httpx.Client shape), same convention as
tests/test_linde_dealer_import.py.

The Cloudflare-challenge response fixture reproduces the REAL response
shape (status 403, `server: cloudflare`, `cf-mitigated: challenge`,
"Just a moment..." body) found by directly fetching 6 real FABTECH
exhibitor domains (3M, 8020 Inc, 5 Star Engineering, 1stSource,
Accurex Measurement, ABC Sheet Metal, 7 Seas Sourcing) during this
module's original development, not a synthetic guess.
"""

from __future__ import annotations

from verification.website_reachability import classify_website_reachability


class FakeResponse:
    def __init__(self, text="", status_code=200, headers=None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttpxClient:
    """`responses` maps url -> FakeResponse; a url with no entry
    defaults to a 404, matching FakeHttpxClient's own convention in
    tests/test_linde_dealer_import.py."""

    def __init__(self, responses=None, raise_for_url=None):
        self._responses = responses or {}
        self._raise_for_url = raise_for_url
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        if self._raise_for_url and url == self._raise_for_url:
            raise RuntimeError("connection failed")
        return self._responses.get(url, FakeResponse("", status_code=404))


class TestClassifyWebsiteReachability:

    def test_2xx_real_content_is_live(self):
        client = FakeHttpxClient(responses={
            "https://real.example": FakeResponse("<html><body>Acme Steel -- real supplier content.</body></html>"),
        })
        assert classify_website_reachability("https://real.example", http_client=client) == "live"

    def test_3xx_is_live(self):
        client = FakeHttpxClient(responses={"https://real.example": FakeResponse("ok", status_code=301)})
        assert classify_website_reachability("https://real.example", http_client=client) == "live"

    def test_cloudflare_challenge_is_blocked_not_dead(self):
        """Real response shape confirmed by directly fetching 6 real
        exhibitor domains (8020 Inc, 5 Star Engineering, 1stSource,
        Accurex Measurement, ABC Sheet Metal, 7 Seas Sourcing) during
        this module's original development -- all real, live
        companies, not dead sites."""
        client = FakeHttpxClient(responses={
            "https://real-but-protected.example": FakeResponse(
                status_code=403,
                headers={"server": "cloudflare", "cf-mitigated": "challenge"},
                text='<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>',
            ),
        })
        assert classify_website_reachability("https://real-but-protected.example", http_client=client) == "blocked"

    def test_403_without_cloudflare_signature_is_dead(self):
        """A plain 403 with no bot-challenge signature at all is still
        treated as dead -- only a POSITIVELY identified challenge page
        gets the "blocked" classification."""
        client = FakeHttpxClient(responses={
            "https://forbidden.example": FakeResponse(status_code=403, text="Forbidden"),
        })
        assert classify_website_reachability("https://forbidden.example", http_client=client) == "dead"

    def test_404_is_dead(self):
        client = FakeHttpxClient(responses={"https://gone.example": FakeResponse(status_code=404)})
        assert classify_website_reachability("https://gone.example", http_client=client) == "dead"

    def test_connection_failure_is_dead_not_blocked(self):
        """Real finding: 3M's own site timed out (not a served
        challenge page) -- no positive signal it's specifically
        bot-protection rather than a genuine outage, so this stays
        "dead", not "blocked"."""
        client = FakeHttpxClient(raise_for_url="https://unreachable.example")
        assert classify_website_reachability("https://unreachable.example", http_client=client) == "dead"

    def test_empty_url_is_dead(self):
        assert classify_website_reachability("", http_client=FakeHttpxClient()) == "dead"

    def test_parking_page_text_is_dead_despite_200(self):
        client = FakeHttpxClient(responses={
            "https://parked.example": FakeResponse(
                status_code=200,
                text="<html><body>Welcome to nginx! If you see this page, the nginx web "
                     "server is successfully installed and working.</body></html>",
            ),
        })
        assert classify_website_reachability("https://parked.example", http_client=client) == "dead"
