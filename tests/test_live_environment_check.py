"""
tests/test_live_environment_check.py

Tests for diagnostics.live_environment_check. These test the check
functions' own logic (skip when unconfigured, parse a response into
pass/fail correctly) using fakes -- they deliberately do NOT prove any
real external service actually works, since that's the entire point
of this module: it exists to be run against real credentials in a
real deployed environment, which this test suite is not.
"""

from __future__ import annotations


from diagnostics.live_environment_check import (
    CheckResult,
    check_amap,
    check_apify,
    check_database,
    check_dns_resolution,
    check_google_places,
    check_openai,
    check_qichacha,
    check_serpapi,
    check_site_reachable,
    run_all_checks,
    source_base_url,
)


class FakeHttpResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self):
        return self._json


class FakeHttpClient:
    def __init__(self, response=None, raise_error=None):
        self._response = response or FakeHttpResponse()
        self._raise_error = raise_error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self._raise_error:
            raise self._raise_error
        return self._response


class TestCheckDatabase:

    def test_missing_database_fails(self, tmp_path, monkeypatch):
        fake_path = tmp_path / "does_not_exist.db"
        monkeypatch.setattr("config.settings.DB_PATH", fake_path)
        result = check_database()
        assert result.status == "fail"
        assert "no database" in result.detail

    def test_existing_correctly_migrated_database_passes(self, tmp_path, monkeypatch):
        from storage.database import initialise_schema

        db_path = tmp_path / "test.db"
        initialise_schema(db_path)
        monkeypatch.setattr("config.settings.DB_PATH", db_path)
        result = check_database()
        assert result.status == "pass"


class TestCheckDnsResolution:
    """Genuinely live -- this one has no meaningful fake, since its
    entire job is proving real DNS resolution works."""

    def test_real_dns_resolution_passes(self):
        result = check_dns_resolution()
        assert result.status == "pass"


class TestCheckApify:

    def test_no_token_is_skipped(self, monkeypatch):
        monkeypatch.setattr("config.settings.APIFY_TOKEN", None)
        result = check_apify()
        assert result.status == "skipped"

    def test_valid_token_passes(self, monkeypatch):
        monkeypatch.setattr("config.settings.APIFY_TOKEN", "fake-token")
        client = FakeHttpClient(FakeHttpResponse(200, {"data": {"username": "testuser"}}))
        result = check_apify(http_client=client)
        assert result.status == "pass"
        assert "testuser" in result.detail

    def test_invalid_token_fails(self, monkeypatch):
        monkeypatch.setattr("config.settings.APIFY_TOKEN", "bad-token")
        client = FakeHttpClient(FakeHttpResponse(401))
        result = check_apify(http_client=client)
        assert result.status == "fail"

    def test_connection_error_fails_not_raises(self, monkeypatch):
        monkeypatch.setattr("config.settings.APIFY_TOKEN", "fake-token")
        client = FakeHttpClient(raise_error=RuntimeError("network down"))
        result = check_apify(http_client=client)
        assert result.status == "fail"


class TestCheckSerpapi:

    def test_no_key_is_skipped(self, monkeypatch):
        monkeypatch.setattr("config.settings.SERPAPI_KEY", None)
        result = check_serpapi()
        assert result.status == "skipped"


class TestCheckOpenai:

    def test_no_key_is_skipped(self, monkeypatch):
        monkeypatch.setattr("config.settings.OPENAI_API_KEY", None)
        result = check_openai()
        assert result.status == "skipped"


class TestCheckGooglePlaces:

    def test_no_key_is_skipped(self, monkeypatch):
        monkeypatch.setattr("config.settings.GOOGLE_PLACES_API_KEY", None)
        result = check_google_places()
        assert result.status == "skipped"


class TestCheckAmap:

    def test_no_key_is_skipped(self, monkeypatch):
        monkeypatch.setattr("config.settings.AMAP_API_KEY", None)
        result = check_amap()
        assert result.status == "skipped"
        assert "registration friction" in result.detail


class TestCheckQichacha:

    def test_no_keys_is_skipped(self, monkeypatch):
        monkeypatch.setattr("config.settings.QICHACHA_API_KEY", None)
        monkeypatch.setattr("config.settings.QICHACHA_SECRET_KEY", None)
        result = check_qichacha()
        assert result.status == "skipped"


