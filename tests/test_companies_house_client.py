"""
tests/test_companies_house_client.py

Tests for verification/companies_house_client.py -- mocked HTTP, no
real network/API key needed. Confirms the request shape (Basic Auth,
query params) and response parsing (search results, company profile,
address formatting), and that ordinary failures (no key, network
error, no results) never raise -- same contract every other verifier
client in this codebase follows.
"""

from __future__ import annotations

import httpx

from verification.companies_house_client import CompaniesHouseClient, _format_address


class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class FakeHttpClient:
    def __init__(self, json_data=None, status_code=200, raise_exc=None):
        self._json_data = json_data or {}
        self._status_code = status_code
        self._raise_exc = raise_exc
        self.calls = []

    def get(self, url, params=None, auth=None):
        self.calls.append({"url": url, "params": params, "auth": auth})
        if self._raise_exc:
            raise self._raise_exc
        return FakeResponse(self._json_data, self._status_code)


SEARCH_RESPONSE = {
    "items": [
        {"company_number": "01234567", "title": "ACME MATERIAL HANDLING LTD", "company_status": "active", "address_snippet": "1 Main St, London"},
        {"company_number": "07654321", "title": "ACME HOLDINGS LTD", "company_status": "dissolved", "address_snippet": "2 Other St, Leeds"},
    ]
}

PROFILE_RESPONSE = {
    "company_name": "ACME MATERIAL HANDLING LTD",
    "company_status": "active",
    "date_of_creation": "2005-03-14",
    "sic_codes": ["28220"],
    "registered_office_address": {
        "address_line_1": "1 Main St", "locality": "London", "postal_code": "EC1A 1AA", "country": "United Kingdom",
    },
}


class TestSearchCompanies:

    def test_no_api_key_returns_empty_list_without_a_call(self, monkeypatch):
        # api_key=None falls back to config.settings.COMPANIES_HOUSE_API_KEY
        # (same `param or DEFAULT` convention every other client in this
        # codebase uses) -- monkeypatched here so this test asserts the
        # genuinely-no-key path regardless of whatever's actually
        # configured in the real environment's .env.
        monkeypatch.setattr("verification.companies_house_client.COMPANIES_HOUSE_API_KEY", None)
        http_client = FakeHttpClient(json_data=SEARCH_RESPONSE)
        client = CompaniesHouseClient(api_key=None, http_client=http_client)
        assert client.search_companies("Acme Material Handling") == []
        assert http_client.calls == []

    def test_parses_search_results(self):
        http_client = FakeHttpClient(json_data=SEARCH_RESPONSE)
        client = CompaniesHouseClient(api_key="test-key", http_client=http_client)
        matches = client.search_companies("Acme Material Handling", max_results=5)
        assert len(matches) == 2
        assert matches[0].company_number == "01234567"
        assert matches[0].title == "ACME MATERIAL HANDLING LTD"
        assert matches[0].company_status == "active"

    def test_uses_basic_auth_with_blank_password(self):
        http_client = FakeHttpClient(json_data=SEARCH_RESPONSE)
        client = CompaniesHouseClient(api_key="test-key", http_client=http_client)
        client.search_companies("Acme")
        assert http_client.calls[0]["auth"] == ("test-key", "")

    def test_query_param_is_the_raw_company_name(self):
        http_client = FakeHttpClient(json_data=SEARCH_RESPONSE)
        client = CompaniesHouseClient(api_key="test-key", http_client=http_client)
        client.search_companies("Acme Material Handling")
        assert http_client.calls[0]["params"]["q"] == "Acme Material Handling"

    def test_no_results_returns_empty_list(self):
        http_client = FakeHttpClient(json_data={"items": []})
        client = CompaniesHouseClient(api_key="test-key", http_client=http_client)
        assert client.search_companies("Nonexistent Co") == []

    def test_network_error_returns_empty_list_not_raise(self):
        http_client = FakeHttpClient(raise_exc=RuntimeError("connection failed"))
        client = CompaniesHouseClient(api_key="test-key", http_client=http_client)
        assert client.search_companies("Acme") == []

    def test_item_without_a_company_number_is_skipped(self):
        http_client = FakeHttpClient(json_data={"items": [{"title": "No Number Ltd"}]})
        client = CompaniesHouseClient(api_key="test-key", http_client=http_client)
        assert client.search_companies("Acme") == []

    def test_empty_name_returns_empty_list_without_a_call(self):
        http_client = FakeHttpClient(json_data=SEARCH_RESPONSE)
        client = CompaniesHouseClient(api_key="test-key", http_client=http_client)
        assert client.search_companies("") == []
        assert http_client.calls == []


class TestGetCompanyProfile:

    def test_no_api_key_returns_none_without_a_call(self, monkeypatch):
        monkeypatch.setattr("verification.companies_house_client.COMPANIES_HOUSE_API_KEY", None)
        http_client = FakeHttpClient(json_data=PROFILE_RESPONSE)
        client = CompaniesHouseClient(api_key=None, http_client=http_client)
        assert client.get_company_profile("01234567") is None
        assert http_client.calls == []

    def test_parses_profile_fields(self):
        http_client = FakeHttpClient(json_data=PROFILE_RESPONSE)
        client = CompaniesHouseClient(api_key="test-key", http_client=http_client)
        profile = client.get_company_profile("01234567")
        assert profile.company_number == "01234567"
        assert profile.company_name == "ACME MATERIAL HANDLING LTD"
        assert profile.company_status == "active"
        assert profile.date_of_creation == "2005-03-14"
        assert profile.sic_codes == ["28220"]
        assert profile.registered_office_address == "1 Main St, London, EC1A 1AA, United Kingdom"

    def test_source_url_points_at_the_public_company_page(self):
        http_client = FakeHttpClient(json_data=PROFILE_RESPONSE)
        client = CompaniesHouseClient(api_key="test-key", http_client=http_client)
        profile = client.get_company_profile("01234567")
        assert profile.source_url == "https://find-and-update.company-information.service.gov.uk/company/01234567"

    def test_network_error_returns_none_not_raise(self):
        http_client = FakeHttpClient(raise_exc=RuntimeError("timeout"))
        client = CompaniesHouseClient(api_key="test-key", http_client=http_client)
        assert client.get_company_profile("01234567") is None

    def test_missing_registered_office_address_is_none(self):
        http_client = FakeHttpClient(json_data={**PROFILE_RESPONSE, "registered_office_address": {}})
        client = CompaniesHouseClient(api_key="test-key", http_client=http_client)
        profile = client.get_company_profile("01234567")
        assert profile.registered_office_address is None


class TestFormatAddress:

    def test_joins_present_parts_with_commas(self):
        assert _format_address({
            "address_line_1": "1 Main St", "locality": "London", "postal_code": "EC1A 1AA", "country": "UK",
        }) == "1 Main St, London, EC1A 1AA, UK"

    def test_skips_missing_parts(self):
        assert _format_address({"address_line_1": "1 Main St", "postal_code": "EC1A 1AA"}) == "1 Main St, EC1A 1AA"

    def test_empty_dict_returns_none(self):
        assert _format_address({}) is None

    def test_whitespace_only_parts_are_skipped(self):
        assert _format_address({"address_line_1": "1 Main St", "locality": "   "}) == "1 Main St"
