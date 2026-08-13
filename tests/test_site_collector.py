"""
tests/test_site_collector.py

Tests for collection/site_collector.py against a real local static-HTML
server (Python's own http.server, bound to 127.0.0.1 on a random free
port) and a real Playwright-launched headless Chromium -- no live
proxy/target needed, matching the redesign plan's own stated approach
for this file. This exercises the actual extraction logic (link
following, contact-form detection, social/download link extraction,
HTML+screenshot artifact saving) end-to-end, not mocked.
"""

from __future__ import annotations

import http.server
import threading
from pathlib import Path

import pytest

from collection.artifact_store import ArtifactStore
from collection.site_collector import (
    SiteCollector,
    _build_candidate_urls,
    _find_certificate_candidates,
    _find_download_links,
    _find_relevant_links,
    _find_social_links,
    _has_contact_form,
)

_SITE_FILES = {
    "index.html": """
        <html><head><title>Acme Trailer Co</title></head>
        <body>
            <h1>Acme Trailer Co</h1>
            <p>We manufacture trailer axles and chassis.</p>
            <nav>
                <a href="/about.html">About Us</a>
                <a href="/contact.html">Contact</a>
                <a href="/products.html">Our Products</a>
                <a href="/catalogue.pdf">Download Catalogue (PDF)</a>
                <a href="/iso-9001-certificate.pdf">ISO 9001 Certificate</a>
                <a href="/rohs-certificate-missing.pdf">RoHS Certificate (broken link)</a>
                <a href="https://linkedin.com/company/acme-trailer">LinkedIn</a>
                <a href="https://facebook.com/acmetrailer">Facebook</a>
            </nav>
            <img src="/images/factory-floor.jpg" alt="factory">
            <img src="/images/logo.png" alt="logo">
        </body></html>
    """,
    "about.html": """
        <html><body>
            <h1>About Acme</h1>
            <p>Founded in 1998, ISO 9001 certified manufacturer.</p>
        </body></html>
    """,
    "contact.html": """
        <html><body>
            <h1>Contact Us</h1>
            <form>
                <input type="text" name="name" placeholder="Your name">
                <input type="email" name="email" placeholder="Your email">
                <textarea name="message" placeholder="Message"></textarea>
                <button type="submit">Send</button>
            </form>
        </body></html>
    """,
    "products.html": """
        <html><body>
            <h1>Products</h1>
            <p>Trailer axles, brake drums, and coupling heads.</p>
        </body></html>
    """,
    "catalogue.pdf": "%PDF-1.4 fake pdf content for testing",
    "iso-9001-certificate.pdf": "%PDF-1.4 fake ISO 9001 certificate content for testing",
}


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 -- matches stdlib signature
        pass  # silence per-request logging, keeps test output readable


@pytest.fixture()
def local_site(tmp_path):
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    for name, content in _SITE_FILES.items():
        (site_dir / name).write_text(content, encoding="utf-8")
    (site_dir / "images").mkdir()

    handler = lambda *args, **kwargs: _QuietHandler(*args, directory=str(site_dir), **kwargs)
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture()
def artifact_store(tmp_path):
    return ArtifactStore(base_dir=tmp_path / "artifacts")


