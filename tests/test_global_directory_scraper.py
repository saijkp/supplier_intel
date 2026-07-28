"""
tests/test_global_directory_scraper.py

Tests for scrapers.global_directory_scraper.GlobalDirectoryScraper.
Focused mainly on the JSON-LD parsing path added after a real
calibration finding: europages_eastern_europe's live page is entirely
Tailwind utility classes with no semantic CSS hooks left for
DIRECTORY_SOURCES' configured selectors to match, while the exact same
page's JSON-LD (schema.org ItemList/Organization) structured-data
block still carries every result's real name/country cleanly. No
network access needed -- everything here operates on plain HTML
strings.
"""

from __future__ import annotations

import json

from scrapers.global_directory_scraper import GlobalDirectoryScraper, _iso_country_name


def _json_ld_page(entries):
    """Build a minimal HTML page embedding one JSON-LD <script> block
    shaped like europages' real one: a top-level @graph array
    containing an ItemList whose itemListElement wraps Organizations."""
    item_list_elements = [
        {
            "@type": "ListItem",
            "position": i + 1,
            "item": {"@type": "Organization", **entry},
        }
        for i, entry in enumerate(entries)
    ]
    graph = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebSite", "name": "example directory"},
            {
                "@type": "ItemList",
                "itemListElement": item_list_elements,
            },
        ],
    }
    return f"<html><head><script type=\"application/ld+json\">{json.dumps(graph)}</script></head><body></body></html>"


def _scraper(directory="europages_eastern_europe"):
    return GlobalDirectoryScraper(directory=directory)


class TestIsoCountryName:

    def test_known_code_resolves_to_full_name(self):
        assert _iso_country_name("DE") == "Germany"
        assert _iso_country_name("GB") == "United Kingdom"

    def test_lowercase_code_still_resolves(self):
        assert _iso_country_name("de") == "Germany"

    def test_unknown_code_returns_none(self):
        assert _iso_country_name("ZZ") is None

    def test_none_and_non_string_return_none(self):
        assert _iso_country_name(None) is None
        assert _iso_country_name(123) is None
        assert _iso_country_name({"name": "Germany"}) is None


class TestParseJsonLdCompanies:

    def test_extracts_real_companies_with_full_country_names(self):
        html = _json_ld_page([
            {
                "name": "Framo Morat GmbH & Co. KG",
                "url": "https://www.europages.co.uk/en/company/framo-morat-973435",
                "address": {"@type": "PostalAddress", "addressLocality": "Eisenbach", "addressCountry": "DE"},
            },
            {
                "name": "Ürmaksan Makine",
                "url": "https://www.europages.co.uk/en/company/uermaksan-22357131",
                "address": {"@type": "PostalAddress", "addressCountry": "TR"},
            },
        ])
        results = _scraper()._parse_companies(html)

        assert len(results) == 2
        assert results[0].raw_data["company_name"] == "Framo Morat GmbH & Co. KG"
        assert results[0].raw_data["country"] == "Germany"
        assert results[1].raw_data["country"] == "Türkiye"

    def test_never_populates_website_from_the_directory_profile_url(self):
        """The URL in this JSON-LD is europages' OWN profile page for
        the company, never the company's real outbound domain --
        populating website/domain with it would collide every company
        onto the same fake shared domain, defeating both the UNIQUE
        constraint on suppliers.domain and domain-exact-match dedup."""
        html = _json_ld_page([{
            "name": "Some Company",
            "url": "https://www.europages.co.uk/en/company/some-company-1",
            "address": {"addressCountry": "FR"},
        }])
        results = _scraper()._parse_companies(html)
        assert "website" not in results[0].raw_data

    def test_source_id_is_the_profile_url(self):
        html = _json_ld_page([{
            "name": "Some Company",
            "url": "https://www.europages.co.uk/en/company/some-company-1",
            "address": {"addressCountry": "FR"},
        }])
        results = _scraper()._parse_companies(html)
        assert results[0].source_id == "https://www.europages.co.uk/en/company/some-company-1"

    def test_entry_with_no_name_is_skipped(self):
        html = _json_ld_page([
            {"name": "", "url": "https://example.com/x", "address": {"addressCountry": "DE"}},
            {"name": "Real Co", "url": "https://example.com/y", "address": {"addressCountry": "DE"}},
        ])
        results = _scraper()._parse_companies(html)
        assert len(results) == 1
        assert results[0].raw_data["company_name"] == "Real Co"

    def test_missing_country_falls_back_to_country_hint(self):
        scraper = _scraper(directory="turkey_tim")  # has a fixed country_hint of "Turkey"
        html = _json_ld_page([{"name": "No Address Co", "url": "https://example.com/z"}])
        results = scraper._parse_companies(html)
        assert results[0].raw_data["country"] == "Turkey"

    def test_unrecognised_country_code_falls_back_to_country_hint(self):
        scraper = _scraper(directory="turkey_tim")
        html = _json_ld_page([{
            "name": "Bad Code Co", "url": "https://example.com/z",
            "address": {"addressCountry": "ZZ"},
        }])
        results = scraper._parse_companies(html)
        assert results[0].raw_data["country"] == "Turkey"


class TestParseCompaniesFallsBackToCssSelectors:
    """No JSON-LD present at all -- the pre-existing CSS-selector path
    (unchanged) must still be reached and must still work, matching
    every directory that doesn't happen to embed structured data."""

    def test_css_selector_path_still_works_without_json_ld(self):
        html = """
        <html><body>
            <div class="member-card">
                <h3 class="member-name">Acme Bearings</h3>
                <span class="member-country">Turkey</span>
                <a class="member-website" href="https://acmebearings.example.com">site</a>
                <a class="member-profile" href="/members/acme-bearings">profile</a>
            </div>
        </body></html>
        """
        results = _scraper(directory="turkey_tim")._parse_companies(html)
        assert len(results) == 1
        assert results[0].raw_data["company_name"] == "Acme Bearings"
        assert results[0].raw_data["website"] == "https://acmebearings.example.com"

    def test_no_json_ld_and_no_matching_cards_returns_empty_not_an_error(self):
        results = _scraper()._parse_companies("<html><body>no results found</body></html>")
        assert results == []


class TestJsonLdMalformedInputNeverRaises:

    def test_invalid_json_in_script_tag_is_skipped(self):
        html = '<html><head><script type="application/ld+json">{not valid json</script></head></html>'
        results = _scraper()._parse_companies(html)
        assert results == []

    def test_json_ld_present_but_not_an_itemlist_is_ignored(self):
        html = (
            '<html><head><script type="application/ld+json">'
            '{"@context": "https://schema.org", "@type": "WebSite", "name": "example"}'
            '</script></head></html>'
        )
        results = _scraper()._parse_companies(html)
        assert results == []

    def test_item_list_element_missing_organization_is_skipped_not_raised(self):
        html = (
            '<html><head><script type="application/ld+json">'
            '{"@graph": [{"@type": "ItemList", "itemListElement": [{"@type": "ListItem", "position": 1}]}]}'
            '</script></head></html>'
        )
        results = _scraper()._parse_companies(html)
        assert results == []
