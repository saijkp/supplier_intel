"""
tests/test_apollo_contact_finder.py

Tests for verification/apollo_contact_finder.py. Uses a fake injected
HTTP client (no network), following the exact pattern
tests/test_facility_address_verifier.py already established.
"""

from __future__ import annotations

import httpx

from verification.apollo_contact_finder import ApolloContactFinder, _categorise_title


class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class FakeApolloClient:
    """Records every call; routes by URL suffix ('api_search' vs
    'people/match') to separate configurable responses, matching how
    ApolloContactFinder actually calls two distinct endpoints."""

    def __init__(self, search_response=None, enrich_response_by_id=None,
                 raise_on_search=None, raise_on_enrich=None):
        self.search_response = search_response
        self.enrich_response_by_id = enrich_response_by_id or {}
        self.raise_on_search = raise_on_search
        self.raise_on_enrich = raise_on_enrich
        self.search_calls = []
        self.enrich_calls = []

    def post(self, url, headers=None, json=None, params=None):
        if "api_search" in url:
            self.search_calls.append({"url": url, "headers": headers, "json": json})
            if self.raise_on_search:
                raise self.raise_on_search
            return self.search_response
        if "people/match" in url:
            self.enrich_calls.append({"url": url, "headers": headers, "params": params})
            if self.raise_on_enrich:
                raise self.raise_on_enrich
            apollo_id = (params or {}).get("id")
            return self.enrich_response_by_id.get(apollo_id, FakeResponse({"person": {}}))
        raise AssertionError(f"unexpected URL: {url}")


def _search_ok(people):
    return FakeResponse({"people": people})


def _enrich_ok(name, title, email, linkedin_url):
    return FakeResponse({"person": {"name": name, "title": title, "email": email, "linkedin_url": linkedin_url}})


class TestCategoriseTitle:

    def test_procurement_keywords(self):
        for title in ("Procurement Manager", "Head of Purchasing", "Sourcing Lead", "Senior Buyer"):
            assert _categorise_title(title) == "procurement"

    def test_sales_keywords(self):
        for title in ("Sales Manager", "Business Development Director", "Export Manager"):
            assert _categorise_title(title) == "sales"

    def test_ceo_keywords(self):
        for title in ("CEO", "Chief Executive Officer", "President", "Managing Director", "Founder", "Owner"):
            assert _categorise_title(title) == "ceo"

    def test_unmatched_title_is_other_not_dropped(self):
        assert _categorise_title("Warehouse Supervisor") == "other"

    def test_is_case_insensitive(self):
        assert _categorise_title("chief executive officer") == "ceo"

    def test_empty_title_is_other(self):
        assert _categorise_title("") == "other"
        assert _categorise_title(None) == "other"

    def test_ceo_checked_before_procurement_and_sales(self):
        """A title combining roles ('Founder & Head of Sales') should
        resolve deterministically to ceo, not whichever dict key
        happened to iterate first."""
        assert _categorise_title("Founder & Head of Sales") == "ceo"


class TestFindContactsUnavailable:

    def test_missing_api_key_is_unavailable(self):
        finder = ApolloContactFinder(api_key=None, http_client=FakeApolloClient())
        result = finder.find_contacts("Acme Trailer Co", domain="acme.example.com")
        assert result.source == "unavailable"
        assert result.contacts == []

    def test_missing_domain_is_unavailable(self):
        finder = ApolloContactFinder(api_key="test-key", http_client=FakeApolloClient())
        result = finder.find_contacts("Acme Trailer Co", domain=None)
        assert result.source == "unavailable"

    def test_search_request_exception_is_unavailable_not_raised(self):
        client = FakeApolloClient(raise_on_search=httpx.ConnectError("network down"))
        finder = ApolloContactFinder(api_key="test-key", http_client=client)
        result = finder.find_contacts("Acme Trailer Co", domain="acme.example.com")  # must not raise
        assert result.source == "unavailable"

    def test_search_error_status_is_unavailable(self):
        client = FakeApolloClient(search_response=FakeResponse({"error": "unauthorized"}, status_code=401))
        finder = ApolloContactFinder(api_key="bad-key", http_client=client)
        result = finder.find_contacts("Acme Trailer Co", domain="acme.example.com")
        assert result.source == "unavailable"