class TestSiteCollectorRealBrowser:
    """Real Playwright, real (local) HTTP server -- no mocking of the
    extraction pipeline itself."""

    def test_collects_homepage_and_follows_relevant_links(self, local_site, artifact_store):
        collector = SiteCollector(artifact_store=artifact_store, max_pages=6)
        result = collector.collect(supplier_id=1, domain=local_site)

        assert result.success is True
        urls = {p.url for p in result.pages}
        assert f"{local_site}/" in urls or local_site in urls
        assert any("about.html" in u for u in urls)
        assert any("contact.html" in u for u in urls)
        assert any("products.html" in u for u in urls)
        assert not any("catalogue.pdf" in u for u in urls)  # download, not a page to visit

    def test_homepage_text_is_extracted(self, local_site, artifact_store):
        collector = SiteCollector(artifact_store=artifact_store)
        result = collector.collect(supplier_id=1, domain=local_site)
        homepage = result.pages[0]
        assert "Acme Trailer Co" in homepage.text
        assert "manufacture trailer axles" in homepage.text

    def test_contact_form_detected_on_contact_page(self, local_site, artifact_store):
        collector = SiteCollector(artifact_store=artifact_store)
        result = collector.collect(supplier_id=1, domain=local_site)
        contact_page = next(p for p in result.pages if "contact.html" in p.url)
        assert contact_page.has_contact_form is True

    def test_homepage_has_no_contact_form(self, local_site, artifact_store):
        collector = SiteCollector(artifact_store=artifact_store)
        result = collector.collect(supplier_id=1, domain=local_site)
        assert result.pages[0].has_contact_form is False

    def test_social_links_extracted_from_homepage(self, local_site, artifact_store):
        collector = SiteCollector(artifact_store=artifact_store)
        result = collector.collect(supplier_id=1, domain=local_site)
        homepage = result.pages[0]
        assert any("linkedin.com" in link for link in homepage.social_links)
        assert any("facebook.com" in link for link in homepage.social_links)

    def test_download_links_extracted_from_homepage(self, local_site, artifact_store):
        collector = SiteCollector(artifact_store=artifact_store)
        result = collector.collect(supplier_id=1, domain=local_site)
        homepage = result.pages[0]
        assert any("catalogue.pdf" in link for link in homepage.download_links)

    def test_facility_image_kept_logo_filtered(self, local_site, artifact_store):
        collector = SiteCollector(artifact_store=artifact_store)
        result = collector.collect(supplier_id=1, domain=local_site)
        homepage = result.pages[0]
        assert any("factory-floor.jpg" in u for u in homepage.image_urls)
        assert not any("logo.png" in u for u in homepage.image_urls)

    def test_html_and_screenshot_artifacts_are_saved_to_disk(self, local_site, artifact_store):
        collector = SiteCollector(artifact_store=artifact_store)
        result = collector.collect(supplier_id=7, domain=local_site)

        run_dir = artifact_store.base_dir / "7" / Path(result.artifacts_dir).name
        homepage = result.pages[0]
        assert (run_dir / homepage.html_path).exists()
        assert "Acme Trailer Co" in (run_dir / homepage.html_path).read_text(encoding="utf-8")
        assert homepage.screenshot_path is not None
        assert (run_dir / homepage.screenshot_path).exists()
        assert (run_dir / homepage.screenshot_path).stat().st_size > 0

    def test_max_pages_caps_total_pages_visited(self, local_site, artifact_store):
        collector = SiteCollector(artifact_store=artifact_store, max_pages=2)
        result = collector.collect(supplier_id=1, domain=local_site)
        assert len(result.pages) <= 2

    def test_unreachable_domain_returns_failure_not_raise(self, artifact_store):
        collector = SiteCollector(artifact_store=artifact_store, page_timeout_ms=3000)
        result = collector.collect(supplier_id=1, domain="http://127.0.0.1:1")  # nothing listening
        assert result.success is False
        assert result.error is not None

    def test_empty_domain_fails_without_launching_a_browser(self, artifact_store):
        collector = SiteCollector(artifact_store=artifact_store)
        result = collector.collect(supplier_id=1, domain="")
        assert result.success is False
        assert result.error == "no domain provided"

    def test_result_records_proxy_provider_name(self, local_site, artifact_store):
        collector = SiteCollector(artifact_store=artifact_store)
        result = collector.collect(supplier_id=1, domain=local_site)
        assert result.proxy_provider == "NoProxyProvider"


class TestCertificateDownload:
    """Real Playwright APIRequestContext against the same local HTTP
    server -- certificate-keyword-matching download_links actually get
    fetched and saved, not just linked."""

    def test_certificate_like_pdf_is_downloaded_and_saved(self, local_site, artifact_store):
        collector = SiteCollector(artifact_store=artifact_store)
        result = collector.collect(supplier_id=1, domain=local_site)

        assert len(result.certificate_documents) == 1
        doc = result.certificate_documents[0]
        assert "iso-9001-certificate.pdf" in doc.url
        assert doc.matched_keyword == "iso"

        run_dir = artifact_store.base_dir / "1" / Path(result.artifacts_dir).name
        saved_path = run_dir / doc.artifact_path
        assert saved_path.exists()
        assert b"fake ISO 9001 certificate content" in saved_path.read_bytes()

    def test_non_certificate_pdf_is_not_downloaded_as_a_certificate(self, local_site, artifact_store):
        collector = SiteCollector(artifact_store=artifact_store)
        result = collector.collect(supplier_id=1, domain=local_site)

        assert not any("catalogue.pdf" in doc.url for doc in result.certificate_documents)

    def test_max_certificate_downloads_caps_the_count(self, local_site, artifact_store, monkeypatch):
        import collection.site_collector as site_collector_module

        monkeypatch.setattr(site_collector_module, "MAX_CERTIFICATE_DOWNLOADS", 0)
        collector = SiteCollector(artifact_store=artifact_store)
        result = collector.collect(supplier_id=1, domain=local_site)

        assert result.success is True  # collection itself is unaffected
        assert result.certificate_documents == []

    def test_broken_certificate_link_is_skipped_not_fatal(self, local_site, artifact_store):
        """rohs-certificate-missing.pdf is linked from index.html but not
        a real file (404) -- must be silently skipped, and must not
        stop the real iso-9001-certificate.pdf from being downloaded,
        and must not fail the collection run as a whole."""
        collector = SiteCollector(artifact_store=artifact_store)
        result = collector.collect(supplier_id=1, domain=local_site)

        assert result.success is True
        assert not any("rohs-certificate-missing" in doc.url for doc in result.certificate_documents)
        assert any("iso-9001-certificate.pdf" in doc.url for doc in result.certificate_documents)


