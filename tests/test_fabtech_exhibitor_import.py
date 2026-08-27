"""
tests/test_fabtech_exhibitor_import.py

Tests for discovery/fabtech_exhibitor_import.py -- list fetch + per-
exhibitor profile fetch + reachability-classify + import via the SAME
dedup engine every other source uses. No real network:
fetch_fabtech_exhibitor_list()/fetch_exhibitor_profile() both take an
injectable http_client (fake httpx.Client shape), and
import_fabtech_exhibitors() additionally accepts fake
reachability_classifier/profile_fetcher callables directly, so most
tests don't need to fake HTTP at all -- same convention as
tests/test_linde_dealer_import.py.

classify_website_reachability's own tests live in
tests/test_website_reachability.py now that the function has been
promoted to verification/website_reachability.py, a shared module --
see that file for the real Cloudflare-challenge response shape this
module's "blocked" classification is built against.

The HTML fixtures below mirror the REAL markup structure confirmed by
fetching FABTECH's actual live pages during this module's development
(`.listTableBody table tbody tr` / `a.exhibitorName` / `a.boothLabel`
with a `sortVal` attribute for the exhibitor list; `.text-secondary` +
`.profileResponse` label/value pairs for the profile page), not an
invented simplified shape.
"""

from __future__ import annotations

import pytest

from deduplication.matcher import SupplierMatcher
from discovery.fabtech_exhibitor_import import (
    FABTECH_EXHIBITOR_LIST_URL,
    FABTECH_PROFILE_URL_TEMPLATE,
    fetch_exhibitor_profile,
    fetch_fabtech_exhibitor_list,
    import_fabtech_exhibitors,
)
from storage.database import initialise_schema
from storage.repository import SupplierRepository


@pytest.fixture()
def repo(tmp_path):
    db_path = tmp_path / "test.db"
    initialise_schema(db_path)
    return SupplierRepository(db_path=db_path)


def _list_row_html(name: str, booth_id: str, booth_number: str = "A100") -> str:
    return f"""
    <tr>
      <td class="companyName"><a class="exhibitorName"
        href="openURL.aspx?BoothID={booth_id}&amp;HyperLinkURL=https://fabtech2026.smallworldlabs.com/?page_id=2424&amp;boothId={booth_id}&amp;EventID=128"
        target="_blank">{name}</a></td>
      <td class="boothLabel"><a class="boothLabel" sortVal="{booth_number}" boothid="{booth_id}"
        href="eventmap.aspx?MapID=1&amp;MapItBoothID={booth_id}">{booth_number}</a></td>
    </tr>
    """


def _list_html(rows: list) -> str:
    body = "\n".join(rows)
    return f"""
    <html><body>
      <div class="listTableBody">
        <table><tbody>{body}</tbody></table>
      </div>
    </body></html>
    """


def _profile_field_html(label: str, value_html: str) -> str:
    return f"""
    <div class="row no-gutters mb-3">
      <div class="col-4"><div class="text-secondary">{label}</div></div>
      <div class="col-8"><div class="profileResponse">{value_html}</div></div>
    </div>
    """


def _profile_html(name="", website="", address_lines=None, phone="", pavilion=""):
    parts = []
    if name:
        parts.append(_profile_field_html("Name", name))
    if website:
        parts.append(_profile_field_html("Website", f'<a href="{website}">{website}</a>'))
    if address_lines:
        parts.append(_profile_field_html("Address", "<br />".join(address_lines)))
    if phone:
        parts.append(_profile_field_html("Phone", phone))
    if pavilion:
        parts.append(_profile_field_html("Pavilion", pavilion))
    return f"<html><head><title>{name}</title></head><body>{''.join(parts)}</body></html>"


