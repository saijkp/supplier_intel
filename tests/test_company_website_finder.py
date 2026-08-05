"""
tests/test_company_website_finder.py

Tests for scrapers.company_website_finder.CompanyWebsiteFinder. The
validation gate is the whole point of this module, so it gets the
most coverage: a search result must never become a supplier's domain
just because it was the top hit -- it has to actually mention the
company's name once fetched.
"""

from __future__ import annotations

from scrapers.company_website_finder import CompanyWebsiteFinder
from scrapers.own_website_scraper import OwnWebsiteFetchResult, OwnWebsitePage


class FakeSearchResult:
    def __init__(self, link, success=True):
        self.raw_data = {"link": link, "title": "result"}
        self.success = success


class FakeGoogleScraper:
    def __init__(self, results=None, raise_error=None):
        self._results = results or []
        self._raise_error = raise_error
        self.queries = []

    def scrape(self, query, max_results=10, **kwargs):
        self.queries.append(query)
        if self._raise_error:
            raise self._raise_error
        return self._results


class FakeOwnWebsiteScraper:
    def __init__(self, result_by_domain=None):
        self._by_domain = result_by_domain or {}
        self.fetched_domains = []

    def fetch(self, domain):
        self.fetched_domains.append(domain)
        if domain in self._by_domain:
            return self._by_domain[domain]
        return OwnWebsiteFetchResult(domain=domain, success=False, error="not configured in fake")


def _finder(search_results, page_text_by_domain=None, **kwargs):
    fetch_results = {
        domain: OwnWebsiteFetchResult(domain=domain, pages=[OwnWebsitePage(url=domain, text=text)])
        for domain, text in (page_text_by_domain or {}).items()
    }
    return CompanyWebsiteFinder(
        google_scraper=FakeGoogleScraper(results=search_results),
        own_website_scraper=FakeOwnWebsiteScraper(result_by_domain=fetch_results),
        **kwargs,
    )


class TestValidationGate:
    """The core property: a candidate is only ever trusted if the
    company's own name is actually findable on the fetched page."""

    def test_matching_name_on_the_page_validates(self):
        finder = _finder(
            [FakeSearchResult("https://acme-trailer.com/")],
            {"acme-trailer.com": "Welcome to Acme Trailer Parts Co., Ltd. — est. 1998"},
        )
        result = finder.find_website("Acme Trailer Parts Co., Ltd.")
        assert result.validated is True
        assert result.domain == "acme-trailer.com"

    def test_unrelated_page_content_does_not_validate(self):
        """The single most important behaviour here: a search result
        that doesn't actually mention the company must never become
        its domain."""
        finder = _finder(
            [FakeSearchResult("https://totally-unrelated-business.com/")],
            {"totally-unrelated-business.com": "We sell garden furniture and patio heaters."},
        )
        result = finder.find_website("Ningbo Acme Trailer Parts Co., Ltd.")
        assert result.validated is False
        assert result.domain is None

    def test_unvalidated_result_still_reports_the_candidate_for_a_human_to_check(self):
        finder = _finder(
            [FakeSearchResult("https://maybe-related.com/")],
            {"maybe-related.com": "Some unrelated content"},
        )
        result = finder.find_website("Acme Trailer Parts")
        assert result.candidate_url == "https://maybe-related.com/"
        assert result.name_match_score is not None

    def test_partial_wording_variation_still_validates(self):
        """Real websites rarely spell their own name exactly like a
        directory listing does -- the match needs some tolerance."""
        finder = _finder(
            [FakeSearchResult("https://acme.example.com/")],
            {"acme.example.com": "Acme Trailer Parts - Manufacturing quality components since 1998"},
        )
        result = finder.find_website("Acme Trailer Parts Co., Ltd.")
        assert result.validated is True