class TestExtractionHelpers:
    """Pure-function tests, no browser/server needed."""

    def test_find_relevant_links_excludes_downloads(self):
        html = '<a href="/catalogue.pdf">Catalog PDF</a><a href="/about.html">About</a>'
        links = _find_relevant_links("https://acme.example.com/", html)
        assert links == ["https://acme.example.com/about.html"]

    def test_find_relevant_links_never_follows_off_domain(self):
        html = '<a href="https://other.example.com/about.html">About</a>'
        links = _find_relevant_links("https://acme.example.com/", html)
        assert links == []

    def test_find_social_links(self):
        html = '<a href="https://linkedin.com/company/acme">LI</a><a href="/about.html">About</a>'
        links = _find_social_links(html)
        assert links == ["https://linkedin.com/company/acme"]

    def test_find_download_links_resolves_relative_urls(self):
        html = '<a href="catalogue.pdf">Catalog</a>'
        links = _find_download_links("https://acme.example.com/products/", html)
        assert links == ["https://acme.example.com/products/catalogue.pdf"]

    def test_has_contact_form_true_for_real_contact_form(self):
        html = '<form><input type="email" name="email"><textarea name="message"></textarea></form>'
        assert _has_contact_form(html) is True

    def test_has_contact_form_false_for_search_box(self):
        html = '<form><input type="text" name="q" placeholder="Search..."></form>'
        assert _has_contact_form(html) is False

    def test_find_certificate_candidates_matches_keyword(self):
        candidates = _find_certificate_candidates(["https://acme.example.com/iso-9001-cert.pdf"])
        assert candidates == [("https://acme.example.com/iso-9001-cert.pdf", "iso")]

    def test_find_certificate_candidates_ignores_non_matching_links(self):
        candidates = _find_certificate_candidates(["https://acme.example.com/catalogue.pdf"])
        assert candidates == []

    def test_find_certificate_candidates_deduplicates(self):
        url = "https://acme.example.com/iso-9001-cert.pdf"
        candidates = _find_certificate_candidates([url, url])
        assert len(candidates) == 1


class TestBuildCandidateUrls:
    """_build_candidate_urls is the fix for the URL-normalisation bug:
    a bare domain used to be fetched only as https://{domain} (protocol
    upgraded AND www stripped in one step), which breaks hosts that
    only resolve on www. Order matters -- see the function's own
    docstring -- so these tests pin the exact sequence, not just set
    membership."""

    def test_no_source_url_tries_www_first_then_bare_then_http_www(self):
        assert _build_candidate_urls("daroaxle.com", None) == [
            "https://www.daroaxle.com",
            "https://daroaxle.com",
            "http://www.daroaxle.com",
        ]

    def test_source_url_is_tried_first_when_given(self):
        candidates = _build_candidate_urls("daroaxle.com", "http://www.daroaxle.com")
        assert candidates[0] == "http://www.daroaxle.com"

    def test_source_url_without_a_scheme_gets_https_prefixed(self):
        candidates = _build_candidate_urls("daroaxle.com", "www.daroaxle.com")
        assert candidates[0] == "https://www.daroaxle.com"

    def test_source_url_duplicating_a_later_candidate_is_not_repeated(self):
        """source_url == http://www.X is exactly the last generated
        candidate -- it should only appear once, first."""
        candidates = _build_candidate_urls("daroaxle.com", "http://www.daroaxle.com")
        assert candidates.count("http://www.daroaxle.com") == 1
        assert candidates == [
            "http://www.daroaxle.com",
            "https://www.daroaxle.com",
            "https://daroaxle.com",
        ]

    def test_www_prefixed_domain_input_is_normalised_not_doubled(self):
        """If `domain` itself already has www (shouldn't happen given
        extract_domain always strips it, but must not produce
        'www.www.X' if it ever does)."""
        candidates = _build_candidate_urls("www.daroaxle.com", None)
        assert candidates == [
            "https://www.daroaxle.com",
            "https://daroaxle.com",
            "http://www.daroaxle.com",
        ]

    def test_no_source_url_means_only_three_candidates(self):
        assert len(_build_candidate_urls("daroaxle.com", None)) == 3