class FakeResponse:
    def __init__(self, text="", status_code=200, headers=None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttpxClient:
    """`responses` maps url -> FakeResponse; a url with no entry
    defaults to a 404, matching FakeHttpxClient's own convention in
    tests/test_linde_dealer_import.py."""

    def __init__(self, responses=None, raise_for_url=None):
        self._responses = responses or {}
        self._raise_for_url = raise_for_url
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        if self._raise_for_url and url == self._raise_for_url:
            raise RuntimeError("connection failed")
        return self._responses.get(url, FakeResponse("", status_code=404))


class TestFetchFabtechExhibitorList:

    def test_parses_real_shaped_rows(self):
        html = _list_html([
            _list_row_html("Mazak Optonics Corporation", "517546", "C3025"),
            _list_row_html("Intermark Steel", "517394", "A3345"),
        ])
        client = FakeHttpxClient(responses={FABTECH_EXHIBITOR_LIST_URL: FakeResponse(html)})
        exhibitors, excluded = fetch_fabtech_exhibitor_list(http_client=client)

        assert excluded == 0
        assert len(exhibitors) == 2
        assert exhibitors[0] == {"name": "Mazak Optonics Corporation", "booth_id": "517546", "booth_number": "C3025"}
        assert exhibitors[1]["name"] == "Intermark Steel"
        assert exhibitors[1]["booth_id"] == "517394"

    def test_test_qa_seed_accounts_are_excluded(self):
        """Real finding: 'CDS a2z SSO Multiuser Company 1'/'2' are
        A2Z's own platform QA accounts -- confirmed via their real
        profile page showing '123 Test Street, Test, KS 12345'."""
        html = _list_html([
            _list_row_html("CDS a2z SSO Multiuser Company 1", "533194"),
            _list_row_html("CDS a2z SSO Multiuser Company 2", "533193"),
            _list_row_html("Real Company Inc", "500001"),
        ])
        client = FakeHttpxClient(responses={FABTECH_EXHIBITOR_LIST_URL: FakeResponse(html)})
        exhibitors, excluded = fetch_fabtech_exhibitor_list(http_client=client)

        assert excluded == 2
        assert len(exhibitors) == 1
        assert exhibitors[0]["name"] == "Real Company Inc"

    def test_http_error_raises(self):
        client = FakeHttpxClient(responses={FABTECH_EXHIBITOR_LIST_URL: FakeResponse("", status_code=500)})
        with pytest.raises(RuntimeError):
            fetch_fabtech_exhibitor_list(http_client=client)

    def test_empty_table_returns_empty_list(self):
        client = FakeHttpxClient(responses={FABTECH_EXHIBITOR_LIST_URL: FakeResponse(_list_html([]))})
        exhibitors, excluded = fetch_fabtech_exhibitor_list(http_client=client)
        assert exhibitors == []
        assert excluded == 0


class TestFetchExhibitorProfile:

    def test_extracts_website_from_real_anchor_href(self):
        html = _profile_html(name="Intermark Steel", website="http://intermarksteel.com")
        client = FakeHttpxClient(responses={
            FABTECH_PROFILE_URL_TEMPLATE.format(booth_id="517394"): FakeResponse(html),
        })
        fields = fetch_exhibitor_profile("517394", http_client=client)
        assert fields["Website"] == "http://intermarksteel.com"
        assert fields["Name"] == "Intermark Steel"

    def test_extracts_multiline_address(self):
        html = _profile_html(
            name="Intermark Steel",
            address_lines=["650 S 500 W Ste 101", "Salt Lake City, UT 84101-2378", "United States"],
        )
        client = FakeHttpxClient(responses={
            FABTECH_PROFILE_URL_TEMPLATE.format(booth_id="517394"): FakeResponse(html),
        })
        fields = fetch_exhibitor_profile("517394", http_client=client)
        assert fields["Address"] == "650 S 500 W Ste 101\nSalt Lake City, UT 84101-2378\nUnited States"

    def test_extracts_phone(self):
        html = _profile_html(name="Intermark Steel", phone="435-637-4435")
        client = FakeHttpxClient(responses={
            FABTECH_PROFILE_URL_TEMPLATE.format(booth_id="517394"): FakeResponse(html),
        })
        fields = fetch_exhibitor_profile("517394", http_client=client)
        assert fields["Phone"] == "435-637-4435"

    def test_404_returns_empty_dict(self):
        client = FakeHttpxClient(responses={
            FABTECH_PROFILE_URL_TEMPLATE.format(booth_id="999999"): FakeResponse("", status_code=404),
        })
        assert fetch_exhibitor_profile("999999", http_client=client) == {}

    def test_connection_failure_returns_empty_dict_not_raised(self):
        url = FABTECH_PROFILE_URL_TEMPLATE.format(booth_id="517394")
        client = FakeHttpxClient(raise_for_url=url)
        assert fetch_exhibitor_profile("517394", http_client=client) == {}

    def test_no_profile_fields_returns_empty_dict(self):
        client = FakeHttpxClient(responses={
            FABTECH_PROFILE_URL_TEMPLATE.format(booth_id="517394"): FakeResponse("<html><body>Page not found</body></html>"),
        })
        assert fetch_exhibitor_profile("517394", http_client=client) == {}


class TestImportFabtechExhibitors:

    def _import(self, repo, exhibitors, profiles, live_domains=frozenset(), blocked_domains=frozenset()):
        """`exhibitors` is a list of (name, booth_id, booth_number) tuples;
        `profiles` maps booth_id -> profile field dict (as fetch_exhibitor_
        profile would return)."""
        list_html = _list_html([_list_row_html(n, b, num) for n, b, num in exhibitors])
        client = FakeHttpxClient(responses={FABTECH_EXHIBITOR_LIST_URL: FakeResponse(list_html)})
        profile_fetcher = lambda booth_id: profiles.get(booth_id, {})

        def classify(url):
            if url in live_domains:
                return "live"
            if url in blocked_domains:
                return "blocked"
            return "dead"

        return import_fabtech_exhibitors(
            repo, SupplierMatcher(repo), http_client=client,
            reachability_classifier=classify, profile_fetcher=profile_fetcher,
        )

    def test_live_exhibitor_creates_a_new_supplier(self, repo):
        stats = self._import(
            repo,
            [("Intermark Steel", "517394", "A3345")],
            {"517394": {"Website": "http://intermarksteel.com", "Phone": "435-637-4435"}},
            live_domains={"http://intermarksteel.com"},
        )

        assert stats.website_live == 1
        assert stats.website_dead == 0
        assert stats.static_import.created == 1
        supplier = repo.find_by_domain("intermarksteel.com")
        assert supplier["canonical_name"] == "Intermark Steel"
        assert supplier["discovery_source"] == "trade-show-exhibitor-fabtech"

    def test_dead_website_exhibitor_is_skipped_not_imported(self, repo):
        stats = self._import(
            repo,
            [("Intermark Steel", "517394", "A3345")],
            {"517394": {"Website": "http://intermarksteel.com"}},
        )

        assert stats.website_dead == 1
        assert stats.static_import.total == 0
        assert repo.find_by_domain("intermarksteel.com") is None

    def test_dead_website_exhibitor_is_still_recorded_in_raw_source_data(self, repo):
        self._import(
            repo,
            [("Intermark Steel", "517394", "A3345")],
            {"517394": {"Website": "http://intermarksteel.com"}},
        )

        from storage.database import connection_scope
        with connection_scope(repo.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM raw_source_data WHERE source = 'trade-show-exhibitor-fabtech'"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["processing_status"] == "failed"
        assert "did not resolve" in rows[0]["error_message"]

    def test_exhibitor_with_no_website_field_is_imported_anyway(self, repo):
        stats = self._import(
            repo,
            [("No Website Co", "500001", "B1")],
            {"500001": {}},  # profile page found no Website field
        )

        assert stats.no_website == 1
        assert stats.website_live == 0
        assert stats.website_dead == 0
        assert stats.static_import.created == 1
        from storage.database import connection_scope
        with connection_scope(repo.db_path) as conn:
            row = conn.execute(
                "SELECT canonical_name, domain FROM suppliers WHERE canonical_name = 'No Website Co'"
            ).fetchone()
        assert row is not None
        assert row["domain"] is None

    def test_blocked_exhibitor_is_still_imported_with_domain(self, repo):
        """The user's explicit decision: a bot-challenge response must
        NOT be treated like a dead domain -- import it with the
        self-reported domain populated, not silently dropped."""
        stats = self._import(
            repo,
            [("8020 Inc", "600001", "C1")],
            {"600001": {"Website": "https://8020.net/"}},
            blocked_domains={"https://8020.net/"},
        )

        assert stats.website_blocked == 1
        assert stats.website_live == 0
        assert stats.website_dead == 0
        assert stats.static_import.created == 1
        supplier = repo.find_by_domain("8020.net")
        assert supplier is not None
        assert supplier["canonical_name"] == "8020 Inc"

    def test_blocked_exhibitor_gets_a_field_provenance_entry_marking_it_unconfirmed(self, repo):
        stats = self._import(
            repo,
            [("8020 Inc", "600001", "C1")],
            {"600001": {"Website": "https://8020.net/"}},
            blocked_domains={"https://8020.net/"},
        )
        supplier = repo.find_by_domain("8020.net")
        provenance = repo.get_field_provenance(supplier["id"], field_name="domain")

        assert len(provenance) == 1
        assert provenance[0]["source_tier"] == "other"
        assert provenance[0]["claim_type"] == "verifiable_fact"
        assert provenance[0]["extraction_method"] == "fabtech_exhibitor_profile_self_reported_bot_blocked"
        assert provenance[0]["value"] == "8020.net"

    def test_blocked_exhibitor_merges_into_existing_supplier_like_a_live_one(self, repo):
        matcher = SupplierMatcher(repo)
        matcher.resolve_and_store({"canonical_name": "8020 Inc", "domain": "8020.net"})

        stats = self._import(
            repo,
            [("8020 Inc", "600001", "C1")],
            {"600001": {"Website": "https://8020.net/"}},
            blocked_domains={"https://8020.net/"},
        )

        assert stats.static_import.merged == 1
        assert stats.static_import.created == 0
        from storage.database import connection_scope
        with connection_scope(repo.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) AS n FROM suppliers").fetchone()["n"]
        assert count == 1

    def test_test_qa_accounts_are_excluded_before_any_profile_fetch(self, repo):
        """The 2 real known A2Z QA seed accounts must never trigger a
        profile fetch at all -- confirmed via a profile_fetcher that
        raises if ever called for that booth id."""
        def profile_fetcher(booth_id):
            if booth_id == "533194":
                raise AssertionError("must not fetch profile for a test/QA account")
            return {"Website": "http://realcompany.example"}

        list_html = _list_html([
            _list_row_html("CDS a2z SSO Multiuser Company 1", "533194"),
            _list_row_html("Real Company Inc", "500001"),
        ])
        client = FakeHttpxClient(responses={FABTECH_EXHIBITOR_LIST_URL: FakeResponse(list_html)})
        stats = import_fabtech_exhibitors(
            repo, SupplierMatcher(repo), http_client=client,
            reachability_classifier=lambda url: "live", profile_fetcher=profile_fetcher,
        )

        assert stats.test_accounts_excluded == 1
        assert stats.total_exhibitors == 1
        assert stats.static_import.created == 1

    def test_limit_caps_how_many_exhibitors_are_processed(self, repo):
        exhibitors = [(f"Company {i}", f"50000{i}", f"B{i}") for i in range(5)]
        profiles = {f"50000{i}": {"Website": f"https://company{i}.example"} for i in range(5)}
        list_html = _list_html([_list_row_html(n, b, num) for n, b, num in exhibitors])
        client = FakeHttpxClient(responses={FABTECH_EXHIBITOR_LIST_URL: FakeResponse(list_html)})
        stats = import_fabtech_exhibitors(
            repo, SupplierMatcher(repo), limit=2, http_client=client,
            reachability_classifier=lambda url: "live", profile_fetcher=lambda bid: profiles.get(bid, {}),
        )
        assert stats.total_exhibitors == 2

    def test_existing_supplier_merges_rather_than_duplicates(self, repo):
        matcher = SupplierMatcher(repo)
        matcher.resolve_and_store({"canonical_name": "Intermark Steel", "domain": "intermarksteel.com"})

        list_html = _list_html([_list_row_html("Intermark Steel", "517394", "A3345")])
        client = FakeHttpxClient(responses={FABTECH_EXHIBITOR_LIST_URL: FakeResponse(list_html)})
        stats = import_fabtech_exhibitors(
            repo, matcher, http_client=client,
            reachability_classifier=lambda url: "live",
            profile_fetcher=lambda bid: {"Website": "http://intermarksteel.com"},
        )

        assert stats.static_import.merged == 1
        assert stats.static_import.created == 0
        from storage.database import connection_scope
        with connection_scope(repo.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) AS n FROM suppliers").fetchone()["n"]
        assert count == 1  # not duplicated

    def test_two_exhibitors_with_different_real_domains_do_not_collide(self, repo):
        """The collision-risk check the user explicitly asked for: two
        unrelated real exhibitors must never accidentally resolve to
        the same stored domain."""
        list_html = _list_html([
            _list_row_html("Intermark Steel", "517394", "A3345"),
            _list_row_html("FARO CREAFORM", "517406", "B100"),
        ])
        client = FakeHttpxClient(responses={FABTECH_EXHIBITOR_LIST_URL: FakeResponse(list_html)})
        profiles = {
            "517394": {"Website": "http://intermarksteel.com"},
            "517406": {"Website": "http://www.creaform3d.com"},
        }
        stats = import_fabtech_exhibitors(
            repo, SupplierMatcher(repo), http_client=client,
            reachability_classifier=lambda url: "live", profile_fetcher=lambda bid: profiles.get(bid, {}),
        )
        assert stats.static_import.created == 2
        assert repo.find_by_domain("intermarksteel.com") is not None
        assert repo.find_by_domain("creaform3d.com") is not None
