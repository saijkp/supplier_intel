"""
tests/test_facility_address_verifier.py

Tests for verification.facility_address_verifier. The routing tests
matter most -- China must go to Amap, everyone else to Google Places,
with no accidental fallthrough either direction.
"""

from __future__ import annotations

import httpx

from verification.facility_address_verifier import (
    AmapAddressVerifier,
    GooglePlacesAddressVerifier,
    select_address_verifier,
)


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
    def __init__(self, response_json=None, raise_error=None):
        self._response_json = response_json
        self._raise_error = raise_error
        self.last_params = None

    def get(self, url, params=None):
        self.last_params = params
        if self._raise_error:
            raise self._raise_error
        return FakeResponse(self._response_json)


class TestGooglePlacesAddressVerifier:

    def test_matching_candidate_is_verified(self):
        client = FakeHttpClient({
            "candidates": [{"formatted_address": "123 Factory Rd, Shenzhen, China", "name": "Acme Co"}],
        })
        verifier = GooglePlacesAddressVerifier(api_key="test-key", http_client=client)
        result = verifier.verify("123 Factory Rd, Shenzhen", company_name="Acme Co")
        assert result.verified is True
        assert result.source == "google_places"
        assert result.formatted_address == "123 Factory Rd, Shenzhen, China"

    def test_no_candidates_is_not_verified_not_an_error(self):
        client = FakeHttpClient({"candidates": []})
        verifier = GooglePlacesAddressVerifier(api_key="test-key", http_client=client)
        result = verifier.verify("some fabricated address")
        assert result.verified is False
        assert result.reason == "no matching place found"

    def test_missing_api_key_reports_unavailable_not_a_crash(self):
        verifier = GooglePlacesAddressVerifier(api_key=None, http_client=FakeHttpClient({}))
        result = verifier.verify("123 Factory Rd")
        assert result.verified is False
        assert result.source == "unavailable"

    def test_empty_address_short_circuits(self):
        verifier = GooglePlacesAddressVerifier(api_key="test-key", http_client=FakeHttpClient({}))
        result = verifier.verify("")
        assert result.verified is False

    def test_request_failure_is_caught_not_raised(self):
        client = FakeHttpClient(raise_error=httpx.ConnectError("network down"))
        verifier = GooglePlacesAddressVerifier(api_key="test-key", http_client=client)
        result = verifier.verify("123 Factory Rd")
        assert result.verified is False
        assert "request failed" in result.reason

    def test_company_name_and_address_are_both_sent_in_the_query(self):
        client = FakeHttpClient({"candidates": []})
        verifier = GooglePlacesAddressVerifier(api_key="test-key", http_client=client)
        verifier.verify("123 Factory Rd", company_name="Acme Co")
        assert "Acme Co" in client.last_params["input"]
        assert "123 Factory Rd" in client.last_params["input"]


class TestAmapAddressVerifier:

    def test_successful_geocode_is_verified(self):
        client = FakeHttpClient({
            "status": "1", "info": "OK",
            "geocodes": [{"formatted_address": "浙江省宁波市..."}],
        })
        verifier = AmapAddressVerifier(api_key="test-key", http_client=client)
        result = verifier.verify("宁波市工业路123号")
        assert result.verified is True
        assert result.source == "amap"

    def test_empty_geocodes_is_not_verified(self):
        client = FakeHttpClient({"status": "1", "info": "OK", "geocodes": []})
        verifier = AmapAddressVerifier(api_key="test-key", http_client=client)
        result = verifier.verify("a fabricated address")
        assert result.verified is False
        assert result.reason == "no matching place found"

    def test_amap_error_status_is_reported_distinctly(self):
        """Amap signals failure via status='0' (a string), not an HTTP
        error code -- must not be misread as a successful empty
        result."""
        client = FakeHttpClient({"status": "0", "info": "INVALID_USER_KEY"})
        verifier = AmapAddressVerifier(api_key="bad-key", http_client=client)
        result = verifier.verify("宁波市工业路123号")
        assert result.verified is False
        assert "INVALID_USER_KEY" in result.reason

    def test_missing_api_key_mentions_registration_friction(self):
        verifier = AmapAddressVerifier(api_key=None, http_client=FakeHttpClient({}))
        result = verifier.verify("宁波市工业路123号")
        assert result.verified is False
        assert result.source == "unavailable"
        assert "registration friction" in result.reason

    def test_request_failure_is_caught_not_raised(self):
        client = FakeHttpClient(raise_error=httpx.ConnectError("network down"))
        verifier = AmapAddressVerifier(api_key="test-key", http_client=client)
        result = verifier.verify("宁波市工业路123号")
        assert result.verified is False


class TestCountryRouting:

    def test_china_routes_to_amap(self):
        google = GooglePlacesAddressVerifier(api_key="g", http_client=FakeHttpClient({}))
        amap = AmapAddressVerifier(api_key="a", http_client=FakeHttpClient({}))
        assert select_address_verifier("China", google, amap) is amap
        assert select_address_verifier("CN", google, amap) is amap

    def test_china_routing_is_case_insensitive(self):
        google = GooglePlacesAddressVerifier(api_key="g", http_client=FakeHttpClient({}))
        amap = AmapAddressVerifier(api_key="a", http_client=FakeHttpClient({}))
        assert select_address_verifier("china", google, amap) is amap
        assert select_address_verifier("cn", google, amap) is amap

    def test_hong_kong_and_macao_are_not_routed_to_amap(self):
        """Deliberate: full Google Maps coverage exists for HK/Macao,
        they aren't behind the same access barrier as the mainland."""
        google = GooglePlacesAddressVerifier(api_key="g", http_client=FakeHttpClient({}))
        amap = AmapAddressVerifier(api_key="a", http_client=FakeHttpClient({}))
        assert select_address_verifier("Hong Kong", google, amap) is google
        assert select_address_verifier("Macao", google, amap) is google

    def test_other_countries_route_to_google(self):
        google = GooglePlacesAddressVerifier(api_key="g", http_client=FakeHttpClient({}))
        amap = AmapAddressVerifier(api_key="a", http_client=FakeHttpClient({}))
        assert select_address_verifier("India", google, amap) is google
        assert select_address_verifier("United Kingdom", google, amap) is google

    def test_missing_country_defaults_to_google(self):
        google = GooglePlacesAddressVerifier(api_key="g", http_client=FakeHttpClient({}))
        amap = AmapAddressVerifier(api_key="a", http_client=FakeHttpClient({}))
        assert select_address_verifier(None, google, amap) is google
        assert select_address_verifier("", google, amap) is google