class _FakePage:
    def __init__(self, working_urls, html="<html><body>ok</body></html>"):
        self._working_urls = set(working_urls)
        self._html = html
        self.goto_calls: List[str] = []
        self._current_url = None

    def goto(self, url, timeout=None):
        self.goto_calls.append(url)
        if url not in self._working_urls:
            raise RuntimeError(f"net::ERR_NAME_NOT_RESOLVED for {url}")
        self._current_url = url

    def content(self):
        return self._html

    def screenshot(self, full_page=True):
        return b"fake-png-bytes"


class _FakeContext:
    def __init__(self, page):
        self._page = page

    def new_page(self):
        return self._page

    def set_default_timeout(self, ms):
        pass


class _FakeBrowser:
    def __init__(self, context):
        self._context = context
        self.closed = False

    def new_context(self):
        return self._context

    def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, browser):
        self._browser = browser

    def launch(self, **kwargs):
        return self._browser


class _FakePlaywright:
    """Injectable via SiteCollector(playwright_factory=...) -- see
    site_collector.py's own docstring for why this seam exists (tests
    don't need a real browser launch to exercise the fallback logic)."""

    def __init__(self, working_urls):
        self.page = _FakePage(working_urls)
        context = _FakeContext(self.page)
        browser = _FakeBrowser(context)
        self.chromium = _FakeChromium(browser)


class TestCandidateUrlFallback:
    """Exercises collect()'s multi-candidate retry against a fake
    Playwright (see _FakePage) -- no real network/DNS involved, so
    "only www resolves" is simulated directly rather than needing a
    real host that behaves that way."""

    def test_falls_through_to_the_candidate_that_actually_works(self, artifact_store):
        fake = _FakePlaywright(working_urls={"https://www.daroaxle.com"})
        collector = SiteCollector(artifact_store=artifact_store, playwright_factory=lambda: fake)
        result = collector.collect(supplier_id=1, domain="daroaxle.com")

        assert result.success is True
        assert result.resolved_url == "https://www.daroaxle.com"
        # bare https tried and failed before falling back to www
        assert fake.page.goto_calls[0] == "https://www.daroaxle.com"

    def test_source_url_that_works_short_circuits_further_attempts(self, artifact_store):
        fake = _FakePlaywright(working_urls={"http://www.daroaxle.com"})
        collector = SiteCollector(artifact_store=artifact_store, playwright_factory=lambda: fake)
        result = collector.collect(supplier_id=1, domain="daroaxle.com", source_url="http://www.daroaxle.com")

        assert result.success is True
        assert result.resolved_url == "http://www.daroaxle.com"
        assert fake.page.goto_calls == ["http://www.daroaxle.com"]  # only one attempt needed

    def test_only_last_candidate_works_all_others_are_tried_first(self, artifact_store):
        fake = _FakePlaywright(working_urls={"http://www.daroaxle.com"})
        collector = SiteCollector(artifact_store=artifact_store, playwright_factory=lambda: fake)
        result = collector.collect(supplier_id=1, domain="daroaxle.com")

        assert result.success is True
        assert result.resolved_url == "http://www.daroaxle.com"
        assert fake.page.goto_calls == [
            "https://www.daroaxle.com", "https://daroaxle.com", "http://www.daroaxle.com",
        ]

    def test_no_candidate_works_reports_failure_with_all_urls_tried(self, artifact_store):
        fake = _FakePlaywright(working_urls=set())
        collector = SiteCollector(artifact_store=artifact_store, playwright_factory=lambda: fake)
        result = collector.collect(supplier_id=1, domain="daroaxle.com")

        assert result.success is False
        assert result.resolved_url is None
        assert "https://www.daroaxle.com" in result.error
        assert "http://www.daroaxle.com" in result.error
        assert len(fake.page.goto_calls) == 3

    def test_full_url_domain_bypasses_candidate_generation_entirely(self, artifact_store):
        """A caller-supplied domain that's already a scheme+host (e.g.
        a test server, or an explicit override) must not be mangled
        into www/http guesses -- matches every existing
        TestSiteCollectorRealBrowser test's calling convention."""
        fake = _FakePlaywright(working_urls={"http://127.0.0.1:9999"})
        collector = SiteCollector(artifact_store=artifact_store, playwright_factory=lambda: fake)
        result = collector.collect(supplier_id=1, domain="http://127.0.0.1:9999", source_url="http://www.ignored.com")

        assert result.success is True
        assert fake.page.goto_calls == ["http://127.0.0.1:9999"]
