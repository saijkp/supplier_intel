"""
tests/test_companies_house_sic_source.py

Tests for discovery/companies_house_sic_source.py -- the Companies
House SIC-search candidate-generation path alongside serpapi/llm in
discovery_service.py. Fakes both CompaniesHouseClient.search_by_sic_codes()
and CompanyWebsiteFinder.find_website() entirely (no network/API key
needed); nothing here touches CandidateValidator or SupplierMatcher --
this module's own job stops at "produce a list of Candidate objects,"
same split test_llm_candidate_source.py already follows for the LLM
source.
"""

from __future__ import annotations

from verification.companies_house_client import SicSearchMatch
from scrapers.company_website_finder import WebsiteFindingResult
from discovery.companies_house_sic_source import CompaniesHouseSicSource


class FakeCompaniesHouseClient:
    def __init__(self, matches=None):
        self._matches = matches or []
        self.calls = []

    def search_by_sic_codes(self, sic_codes, max_results=100, company_status="active"):
        self.calls.append((sic_codes, max_results, company_status))
        return self._matches[:max_results]


class FakeWebsiteFinder:
    """`results` maps company_name -> WebsiteFindingResult; a name with
    no entry defaults to "not found" so a test only needs to specify
    the names it cares about. `raise_for_name` injects an exception for
    fault-isolation tests, mirroring FakeCandidateValidator's
    `raise_for_domain` convention elsewhere in this test suite."""

    def __init__(self, results=None, raise_for_name=None):
        self._results = results or {}
        self._raise_for_name = raise_for_name
        self.calls = []

    def find_website(self, company_name, country=None):
        self.calls.append((company_name, country))
        if self._raise_for_name and company_name == self._raise_for_name:
            raise RuntimeError("website finder exploded")
        return self._results.get(company_name, WebsiteFindingResult(
            company_name=company_name, domain=None, validated=False,
            candidate_url=None, name_match_score=None, reason="not configured in test fake",
        ))


def _match(company_name="Acme Handling Ltd", company_number="01234567", **overrides):
    fields = {
        "company_number": company_number, "company_name": company_name,
        "company_status": "active", "sic_codes": ["28220"],
        "registered_office_address": "1 Main St, London", "date_of_creation": "2005-03-14",
        "source_url": f"https://find-and-update.company-information.service.gov.uk/company/{company_number}",
    }
    fields.update(overrides)
    return SicSearchMatch(**fields)


def _found(company_name="Acme Handling Ltd", domain="acmehandling.co.uk"):
    return WebsiteFindingResult(
        company_name=company_name, domain=domain, validated=True,
        candidate_url=f"https://{domain}", name_match_score=95.0,
        reason="company name matched candidate site text (score=95)",
    )


class TestFindCandidatesHappyPath:

    def test_matched_company_with_a_validated_website_produces_a_candidate(self):
        ch_client = FakeCompaniesHouseClient(matches=[_match()])
        finder = FakeWebsiteFinder(results={"Acme Handling Ltd": _found()})
        source = CompaniesHouseSicSource(companies_house_client=ch_client, website_finder=finder)

        candidates, stats = source.find_candidates(["28220"])

        assert len(candidates) == 1
        assert candidates[0].title == "Acme Handling Ltd"
        assert candidates[0].domain == "acmehandling.co.uk"
        assert candidates[0].link == "https://acmehandling.co.uk"
        assert stats.companies_found == 1
        assert stats.deduplicated == 1

    def test_snippet_carries_companies_house_provenance(self):
        ch_client = FakeCompaniesHouseClient(matches=[_match(company_number="09999999", sic_codes=["28220", "46690"])])
        finder = FakeWebsiteFinder(results={"Acme Handling Ltd": _found()})
        source = CompaniesHouseSicSource(companies_house_client=ch_client, website_finder=finder)

        candidates, _ = source.find_candidates(["28220"])

        assert "09999999" in candidates[0].snippet
        assert "28220" in candidates[0].snippet
        assert "46690" in candidates[0].snippet
        assert "1 Main St, London" in candidates[0].snippet

    def test_sic_codes_and_max_results_are_forwarded_to_the_ch_client(self):
        ch_client = FakeCompaniesHouseClient(matches=[])
        finder = FakeWebsiteFinder()
        source = CompaniesHouseSicSource(companies_house_client=ch_client, website_finder=finder)

        source.find_candidates(["28220", "46140"], max_candidates=15)

        assert ch_client.calls == [(["28220", "46140"], 15, "active")]


