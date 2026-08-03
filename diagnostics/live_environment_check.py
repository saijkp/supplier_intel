"""
diagnostics/live_environment_check.py

Answers the question `doctor` alone can't: not just "is an API key
present," but "does it actually work, right now, in this deployed
environment." Every check here makes the smallest real call that can
distinguish "this integration works" from "this integration is
configured but broken" -- reusing the actual production classes
(`QichachaVerifier`, `GoogleSearchScraper`, `GooglePlacesAddressVerifier`,
`AmapAddressVerifier`) rather than a parallel hand-rolled HTTP check,
so a pass here means the real code path works, not just that some
similar-looking request succeeded.

Why this exists
------------------
A large fraction of what's been built against this codebase's external
integrations (Google Places, Amap, the OpenAI-based verifiers) has
been built against documented API contracts and tested against fake
clients -- explicitly and repeatedly disclosed as NOT exercised
against a live key, because no key was available in the environment
building it. That disclosure is only useful once someone can actually
close the loop and find out which of those integrations work for
real. This script is that closing step, meant to be run once real
credentials exist -- e.g. right after a Railway deployment, or any
time credentials rotate.

Cost and side effects
--------------------------
Every check is designed to be the cheapest possible real call:
- Apify: an account-info lookup, no actor run.
- SerpAPI/Google Places/Amap: a single minimal query each.
- OpenAI: a tiny completion (a handful of tokens).
- Qichacha: one lookup against a syntactically-valid, essentially
  guaranteed-nonexistent test USCC -- proves the signing/auth path
  works via a structured "not found" response, without needing a real
  company's data.
- HKTDC/ImportYeti/Volza: a plain reachability check (can the site be
  reached at all), not a scrape.

Nothing here writes to your real database. Every check is read-only
against the external service and stateless against this codebase.

Never run automatically against a real API key without deliberately
choosing to -- these do spend a small amount of real quota/money each
run.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Callable, List, Optional


@dataclasses.dataclass
class CheckResult:
    name: str
    status: str  # 'pass' | 'fail' | 'skipped'
    detail: str
    duration_ms: Optional[int] = None


def _timed(name: str, fn: Callable[[], CheckResult]) -> CheckResult:
    """Wraps a check so a bug in the check itself (not the external
    service) becomes a clearly-labelled failure rather than crashing
    the whole diagnostic run -- the same fault-isolation discipline
    every pipeline stage in this codebase already follows."""
    start = time.monotonic()
    try:
        result = fn()
    except Exception as e:
        result = CheckResult(name=name, status="fail", detail=f"diagnostic check itself raised: {e}")
    result.duration_ms = int((time.monotonic() - start) * 1000)
    return result


def check_database() -> CheckResult:
    from config.settings import DB_PATH
    from storage.database import SCHEMA_VERSION, get_schema_version

    if not DB_PATH.exists():
        return CheckResult("database", "fail", f"no database at {DB_PATH} -- run `python main.py init-db`")
    version = get_schema_version(DB_PATH)
    if version != SCHEMA_VERSION:
        return CheckResult(
            "database", "fail",
            f"schema at v{version}, code expects v{SCHEMA_VERSION} -- run `python main.py init-db` to migrate",
        )
    return CheckResult("database", "pass", f"reachable, schema v{version}")


def check_dns_resolution() -> CheckResult:
    """Free, no key needed -- proves outbound DNS works at all, which
    verification.email_deliverability depends on entirely."""
    import dns.resolver

    try:
        dns.resolver.resolve("gmail.com", "MX")
        return CheckResult("dns_resolution", "pass", "MX lookup against a known-good domain succeeded")
    except Exception as e:
        return CheckResult("dns_resolution", "fail", f"DNS resolution is not working in this environment: {e}")


def check_apify(http_client: Optional[object] = None) -> CheckResult:
    from config.settings import APIFY_TOKEN

    if not APIFY_TOKEN:
        return CheckResult("apify", "skipped", "APIFY_TOKEN not configured")
    try:
        import httpx

        client = http_client or httpx
        response = client.get("https://api.apify.com/v2/users/me", params={"token": APIFY_TOKEN}, timeout=15)
        if response.status_code == 200:
            username = response.json().get("data", {}).get("username", "unknown")
            return CheckResult("apify", "pass", f"token valid, account: {username}")
        return CheckResult("apify", "fail", f"HTTP {response.status_code} -- token likely invalid or revoked")
    except Exception as e:
        return CheckResult("apify", "fail", f"request failed: {e}")


def check_serpapi() -> CheckResult:
    from config.settings import SERPAPI_KEY
    from scrapers.google_search_scraper import GoogleSearchScraper

    if not SERPAPI_KEY:
        return CheckResult("serpapi", "skipped", "SERPAPI_KEY not configured")
    try:
        scraper = GoogleSearchScraper(api_key=SERPAPI_KEY)
        results = scraper.scrape("test query", max_results=1)
        if results and results[0].success:
            return CheckResult("serpapi", "pass", "search returned a real result")
        error = results[0].error if results else "no results returned at all"
        return CheckResult("serpapi", "fail", f"search did not succeed: {error}")
    except Exception as e:
        return CheckResult("serpapi", "fail", f"request failed: {e}")


def check_openai() -> CheckResult:
    from config.settings import OPENAI_API_KEY

    if not OPENAI_API_KEY:
        return CheckResult("openai", "skipped", "OPENAI_API_KEY not configured")
    try:
        from openai import OpenAI

        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=5,
            messages=[{"role": "user", "content": "Reply with just: ok"}],
        )
        text = (response.choices[0].message.content or "").strip()
        return CheckResult("openai", "pass", f"completion succeeded, model responded: {text!r}")
    except Exception as e:
        return CheckResult("openai", "fail", f"request failed: {e}")


def check_google_places() -> CheckResult:
    from config.settings import GOOGLE_PLACES_API_KEY
    from verification.facility_address_verifier import GooglePlacesAddressVerifier

    if not GOOGLE_PLACES_API_KEY:
        return CheckResult("google_places", "skipped", "GOOGLE_PLACES_API_KEY not configured")
    try:
        verifier = GooglePlacesAddressVerifier(api_key=GOOGLE_PLACES_API_KEY)
        result = verifier.verify("Buckingham Palace, London")
        if result.source == "unavailable":
            return CheckResult("google_places", "fail", result.reason)
        if result.verified:
            return CheckResult("google_places", "pass", f"resolved: {result.formatted_address}")
        return CheckResult(
            "google_places", "fail",
            f"a well-known landmark did not resolve ({result.reason}) -- check API key permissions/billing",
        )
    except Exception as e:
        return CheckResult("google_places", "fail", f"request failed: {e}")


def check_amap() -> CheckResult:
    from config.settings import AMAP_API_KEY
    from verification.facility_address_verifier import AmapAddressVerifier

    if not AMAP_API_KEY:
        return CheckResult(
            "amap", "skipped",
            "AMAP_API_KEY not configured (see facility_address_verifier's own module docstring "
            "for the real registration friction to expect here as a non-Chinese developer)",
        )
    try:
        verifier = AmapAddressVerifier(api_key=AMAP_API_KEY)
        result = verifier.verify("北京市东城区天安门")
        if result.source == "unavailable":
            return CheckResult("amap", "fail", result.reason)
        if result.verified:
            return CheckResult("amap", "pass", f"resolved: {result.formatted_address}")
        return CheckResult(
            "amap", "fail", f"a well-known landmark did not resolve ({result.reason})",
        )
    except Exception as e:
        return CheckResult("amap", "fail", f"request failed: {e}")


def check_qichacha() -> CheckResult:
    from config.settings import QICHACHA_API_KEY, QICHACHA_SECRET_KEY
    from verification.qichacha import QichachaVerifier

    if not QICHACHA_API_KEY or not QICHACHA_SECRET_KEY:
        return CheckResult("qichacha", "skipped", "QICHACHA_API_KEY/QICHACHA_SECRET_KEY not configured")
    try:
        verifier = QichachaVerifier(app_key=QICHACHA_API_KEY, app_secret=QICHACHA_SECRET_KEY)
        # A syntactically valid but essentially guaranteed-nonexistent
        # USCC -- proves the signing scheme and auth work via a
        # structured API response, without needing real company data.
        result = verifier.verify("91440101MA5ABCDE1M")
        if "error" in result and result["error"] not in ("not_found", None):
            return CheckResult("qichacha", "fail", f"API returned an error: {result['error']}")
        return CheckResult("qichacha", "pass", "signing scheme accepted, API responded with a structured result")
    except Exception as e:
        return CheckResult("qichacha", "fail", f"request failed: {e}")


def check_webshare_proxy() -> CheckResult:
    """Confirms Webshare credentials actually route real traffic, not
    just that they're present -- makes one minimal request through the
    proxy to an IP-echo endpoint and confirms it succeeds. See
    collection/proxy_provider.py for why Webshare specifically (the
    only rotating-proxy provider actually implemented so far)."""
    from collection.proxy_provider import WebshareProxyProvider

    provider = WebshareProxyProvider()
    if not provider.is_configured():
        return CheckResult(
            "webshare_proxy", "skipped",
            "WEBSHARE_PROXY_USERNAME/WEBSHARE_PROXY_PASSWORD not configured",
        )
    try:
        import httpx

        config = provider.get_proxy_config()
        proxy_url = f"http://{config['username']}:{config['password']}@{config['server'].removeprefix('http://')}"
        with httpx.Client(proxy=proxy_url, timeout=15) as client:
            response = client.get("https://httpbin.org/ip")
        if response.status_code == 200:
            return CheckResult("webshare_proxy", "pass", f"proxied request succeeded: {response.text.strip()}")
        return CheckResult("webshare_proxy", "fail", f"proxied request returned HTTP {response.status_code}")
    except Exception as e:
        return CheckResult("webshare_proxy", "fail", f"request through proxy failed: {e}")


def check_playwright_chromium() -> CheckResult:
    """Confirms the actual Chromium browser binary collection.SiteCollector
    needs is installed and launchable -- NOT the same question as "is
    the playwright Python package installed." On Railway specifically,
    this is the check that would have caught the real deploy issue
    found while building Collection Service: RAILPACK_PYTHON_PLAYWRIGHT_INSTALL=1
    must be set or the browser binary is silently absent even though
    `pip install` succeeds cleanly (see DEPLOY.md)."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content("<html><body>ok</body></html>")
            text = page.inner_text("body")
            browser.close()
        if text.strip() == "ok":
            return CheckResult("playwright_chromium", "pass", "Chromium launched and rendered a page successfully")
        return CheckResult("playwright_chromium", "fail", f"unexpected page content: {text!r}")
    except Exception as e:
        return CheckResult(
            "playwright_chromium", "fail",
            f"Chromium launch failed: {e} -- on Railway, confirm RAILPACK_PYTHON_PLAYWRIGHT_INSTALL=1 is set "
            f"(see DEPLOY.md); a bare 'pip install playwright' does not download the browser binary",
        )


