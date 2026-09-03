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


class FakeLLMClient:
    """Same shape as tests/test_discovery_candidate_validator.py's own
    fake -- `response` is whatever complete_json should return this
    call (a dict, or None to simulate "found nothing stated" / a
    failed call, since llm.client.LLMClient.complete_json() never
    raises, only returns None on failure)."""

    def __init__(self, response=None):
        self._response = response
        self.calls = []

    def complete_json(self, system_prompt, user_prompt, **kwargs):
        self.calls.append((system_prompt, user_prompt))
        return self._response


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

    def test_additional_b2b_marketplace_domains_are_skipped(self):
        """Real bug this guards against: tradekey.com surfaced as a
        candidate for a real "trailer axle China" brief -- a malformed
        contact-page href on it also crashed the sub-page fetcher
        (see test_own_website_scraper.py's mailto: fix)."""
        for domain in ("tradekey.com", "dhgate.com", "ec21.com",
                        "exportersindia.com", "go4worldbusiness.com"):
            finder = _finder([FakeSearchResult(f"https://{domain}/acme-trailer")])
            result = finder.find_website("Acme Trailer Parts")
            assert result.domain is None, f"{domain} should have been excluded"

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

    def test_stock_media_domains_are_skipped_across_tld_variants(self):
        """Real bug this guards against: gettyimages.nl was VALIDATED as
        a "trailer mudguard manufacturer" candidate by discovery's
        CandidateValidator -- a stock-photo listing/caption page
        happened to mention "trailer mudguard". Matched on the
        registered domain's LABEL alone (not the full registered-domain
        string every other exclusion here uses), since these platforms
        operate region-specific TLDs -- gettyimages.com, .nl, .co.uk,
        .de are all the same real platform."""
        for domain in ("gettyimages.com", "gettyimages.nl", "gettyimages.co.uk",
                        "istockphoto.com", "shutterstock.com", "alamy.com",
                        "dreamstime.com", "123rf.com", "depositphotos.com"):
            finder = _finder([FakeSearchResult(f"https://{domain}/photo/trailer-mudguard")])
            result = finder.find_website("Acme Trailer Parts")
            assert result.domain is None, f"{domain} should have been excluded"

    def test_domain_merely_containing_a_similar_substring_is_not_excluded(self):
        """The stock-media check compares the registered domain's LABEL
        exactly (via tldextract), not a substring -- a real company
        whose name happens to share a fragment with a stock-media brand
        must not be falsely excluded."""
        finder = _finder(
            [FakeSearchResult("https://gettysburgtools.com/")],
            {"gettysburgtools.com": "Gettysburg Tools - a real manufacturer since 1950"},
        )
        result = finder.find_website("Gettysburg Tools")
        assert result.domain == "gettysburgtools.com"

    def test_companies_house_own_domain_is_skipped(self):
        """Real bug this guards against: a small UK company with no
        strong independent web presence surfaces ITS OWN Companies
        House profile page as the top search result, which trivially
        "validates" (the profile page literally contains the company's
        own registered name) -- worse than just a wasted candidate,
        MULTIPLE different real companies all "resolving" to this
        exact same domain string tripped
        discovery.companies_house_sic_source's own within-batch
        seen_domains dedup, silently dropping every occurrence after
        the first as a false duplicate."""
        for domain in (
            "find-and-update.company-information.service.gov.uk",
            "www.tax.service.gov.uk",
        ):
            finder = _finder([FakeSearchResult(f"https://{domain}/company/12345678")])
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


