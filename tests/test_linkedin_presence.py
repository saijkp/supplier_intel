"""
tests/test_linkedin_presence.py

Tests for verification.linkedin_presence.LinkedInPresenceChecker.
"""

from __future__ import annotations

from verification.linkedin_presence import LinkedInPresenceChecker


class FakeSearchResult:
    def __init__(self, link, snippet="", success=True):
        self.raw_data = {"link": link, "snippet": snippet, "title": "result"}
        self.success = success


class FakeGoogleScraper:
    def __init__(self, results=None, raise_error=None):
        self._results = results or []
        self._raise_error = raise_error
        self.calls = []

    def scrape(self, query, max_results=10, site_filter=None, **kwargs):
        self.calls.append({"query": query, "site_filter": site_filter})
        if self._raise_error:
            raise self._raise_error
        return self._results


class TestLinkedInPresenceCheck:

    def test_finds_a_linkedin_company_page(self):
        google = FakeGoogleScraper([
            FakeSearchResult("https://www.linkedin.com/company/acme-trailer-parts", snippet="500+ employees"),
        ])
        checker = LinkedInPresenceChecker(google)
        result = checker.check("Acme Trailer Parts")
        assert result.presence_confirmed is True
        assert result.linkedin_url == "https://www.linkedin.com/company/acme-trailer-parts"
        assert result.snippet == "500+ employees"

    def test_no_results_means_no_presence(self):
        google = FakeGoogleScraper([])
        checker = LinkedInPresenceChecker(google)
        result = checker.check("Totally Obscure Company")
        assert result.presence_confirmed is False
        assert result.linkedin_url is None

    def test_search_is_scoped_to_linkedin_domain(self):
        google = FakeGoogleScraper([])
        checker = LinkedInPresenceChecker(google)
        checker.check("Acme Trailer Parts")
        assert google.calls[0]["site_filter"] == "linkedin.com"

    def test_company_name_is_quoted_in_the_query(self):
        google = FakeGoogleScraper([])
        checker = LinkedInPresenceChecker(google)
        checker.check("Acme Trailer Parts")
        assert '"Acme Trailer Parts"' in google.calls[0]["query"]

    def test_empty_company_name_short_circuits_without_searching(self):
        google = FakeGoogleScraper([])
        checker = LinkedInPresenceChecker(google)
        result = checker.check("")
        assert result.presence_confirmed is False
        assert google.calls == []

    def test_search_failure_is_caught_not_raised(self):
        checker = LinkedInPresenceChecker(FakeGoogleScraper(raise_error=RuntimeError("SerpAPI down")))
        result = checker.check("Acme Trailer Parts")
        assert result.presence_confirmed is False
        assert "search failed" in result.reason

    def test_non_linkedin_result_in_the_list_is_ignored(self):
        """Defensive: even though site_filter should already restrict
        results, don't trust a non-linkedin.com link if one somehow
        appears."""
        google = FakeGoogleScraper([FakeSearchResult("https://example.com/not-linkedin")])
        checker = LinkedInPresenceChecker(google)
        result = checker.check("Acme Trailer Parts")
        assert result.presence_confirmed is False

    def test_failed_result_entries_are_skipped(self):
        google = FakeGoogleScraper([
            FakeSearchResult("", success=False),
            FakeSearchResult("https://www.linkedin.com/company/acme", success=True),
        ])
        checker = LinkedInPresenceChecker(google)
        result = checker.check("Acme Trailer Parts")
        assert result.presence_confirmed is True
        assert result.linkedin_url == "https://www.linkedin.com/company/acme"