class TestFindCandidatesFiltering:

    def test_company_with_no_validated_website_is_dropped(self):
        ch_client = FakeCompaniesHouseClient(matches=[_match()])
        finder = FakeWebsiteFinder()  # no entry for "Acme Handling Ltd" -> defaults to not-found
        source = CompaniesHouseSicSource(companies_house_client=ch_client, website_finder=finder)

        candidates, stats = source.find_candidates(["28220"])

        assert candidates == []
        assert stats.companies_found == 1
        assert stats.no_website_found == 1

    def test_unvalidated_website_result_is_dropped(self):
        ch_client = FakeCompaniesHouseClient(matches=[_match()])
        unvalidated = WebsiteFindingResult(
            company_name="Acme Handling Ltd", domain="acmehandling.co.uk", validated=False,
            candidate_url="https://acmehandling.co.uk", name_match_score=30.0, reason="name did not match",
        )
        finder = FakeWebsiteFinder(results={"Acme Handling Ltd": unvalidated})
        source = CompaniesHouseSicSource(companies_house_client=ch_client, website_finder=finder)

        candidates, stats = source.find_candidates(["28220"])

        assert candidates == []
        assert stats.no_website_found == 1

    def test_website_finder_raising_does_not_abort_other_candidates(self):
        ch_client = FakeCompaniesHouseClient(matches=[
            _match(company_name="Boom Co", company_number="11111111"),
            _match(company_name="Acme Handling Ltd", company_number="22222222"),
        ])
        finder = FakeWebsiteFinder(
            results={"Acme Handling Ltd": _found()}, raise_for_name="Boom Co",
        )
        source = CompaniesHouseSicSource(companies_house_client=ch_client, website_finder=finder)

        candidates, stats = source.find_candidates(["28220"])

        assert len(candidates) == 1
        assert candidates[0].title == "Acme Handling Ltd"
        assert stats.no_website_found == 1

    def test_two_companies_resolving_to_the_same_domain_are_deduplicated(self):
        ch_client = FakeCompaniesHouseClient(matches=[
            _match(company_name="Acme Handling Ltd", company_number="11111111"),
            _match(company_name="Acme Handling (Trading Name) Ltd", company_number="22222222"),
        ])
        finder = FakeWebsiteFinder(results={
            "Acme Handling Ltd": _found(domain="acmehandling.co.uk"),
            "Acme Handling (Trading Name) Ltd": _found(company_name="Acme Handling (Trading Name) Ltd", domain="acmehandling.co.uk"),
        })
        source = CompaniesHouseSicSource(companies_house_client=ch_client, website_finder=finder)

        candidates, stats = source.find_candidates(["28220"])

        assert len(candidates) == 1
        assert stats.deduplicated == 1

    def test_stops_at_max_candidates(self):
        ch_client = FakeCompaniesHouseClient(matches=[
            _match(company_name=f"Company {i}", company_number=str(i).zfill(8)) for i in range(5)
        ])
        finder = FakeWebsiteFinder(results={
            f"Company {i}": _found(company_name=f"Company {i}", domain=f"company{i}.co.uk") for i in range(5)
        })
        source = CompaniesHouseSicSource(companies_house_client=ch_client, website_finder=finder)

        candidates, _ = source.find_candidates(["28220"], max_candidates=2)

        assert len(candidates) == 2

    def test_no_ch_matches_produces_no_candidates(self):
        ch_client = FakeCompaniesHouseClient(matches=[])
        finder = FakeWebsiteFinder()
        source = CompaniesHouseSicSource(companies_house_client=ch_client, website_finder=finder)

        candidates, stats = source.find_candidates(["99999"])

        assert candidates == []
        assert stats.companies_found == 0