class TestGroundedMatchGuards:
    """Two real false matches confirmed live in production, both from
    the SAME underlying gap: fuzz.partial_ratio against raw page text
    has no concept of "is this actually the company's own site," only
    "does something on this page look similar." Fixed with two
    additional, independent gates -- see find_website's own inline
    comments for the exact live cases."""

    def test_ashpock_shpock_false_match_is_now_rejected(self):
        """Real case: "Ashpock" (intended: Aspock/Aspoeck, the trailer-
        lighting manufacturer) resolved to shpock.com (Shpock, an
        unrelated classifieds app) -- fuzz.partial_ratio scores this
        highly because "shpock" aligns as a near-perfect substring of
        "ashpock". The distinctive-token guard rejects it: the literal
        word "ashpock" never appears anywhere on shpock.com's page."""
        finder = _finder(
            [FakeSearchResult("https://shpock.com/")],
            {"shpock.com": "Shpock is the marketplace app for buying and selling locally."},
            min_name_similarity=10.0,  # isolate the distinctive-token gate, not the score gate
        )
        result = finder.find_website("Ashpock")
        assert result.validated is False
        assert "distinctive name" in result.reason

    def test_legitimate_match_still_passes_the_distinctive_token_guard(self):
        """The real company's own site literally says its name --
        the guard must not become a blanket rejection."""
        finder = _finder(
            [FakeSearchResult("https://aspoeck.com/")],
            {"aspoeck.com": "Welcome to Aspoeck Systems -- trailer lighting since 1958."},
            llm_client=FakeLLMClient(response={"company_name": "Aspoeck Systems", "country": None}),
        )
        result = finder.find_website("Aspoeck")
        assert result.validated is True

    def test_ik_eng_third_party_mention_is_now_rejected(self):
        """Real case: "IK Eng Ltd" resolved to easydigitalfiling.com, a
        UK company-formation/filing agent's site that merely lists the
        name among its client records. "IK Eng Ltd" has no word >=4
        characters once "Ltd" is stripped, so the distinctive-token
        guard alone has no signal to reject on (same "insufficient
        signal, don't invent a rejection" rule as everywhere else) --
        this needs the grounded-extraction gate: the LLM reads the
        page's own stated identity (footer/copyright), which is the
        filing agent's name, not the searched company's."""
        finder = _finder(
            [FakeSearchResult("https://easydigitalfiling.com/")],
            {"easydigitalfiling.com": "IK Eng Ltd -- company formation completed. "
                                      "(c) Easy Digital Filing Ltd, a UK company formation agent."},
            llm_client=FakeLLMClient(response={"company_name": "Easy Digital Filing Ltd", "country": "United Kingdom"}),
        )
        result = finder.find_website("IK Eng Ltd")
        assert result.validated is False
        assert "does not corroborate" in result.reason

    def test_ik_eng_real_site_still_validates(self):
        """The real ikeng.co.uk site states its own name in the
        footer -- the grounded extraction corroborates it even though
        "IK Eng Ltd" has no >=4-character distinctive word (this is
        exactly names_plausibly_corroborate's short-name ratio
        fallback, not the token-overlap path)."""
        finder = _finder(
            [FakeSearchResult("https://ikeng.co.uk/")],
            {"ikeng.co.uk": "Precision engineering services. (c) IK Eng Ltd, registered in England."},
            llm_client=FakeLLMClient(response={"company_name": "IK Eng Ltd", "country": "United Kingdom"}),
        )
        result = finder.find_website("IK Eng Ltd")
        assert result.validated is True

    def test_grounded_extraction_finding_nothing_does_not_reject(self):
        """Many real product-catalogue homepages never state the
        company name at all in the first ~8,000 characters -- absence
        of a grounded extraction must not undo a fuzzy/distinctive-
        token match that already passed on real evidence."""
        finder = _finder(
            [FakeSearchResult("https://acme-trailer.com/")],
            {"acme-trailer.com": "Welcome to Acme Trailer Parts Co., Ltd. -- est. 1998"},
            llm_client=FakeLLMClient(response={"company_name": None, "country": None}),
        )
        result = finder.find_website("Acme Trailer Parts Co., Ltd.")
        assert result.validated is True

    def test_grounded_extraction_call_failure_does_not_reject(self):
        """llm.client.LLMClient.complete_json() never raises by
        contract, but this module must not depend on that -- a raising
        fake still must not cost a real, already-passing match."""
        class RaisingLLMClient:
            def complete_json(self, *args, **kwargs):
                raise RuntimeError("simulated transient failure")

        finder = _finder(
            [FakeSearchResult("https://acme-trailer.com/")],
            {"acme-trailer.com": "Welcome to Acme Trailer Parts Co., Ltd. -- est. 1998"},
            llm_client=RaisingLLMClient(),
        )
        result = finder.find_website("Acme Trailer Parts Co., Ltd.")
        assert result.validated is True
