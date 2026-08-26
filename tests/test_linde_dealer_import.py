"""
tests/test_linde_dealer_import.py

Tests for discovery/linde_dealer_import.py -- fetch + liveness-filter
+ import via the SAME dedup engine every other source uses. No real
network: fetch_linde_dealers()/check_website_live() both take an
injectable http_client (fake httpx.Client shape), and
import_linde_dealer_network() additionally accepts a fake
website_checker callable directly, so most tests don't need to fake
HTTP at all.
"""

from __future__ import annotations

import pytest

from deduplication.matcher import SupplierMatcher
from discovery.linde_dealer_import import (
    LINDE_DEALER_DATA_URL,
    _LINDE_OWNED_DOMAINS,
    _normalize_malformed_scheme,
    check_website_live,
    fetch_linde_dealers,
    import_linde_dealer_network,
)
from storage.database import initialise_schema
from storage.repository import SupplierRepository


@pytest.fixture()
def repo(tmp_path):
    db_path = tmp_path / "test.db"
    initialise_schema(db_path)
    return SupplierRepository(db_path=db_path)


def _dealer(name="A.G. Pruden & Cia. S.A.", website="https://www.agpruden.com", **overrides):
    base = {
        "city": "Buenos Aires", "country": "ar", "mail": "ventas@agpruden.com",
        "name": name, "phone": "+54 11 4733-2500",
        "street": "Av. Hipolito Yrigoyen 2441", "website": website, "zip": "B1640HFW",
    }
    base.update(overrides)
    return base


class FakeResponse:
    def __init__(self, json_data=None, status_code=200, text=""):
        self._json = json_data
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttpxClient:
    """`responses` maps url -> FakeResponse; a url with no entry
    defaults to a 404, matching FakeCandidateValidator's own
    "unconfigured means fail" convention elsewhere in this suite."""

    def __init__(self, responses=None, raise_for_url=None):
        self._responses = responses or {}
        self._raise_for_url = raise_for_url
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        if self._raise_for_url and url == self._raise_for_url:
            raise RuntimeError("connection failed")
        return self._responses.get(url, FakeResponse({}, status_code=404))


class TestFetchLindeDealers:

    def test_parses_the_dealers_list(self):
        client = FakeHttpxClient(responses={
            LINDE_DEALER_DATA_URL: FakeResponse({"dealers": [_dealer(), _dealer(name="Other Co")]}),
        })
        dealers = fetch_linde_dealers(http_client=client)
        assert len(dealers) == 2
        assert dealers[0]["name"] == "A.G. Pruden & Cia. S.A."

    def test_missing_dealers_key_returns_empty_list(self):
        client = FakeHttpxClient(responses={LINDE_DEALER_DATA_URL: FakeResponse({})})
        assert fetch_linde_dealers(http_client=client) == []

    def test_http_error_raises(self):
        """Unlike per-candidate scrapers, a failed bulk fetch means
        there is nothing at all to import -- this is expected to raise,
        not silently return an empty list."""
        client = FakeHttpxClient(responses={LINDE_DEALER_DATA_URL: FakeResponse({}, status_code=500)})
        with pytest.raises(RuntimeError):
            fetch_linde_dealers(http_client=client)


class TestCheckWebsiteLive:

    def test_2xx_is_live(self):
        client = FakeHttpxClient(responses={"https://real.example": FakeResponse({}, status_code=200)})
        assert check_website_live("https://real.example", http_client=client) is True

    def test_3xx_is_live(self):
        client = FakeHttpxClient(responses={"https://real.example": FakeResponse({}, status_code=301)})
        assert check_website_live("https://real.example", http_client=client) is True

    def test_4xx_is_dead(self):
        client = FakeHttpxClient(responses={"https://dead.example": FakeResponse({}, status_code=404)})
        assert check_website_live("https://dead.example", http_client=client) is False

    def test_connection_failure_is_dead_not_raised(self):
        client = FakeHttpxClient(raise_for_url="https://unreachable.example")
        assert check_website_live("https://unreachable.example", http_client=client) is False

    def test_empty_url_is_dead(self):
        assert check_website_live("", http_client=FakeHttpxClient()) is False

    def test_parking_page_text_is_dead_despite_200(self):
        """Real finding: tq-linde.vn returns HTTP 200 but serves a bare
        default nginx page -- a status-code-only check would wrongly
        count this as live."""
        client = FakeHttpxClient(responses={
            "https://parked.example": FakeResponse(
                status_code=200,
                text="<html><body>Welcome to nginx! If you see this page, the nginx web "
                     "server is successfully installed and working.</body></html>",
            ),
        })
        assert check_website_live("https://parked.example", http_client=client) is False

    def test_real_content_with_200_is_live(self):
        client = FakeHttpxClient(responses={
            "https://real.example": FakeResponse(
                status_code=200, text="<html><body>Acme Forklifts Ltd -- your local dealer.</body></html>",
            ),
        })
        assert check_website_live("https://real.example", http_client=client) is True


