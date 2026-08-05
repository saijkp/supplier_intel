"""
tests/test_candidate_extractor.py

Tests for discovery/candidate_extractor.py -- purely mechanical
extraction of unique candidate domains from search results, no LLM.
"""

from __future__ import annotations

from types import SimpleNamespace

from discovery.candidate_extractor import extract_candidates


def _result(link, title="", snippet="", success=True):
    return SimpleNamespace(
        success=success, raw_data={"link": link, "title": title, "snippet": snippet},
    )


class TestExtractCandidates:

    def test_extracts_a_candidate_from_a_real_company_domain(self):
        results = [_result("https://acmetrailer.com/", title="Acme Trailer Co")]
        candidates = extract_candidates(results)
        assert len(candidates) == 1
        assert candidates[0].domain == "acmetrailer.com"
        assert candidates[0].title == "Acme Trailer Co"

    def test_filters_out_platform_domains(self):
        results = [_result("https://acme.en.alibaba.com/product.html", title="Acme on Alibaba")]
        candidates = extract_candidates(results)
        assert candidates == []

    def test_filters_out_social_and_directory_domains(self):
        results = [
            _result("https://www.linkedin.com/company/acme", title="Acme on LinkedIn"),
            _result("https://www.facebook.com/acme", title="Acme on Facebook"),
            _result("https://en.wikipedia.org/wiki/Acme", title="Acme - Wikipedia"),
        ]
        candidates = extract_candidates(results)
        assert candidates == []

    def test_dedupes_to_one_candidate_per_registered_domain(self):
        results = [
            _result("https://acmetrailer.com/", title="Acme Home"),
            _result("https://acmetrailer.com/about", title="Acme About"),
            _result("https://www.acmetrailer.com/products", title="Acme Products"),
        ]
        candidates = extract_candidates(results)
        assert len(candidates) == 1

    def test_first_seen_wins_on_dedup(self):
        results = [
            _result("https://acmetrailer.com/", title="First Seen Title"),
            _result("https://acmetrailer.com/about", title="Second Seen Title"),
        ]
        candidates = extract_candidates(results)
        assert candidates[0].title == "First Seen Title"

    def test_skips_failed_results(self):
        results = [_result("https://acmetrailer.com/", success=False)]
        assert extract_candidates(results) == []

    def test_skips_results_without_a_link(self):
        results = [SimpleNamespace(success=True, raw_data={"title": "No link here"})]
        assert extract_candidates(results) == []

    def test_filters_out_cloudflare_email_protection_links(self):
        """Real production bug: a search result pointing at
        https://toyota-4runner.org/cdn-cgi/l/email-protection (an
        obfuscated-mailto redirect that 404s outside a real browser,
        on a domain with no relevance to the search at all) was treated
        as a real candidate and burned a discovery pass on a dead
        fetch. The path marker is the signal, not the domain -- this
        must be excluded regardless of which domain it appears on."""
        results = [_result("https://toyota-4runner.org/cdn-cgi/l/email-protection", title="some forum post")]
        assert extract_candidates(results) == []

    def test_filters_out_industry_portal_domains(self):
        """Real production bug: marklines.com and gasgoo.com (automotive
        industry data/news portals) surfaced as Discovery Service
        candidates for a real "wheel bearing units China" brief and
        burned the entire discovery pass on dead fetches, since neither
        is an individual manufacturer's own site."""
        results = [
            _result("https://www.marklines.com/en/some-report", title="MarkLines report"),
            _result("https://en.gasgoo.com/some-article", title="Gasgoo news"),
        ]
        candidates = extract_candidates(results)
        assert candidates == []

    def test_multiple_distinct_real_companies_all_extracted(self):
        results = [
            _result("https://acmetrailer.com/", title="Acme Trailer Co"),
            _result("https://bestaxles.com/", title="Best Axles Ltd"),
            _result("https://www.linkedin.com/company/acme", title="filtered out"),
        ]
        candidates = extract_candidates(results)
        domains = {c.domain for c in candidates}
        assert domains == {"acmetrailer.com", "bestaxles.com"}
