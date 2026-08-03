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