class TestNormalizeMalformedScheme:

    def test_single_slash_after_http_is_fixed(self):
        """Real finding: Bravo Montacargas / Lift Truck Service Center
        entries use "http:/www.example.com" (one slash) in Linde's own
        raw data."""
        assert _normalize_malformed_scheme("http:/www.bravomontacargas.com") == "http://www.bravomontacargas.com"

    def test_single_slash_after_https_is_fixed(self):
        assert _normalize_malformed_scheme("https:/www.example.com") == "https://www.example.com"

    def test_well_formed_url_is_unchanged(self):
        assert _normalize_malformed_scheme("https://www.agpruden.com") == "https://www.agpruden.com"

    def test_strips_surrounding_whitespace(self):
        assert _normalize_malformed_scheme("  https://www.agpruden.com  ") == "https://www.agpruden.com"


class TestLindeOwnedDomains:

    def test_generic_corporate_template_domains_are_excluded(self):
        """Every entry confirmed via a real fetch showing Linde's own
        shared "Homepage Linde Material Handling" template -- see this
        set's own module-level docstring for the per-domain evidence."""
        assert "linde-mh.com" in _LINDE_OWNED_DOMAINS
        assert "linde-mh.es" in _LINDE_OWNED_DOMAINS
        assert "linde-mh.co.za" not in _LINDE_OWNED_DOMAINS  # never actually confirmed live (timed out); not excluded on a guess

    def test_linde_mh_hu_is_excluded_same_template_family_as_linde_mh_it(self):
        """Real correction found via a full-370-run spot check: this
        was missed in the first classification pass -- same "Linde's
        own [Country] entity" naming pattern as linde-mh.it, not a
        genuinely distinct third party."""
        assert "linde-mh.hu" in _LINDE_OWNED_DOMAINS

    def test_genuine_independent_distributors_are_not_excluded(self):
        """Real, distinct local companies confirmed via fetch -- same
        rigor fenwick-linde.fr itself was checked with, not a blanket
        "contains linde" pattern match."""
        assert "fenwick-linde.fr" not in _LINDE_OWNED_DOMAINS
        assert "linde-hl.cl" not in _LINDE_OWNED_DOMAINS       # "Linde High Lift Chile S.A." -- distinct subsidiary brand
        assert "lindemh.co.kr" not in _LINDE_OWNED_DOMAINS     # real Korean company "SAY T&C"
        assert "lindemh.com.au" not in _LINDE_OWNED_DOMAINS    # real, substantive region-specific promotional content