class TestFindContactsRealNegative:

    def test_genuinely_empty_search_result_is_a_real_negative_not_unavailable(self):
        """Distinct from the failure cases above: a completed search
        that simply found nobody IS real evidence, not "the check
        couldn't run"."""
        client = FakeApolloClient(search_response=_search_ok([]))
        finder = ApolloContactFinder(api_key="test-key", http_client=client)
        result = finder.find_contacts("Acme Trailer Co", domain="acme.example.com")
        assert result.source == "apollo"
        assert result.contacts == []

    def test_search_results_with_no_role_matching_titles_is_a_real_negative(self):
        client = FakeApolloClient(search_response=_search_ok([
            {"id": "p1", "first_name": "Jane", "title": "Warehouse Supervisor"},
        ]))
        finder = ApolloContactFinder(api_key="test-key", http_client=client)
        result = finder.find_contacts("Acme Trailer Co", domain="acme.example.com")
        assert result.source == "apollo"
        assert result.contacts == []
        assert client.enrich_calls == []  # never spend a credit enriching an irrelevant person


class TestFindContactsSuccess:

    def test_finds_and_enriches_a_procurement_contact(self):
        client = FakeApolloClient(
            search_response=_search_ok([{"id": "p1", "first_name": "Jane", "title": "Procurement Manager"}]),
            enrich_response_by_id={"p1": _enrich_ok(
                "Jane Doe", "Procurement Manager", "jane@acme.example.com", "https://linkedin.com/in/janedoe",
            )},
        )
        finder = ApolloContactFinder(api_key="test-key", http_client=client)

        result = finder.find_contacts("Acme Trailer Co", domain="acme.example.com")

        assert result.source == "apollo"
        assert len(result.contacts) == 1
        contact = result.contacts[0]
        assert contact.name == "Jane Doe"
        assert contact.email == "jane@acme.example.com"
        assert contact.linkedin_url == "https://linkedin.com/in/janedoe"
        assert contact.role_category == "procurement"
        assert contact.phone is None  # never implemented in this version -- see module docstring

    def test_enriches_at_most_one_person_per_role_never_more_than_three_total(self):
        """Cost governance: even with many matching candidates, at most
        3 enrichment calls (one per role) -- never one per person
        found in search."""
        client = FakeApolloClient(
            search_response=_search_ok([
                {"id": "p1", "first_name": "A", "title": "Procurement Manager"},
                {"id": "p2", "first_name": "B", "title": "Purchasing Director"},  # also procurement -- must be skipped
                {"id": "p3", "first_name": "C", "title": "Sales Manager"},
                {"id": "p4", "first_name": "D", "title": "CEO"},
                {"id": "p5", "first_name": "E", "title": "Founder"},  # also ceo -- must be skipped
            ]),
            enrich_response_by_id={
                "p1": _enrich_ok("A Person", "Procurement Manager", "a@acme.com", None),
                "p3": _enrich_ok("C Person", "Sales Manager", "c@acme.com", None),
                "p4": _enrich_ok("D Person", "CEO", "d@acme.com", None),
            },
        )
        finder = ApolloContactFinder(api_key="test-key", http_client=client)

        result = finder.find_contacts("Acme Trailer Co", domain="acme.example.com")

        assert len(result.contacts) == 3
        assert len(client.enrich_calls) == 3
        assert {c.role_category for c in result.contacts} == {"procurement", "sales", "ceo"}

    def test_one_contacts_enrichment_failure_falls_back_to_search_data_not_a_whole_failure(self):
        client = FakeApolloClient(
            search_response=_search_ok([{"id": "p1", "first_name": "Jane", "title": "CEO"}]),
            raise_on_enrich=httpx.ConnectError("network down"),
        )
        finder = ApolloContactFinder(api_key="test-key", http_client=client)

        result = finder.find_contacts("Acme Trailer Co", domain="acme.example.com")

        assert result.source == "apollo"
        assert len(result.contacts) == 1
        assert result.contacts[0].name == "Jane"  # fell back to search's first_name
        assert result.contacts[0].email is None

    def test_search_filters_by_domain_and_target_titles(self):
        client = FakeApolloClient(search_response=_search_ok([]))
        finder = ApolloContactFinder(api_key="test-key", http_client=client)

        finder.find_contacts("Acme Trailer Co", domain="acme.example.com")

        sent = client.search_calls[0]["json"]
        assert sent["q_organization_domains_list"] == ["acme.example.com"]
        assert "procurement" in sent["person_titles"]
        assert "ceo" in sent["person_titles"]

    def test_api_key_sent_as_header_on_both_calls(self):
        client = FakeApolloClient(
            search_response=_search_ok([{"id": "p1", "first_name": "Jane", "title": "CEO"}]),
            enrich_response_by_id={"p1": _enrich_ok("Jane Doe", "CEO", "jane@acme.com", None)},
        )
        finder = ApolloContactFinder(api_key="real-key-123", http_client=client)

        finder.find_contacts("Acme Trailer Co", domain="acme.example.com")

        assert client.search_calls[0]["headers"]["x-api-key"] == "real-key-123"
        assert client.enrich_calls[0]["headers"]["x-api-key"] == "real-key-123"