def check_site_reachable(name: str, url: str, http_client: Optional[object] = None) -> CheckResult:
    """Plain reachability, not a scrape -- for the direct-HTTP
    scrapers (HKTDC, ImportYeti, Volza) that need no API key at all,
    just confirms the target isn't blocking this environment's IP or
    otherwise unreachable."""
    try:
        import httpx

        client = http_client or httpx
        response = client.get(url, timeout=15, follow_redirects=True)
        if response.status_code < 400:
            return CheckResult(name, "pass", f"HTTP {response.status_code}")
        return CheckResult(
            name, "fail", f"HTTP {response.status_code} -- may be blocking this environment's IP",
        )
    except Exception as e:
        return CheckResult(name, "fail", f"unreachable: {e}")


def source_base_url(search_url_template: str) -> str:
    """scheme://host extracted from a scraper's own search_url_template
    (`{query}`/`{page}` placeholders and all) -- reachability only
    needs the bare host, and deriving it from the same config dict the
    real scraper uses means there's exactly one place these URLs are
    ever written down, never a second copy here that could drift."""
    import urllib.parse

    parsed = urllib.parse.urlsplit(search_url_template)
    return f"{parsed.scheme}://{parsed.netloc}"


def run_all_checks() -> List[CheckResult]:
    """Every check, run in sequence, each fault-isolated from the
    others. Order is cheapest/most-fundamental first, so a database or
    DNS failure is visible immediately rather than buried after a
    dozen paid API calls.

    The site-reachability checks (hktdc, importyeti, volza, and every
    DIRECTORY_SOURCES/EXHIBITION_SOURCES entry) need no API key at all
    -- they exist to answer a different question than every check
    above them: not "is a credential configured," but "is this
    environment's IP even able to reach the site." Several of these
    are known, from README's own disclosed limitations, to actively
    block datacenter/cloud IPs -- this is how that gets confirmed for
    a specific deployment rather than assumed from the README alone.
    """
    from scrapers.global_directory_scraper import DIRECTORY_SOURCES
    from scrapers.global_trade_scraper import TRADE_PROVIDERS
    from scrapers.shanghai_expo_scraper import EXHIBITION_SOURCES

    checks = [
        ("database", check_database),
        ("dns_resolution", check_dns_resolution),
        ("apify", check_apify),
        ("serpapi", check_serpapi),
        ("openai", check_openai),
        ("google_places", check_google_places),
        ("amap", check_amap),
        ("qichacha", check_qichacha),
        ("webshare_proxy", check_webshare_proxy),
        ("playwright_chromium", check_playwright_chromium),
        ("hktdc", lambda: check_site_reachable("hktdc", "https://www.hktdc.com")),
        ("importyeti", lambda: check_site_reachable("importyeti", "https://www.importyeti.com")),
    ]
    for name, config in {
        "volza": TRADE_PROVIDERS["volza"],
        **DIRECTORY_SOURCES,
        **EXHIBITION_SOURCES,
    }.items():
        url = source_base_url(config["search_url_template"])
        checks.append((name, lambda name=name, url=url: check_site_reachable(name, url)))

    return [_timed(name, fn) for name, fn in checks]