class TestImportLindeDealerNetwork:

    def _network(self, repo, dealers, live_domains=frozenset()):
        client = FakeHttpxClient(responses={LINDE_DEALER_DATA_URL: FakeResponse({"dealers": dealers})})
        checker = lambda url: url in live_domains
        return import_linde_dealer_network(
            repo, SupplierMatcher(repo), http_client=client, website_checker=checker,
        )

    def test_live_dealer_creates_a_new_supplier(self, repo):
        dealer = _dealer()
        stats = self._network(repo, [dealer], live_domains={dealer["website"]})

        assert stats.website_live == 1
        assert stats.website_dead == 0
        assert stats.static_import.created == 1
        supplier = repo.find_by_domain("agpruden.com")
        assert supplier["canonical_name"] == "A.G. Pruden & Cia. S.A."
        assert supplier["domain"] == "agpruden.com"
        assert supplier["discovery_source"] == "linde-oem-dealer-network"

    def test_dead_website_dealer_is_skipped_not_imported(self, repo):
        dealer = _dealer()
        stats = self._network(repo, [dealer], live_domains=frozenset())  # nothing is live

        assert stats.website_dead == 1
        assert stats.static_import.total == 0
        assert repo.find_by_domain("agpruden.com") is None

    def test_dead_website_dealer_is_still_recorded_in_raw_source_data(self, repo):
        dealer = _dealer()
        self._network(repo, [dealer], live_domains=frozenset())

        from storage.database import connection_scope
        with connection_scope(repo.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM raw_source_data WHERE source = 'linde-oem-dealer-network'"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["processing_status"] == "failed"
        assert "did not resolve" in rows[0]["error_message"]

    def test_dealer_with_no_website_is_imported_anyway(self, repo):
        dealer = _dealer(website="")
        stats = self._network(repo, [dealer], live_domains=frozenset())

        assert stats.no_website == 1
        assert stats.website_live == 0
        assert stats.website_dead == 0
        assert stats.static_import.created == 1

    def test_limit_caps_how_many_dealers_are_processed(self, repo):
        dealers = [_dealer(name=f"Dealer {i}", website=f"https://dealer{i}.example") for i in range(5)]
        client = FakeHttpxClient(responses={LINDE_DEALER_DATA_URL: FakeResponse({"dealers": dealers})})
        stats = import_linde_dealer_network(
            repo, SupplierMatcher(repo), limit=2, http_client=client, website_checker=lambda url: True,
        )
        assert stats.total_dealers == 2

    def test_malformed_scheme_is_normalized_before_domain_extraction(self, repo):
        """Real finding: "http:/www.bravomontacargas.com" (one slash)
        must resolve to domain "bravomontacargas.com", not the
        parse-broken "http" that would collide every dealer hitting
        this typo into one shared golden record."""
        dealer = _dealer(name="Bravo Montacargas Central", website="http:/www.bravomontacargas.com")
        client = FakeHttpxClient(responses={LINDE_DEALER_DATA_URL: FakeResponse({"dealers": [dealer]})})
        stats = import_linde_dealer_network(
            repo, SupplierMatcher(repo), http_client=client,
            website_checker=lambda url: url == "http://www.bravomontacargas.com",
        )

        assert stats.website_live == 1
        supplier = repo.find_by_domain("bravomontacargas.com")
        assert supplier is not None
        assert supplier["canonical_name"] == "Bravo Montacargas Central"

    def test_linde_owned_domain_is_imported_without_a_domain(self, repo):
        """Real finding: Alkhorayef Commercial's own listed "website"
        is a linde-mh.com link with a UTM tracking parameter -- Linde's
        own portal, not Alkhorayef's own site. Must still import
        (name/address/phone are real and valuable), just with no
        domain, so it can never falsely collide with another dealer
        sharing the same Linde-owned fallback URL."""
        dealer = _dealer(name="Alkhorayef Commercial", website="https://www.linde-mh.com/en/?utm_source=5571")
        client = FakeHttpxClient(responses={LINDE_DEALER_DATA_URL: FakeResponse({"dealers": [dealer]})})
        # website_checker deliberately NEVER called with a live/True response --
        # if the Linde-owned-domain path fell through to a real liveness
        # check instead of short-circuiting, this would fail the import.
        stats = import_linde_dealer_network(
            repo, SupplierMatcher(repo), http_client=client, website_checker=lambda url: False,
        )

        assert stats.linde_owned_domain == 1
        assert stats.website_live == 0
        assert stats.website_dead == 0
        assert stats.static_import.created == 1

    def test_two_dealers_both_on_a_linde_owned_domain_do_not_collide(self, repo):
        """The actual bug this whole fix prevents: two UNRELATED
        dealers both listing linde-mh.com as their "website" must NOT
        auto-merge into one golden record via the domain-exact-match
        dedup tier."""
        dealer_a = _dealer(
            name="Dealer A", website="https://www.linde-mh.com/en/?utm_source=1",
            phone="+1 111 111 1111", city="Springfield", street="1 First St", zip="00001",
        )
        dealer_b = _dealer(
            name="Dealer B", website="https://www.linde-mh.com/en/?utm_source=2",
            phone="+61 2 9000 0000", city="Sydney", street="2 Second St", zip="99999", country="au",
        )
        client = FakeHttpxClient(responses={LINDE_DEALER_DATA_URL: FakeResponse({"dealers": [dealer_a, dealer_b]})})
        stats = import_linde_dealer_network(
            repo, SupplierMatcher(repo), http_client=client, website_checker=lambda url: False,
        )

        assert stats.linde_owned_domain == 2
        assert stats.static_import.created == 2  # two distinct suppliers, not merged into one

    def test_existing_supplier_merges_rather_than_duplicates(self, repo):
        """The whole point of reusing import_static_supplier_list: a
        dealer already on file (e.g. from a live search) merges into
        the SAME row instead of creating a second one."""
        matcher = SupplierMatcher(repo)
        matcher.resolve_and_store({"canonical_name": "A.G. Pruden & Cia. S.A.", "domain": "agpruden.com"})

        dealer = _dealer()
        client = FakeHttpxClient(responses={LINDE_DEALER_DATA_URL: FakeResponse({"dealers": [dealer]})})
        stats = import_linde_dealer_network(
            repo, matcher, http_client=client, website_checker=lambda url: True,
        )

        assert stats.static_import.merged == 1
        assert stats.static_import.created == 0
        from storage.database import connection_scope
        with connection_scope(repo.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) AS n FROM suppliers").fetchone()["n"]
        assert count == 1  # not duplicated