class TestExcludesNonCompanyDomains:

    def test_alibaba_result_is_skipped_in_favour_of_next_candidate(self):
        finder = _finder(
            [
                FakeSearchResult("https://acme.en.alibaba.com/"),
                FakeSearchResult("https://acme-trailer.com/"),
            ],
            {"acme-trailer.com": "Acme Trailer Parts, your trusted manufacturer"},
        )
        result = finder.find_website("Acme Trailer Parts")
        assert result.domain == "acme-trailer.com"

    def test_made_in_china_result_is_skipped(self):
        finder = _finder([FakeSearchResult("https://acme.made-in-china.com/")])
        result = finder.find_website("Acme Trailer Parts")
        assert result.domain is None
        assert result.reason == "no non-platform, non-directory result found"

    def test_social_and_directory_domains_are_skipped(self):
        for domain in ("facebook.com", "linkedin.com", "youtube.com", "yellowpages.com"):
            finder = _finder([FakeSearchResult(f"https://{domain}/acme-trailer")])
            result = finder.find_website("Acme Trailer Parts")
            assert result.domain is None, f"{domain} should have been excluded"

    def test_cloudflare_email_protection_links_are_skipped(self):
        """Same real bug as discovery/candidate_extractor.py's
        equivalent fix -- a /cdn-cgi/ link is never a real page,
        regardless of domain."""
        finder = _finder([FakeSearchResult("https://some-forum.example.com/cdn-cgi/l/email-protection")])
        result = finder.find_website("Acme Trailer Parts")
        assert result.domain is None

    def test_industry_portal_domains_are_skipped(self):
        """Real bug this guards against: marklines.com and gasgoo.com
        (automotive industry data/news portals, not individual
        manufacturer sites) surfaced as Discovery Service "candidates"
        for a real "wheel bearing units China" brief and burned the
        whole search on dead fetches, since neither is a company's own
        website."""
        for domain in ("marklines.com", "gasgoo.com", "thomasnet.com", "globalspec.com",
                        "kompass.com", "panjiva.com", "importgenius.com", "just-auto.com"):
            finder = _finder([FakeSearchResult(f"https://{domain}/acme-trailer")])
            result = finder.find_website("Acme Trailer Parts")
            assert result.domain is None, f"{domain} should have been excluded"

    def test_all_results_excluded_reports_no_candidate_found(self):
        finder = _finder([
            FakeSearchResult("https://facebook.com/acme"),
            FakeSearchResult("https://acme.en.alibaba.com/"),
        ])
        result = finder.find_website("Acme Trailer Parts")
        assert result.domain is None
        assert result.candidate_url is None


class TestSearchQueryConstruction:

    def test_company_name_is_quoted_in_the_query(self):
        google = FakeGoogleScraper(results=[])
        finder = CompanyWebsiteFinder(google, FakeOwnWebsiteScraper())
        finder.find_website("Acme Trailer Parts")
        assert '"Acme Trailer Parts"' in google.queries[0]

    def test_country_is_appended_when_given(self):
        google = FakeGoogleScraper(results=[])
        finder = CompanyWebsiteFinder(google, FakeOwnWebsiteScraper())
        finder.find_website("Acme Trailer Parts", country="China")
        assert "China" in google.queries[0]

    def test_no_country_omits_it_cleanly(self):
        google = FakeGoogleScraper(results=[])
        finder = CompanyWebsiteFinder(google, FakeOwnWebsiteScraper())
        finder.find_website("Acme Trailer Parts")
        assert google.queries[0] == '"Acme Trailer Parts"'


class TestFailureHandling:

    def test_empty_company_name_short_circuits_without_searching(self):
        google = FakeGoogleScraper(results=[])
        finder = CompanyWebsiteFinder(google, FakeOwnWebsiteScraper())
        result = finder.find_website("")
        assert result.domain is None
        assert google.queries == []

    def test_search_failure_is_caught_not_raised(self):
        finder = CompanyWebsiteFinder(
            FakeGoogleScraper(results=[], raise_error=RuntimeError("SerpAPI down")),
            FakeOwnWebsiteScraper(),
        )
        result = finder.find_website("Acme Trailer Parts")
        assert result.validated is False
        assert "search failed" in result.reason

    def test_candidate_fetch_failure_does_not_validate(self):
        finder = _finder([FakeSearchResult("https://dead-site.example.com/")])
        result = finder.find_website("Acme Trailer Parts")
        assert result.validated is False
        assert result.domain is None
        assert "could not fetch" in result.reason

    def test_no_results_at_all_reports_cleanly(self):
        finder = _finder([])
        result = finder.find_website("Acme Trailer Parts")
        assert result.validated is False
        assert result.domain is None


class TestThresholdIsConfigurable:

    def test_stricter_threshold_rejects_a_borderline_match(self):
        finder = _finder(
            [FakeSearchResult("https://acme.example.com/")],
            {"acme.example.com": "A generic page mentioning Acme somewhere in passing"},
            min_name_similarity=99.0,
        )
        result = finder.find_website("Acme Trailer Parts Manufacturing Company")
        assert result.validated is False

    def test_looser_threshold_accepts_the_same_borderline_match(self):
        finder = _finder(
            [FakeSearchResult("https://acme.example.com/")],
            {"acme.example.com": "A generic page mentioning Acme somewhere in passing"},
            min_name_similarity=10.0,
        )
        result = finder.find_website("Acme Trailer Parts Manufacturing Company")
        assert result.validated is True