class TestCheckSiteReachable:

    def test_2xx_response_passes(self):
        client = FakeHttpClient(FakeHttpResponse(200))
        result = check_site_reachable("test_site", "https://example.com", http_client=client)
        assert result.status == "pass"

    def test_403_fails_with_a_helpful_reason(self):
        client = FakeHttpClient(FakeHttpResponse(403))
        result = check_site_reachable("test_site", "https://example.com", http_client=client)
        assert result.status == "fail"
        assert "blocking" in result.detail

    def test_connection_error_fails_not_raises(self):
        client = FakeHttpClient(raise_error=RuntimeError("dns failure"))
        result = check_site_reachable("test_site", "https://example.com", http_client=client)
        assert result.status == "fail"


class TestSourceBaseUrl:

    def test_extracts_scheme_and_host_only(self):
        assert source_base_url("https://www.volza.com/p/{query}/import/import-in-united-kingdom/?page={page}") == "https://www.volza.com"

    def test_works_with_query_string_before_any_placeholder(self):
        assert source_base_url("https://www.tim.org.tr/en/member-search?q={query}&page={page}") == "https://www.tim.org.tr"

    def test_works_with_subdomain(self):
        assert source_base_url("https://en.vcci.com.vn/directory/search?keyword={query}&page={page}") == "https://en.vcci.com.vn"

    def test_works_with_multi_segment_path_before_query(self):
        assert source_base_url(
            "https://automechanika-frankfurt.messefrankfurt.com/frankfurt/en/exhibitor-search.html?search={query}&page={page}"
        ) == "https://automechanika-frankfurt.messefrankfurt.com"


class TestCheckResultShape:

    def test_run_all_checks_populates_duration_on_every_result(self, monkeypatch):
        """Timing is the orchestration's job (_timed, inside
        run_all_checks), not each individual check function's --
        calling a check function directly (as the tests above do) is
        expected to leave duration_ms unset."""
        for key in ("APIFY_TOKEN", "SERPAPI_KEY", "OPENAI_API_KEY", "GOOGLE_PLACES_API_KEY",
                    "AMAP_API_KEY", "QICHACHA_API_KEY", "QICHACHA_SECRET_KEY"):
            monkeypatch.setattr(f"config.settings.{key}", None)
        results = run_all_checks()
        for r in results:
            assert isinstance(r, CheckResult)
            assert r.duration_ms is not None
            assert r.duration_ms >= 0


class TestRunAllChecks:

    def test_returns_a_result_for_every_check(self, monkeypatch):
        # Force every paid integration to skip cleanly so this test
        # doesn't depend on real credentials or network conditions
        # beyond DNS -- proving the orchestration wiring, not the
        # individual live behaviours (those are covered above).
        for key in ("APIFY_TOKEN", "SERPAPI_KEY", "OPENAI_API_KEY", "GOOGLE_PLACES_API_KEY",
                    "AMAP_API_KEY", "QICHACHA_API_KEY", "QICHACHA_SECRET_KEY"):
            monkeypatch.setattr(f"config.settings.{key}", None)

        results = run_all_checks()
        names = {r.name for r in results}
        assert "database" in names
        assert "dns_resolution" in names
        assert "apify" in names
        # Every no-key-required site-reachability check -- hktdc/importyeti
        # plus volza and every DIRECTORY_SOURCES/EXHIBITION_SOURCES entry --
        # must be present. Named explicitly (not just a count) so a source
        # silently dropping out of run_all_checks would fail this test even
        # if the total count happened to stay the same.
        for source in (
            "hktdc", "importyeti", "volza",
            "turkey_tim", "vietnam_vcci", "europages_eastern_europe",
            "ciape", "auto_shanghai", "automechanika_frankfurt",
        ):
            assert source in names, f"{source!r} missing from run_all_checks() output"
        assert len(results) >= 15

    def test_one_check_raising_does_not_abort_the_others(self, monkeypatch):
        """_timed's own fault isolation -- a bug inside one check must
        not prevent the rest of the diagnostic from running."""
        import diagnostics.live_environment_check as mod

        def exploding_check():
            raise RuntimeError("boom")

        monkeypatch.setattr(mod, "check_apify", exploding_check)
        results = run_all_checks()
        # The other checks should still be present and not blown away
        assert any(r.name == "database" for r in results)
