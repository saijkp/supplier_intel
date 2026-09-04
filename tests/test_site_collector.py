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
    _MAX_IFRAMES_PER_PAGE,
    _build_candidate_urls,
    _extract_facility_photo_urls,
    _extract_footer_text,
    _extract_sitemap_locs,
    _filter_relevant_sitemap_urls,
    _find_certificate_candidates,
    _find_download_links,
    _find_mailto_emails,
    _find_relevant_links,
    _find_social_links,
    _find_tel_phones,
    _has_contact_form,
    _prioritise_relevant_links,
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
                <a href="/impressum.html">Impressum</a>
                <a href="/catalogue.pdf">Download Catalogue (PDF)</a>
                <a href="/iso-9001-certificate.pdf">ISO 9001 Certificate</a>
                <a href="/rohs-certificate-missing.pdf">RoHS Certificate (broken link)</a>
                <a href="https://linkedin.com/company/acme-trailer">LinkedIn</a>
                <a href="https://facebook.com/acmetrailer">Facebook</a>
            </nav>
            <img src="/images/factory-floor.jpg" alt="factory">
            <img src="/images/logo.png" alt="logo">
            <footer>
                <p>Acme Trailer Co, 123 Industrial Way, Springfield, IL 62704, USA</p>
                <p>&copy; 2026 Acme Trailer Co. All rights reserved.</p>
            </footer>
        </body></html>
    """,
    "impressum.html": """
        <html><body>
            <h1>Impressum</h1>
            <p>Acme Trailer Co GmbH, 5 Impressum Str, 10115 Berlin, Germany.</p>
            <p>Registered at the local court, VAT ID DE123456789.</p>
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


def _serve_files(tmp_path, files: dict):
    """Shared boilerplate behind local_site/sitemap_only_site -- writes
    `files` to a fresh temp dir and serves them over a real local
    http.server. Caller is responsible for shutting the server down."""
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    for name, content in files.items():
        (site_dir / name).write_text(content, encoding="utf-8")
    (site_dir / "images").mkdir(exist_ok=True)

    handler = lambda *args, **kwargs: _QuietHandler(*args, directory=str(site_dir), **kwargs)
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{port}"


@pytest.fixture()
def local_site(tmp_path):
    server, thread, base_url = _serve_files(tmp_path, _SITE_FILES)
    try:
        yield base_url
    finally:
        server.shutdown()
        thread.join(timeout=5)


# A page reachable ONLY via /sitemap.xml -- deliberately not linked
# anywhere in index.html's nav, and its filename/anchor-text would
# never exist since there's no anchor at all. Mirrors the real gap
# found live (Mansfield Engineered Components' "Commitment" page):
# capability evidence sitting on a page the homepage simply never
# points at with matching link text.
_SITEMAP_ONLY_SITE_FILES = {
    "index.html": """
        <html><head><title>Acme Trailer Co</title></head>
        <body>
            <h1>Acme Trailer Co</h1>
            <nav>
                <a href="/contact.html">Contact</a>
            </nav>
        </body></html>
    """,
    "contact.html": "<html><body><h1>Contact Us</h1></body></html>",
    "sitemap.xml": """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>/commitment.html</loc></url>
</urlset>""",
    "commitment.html": """
        <html><body>
            <h1>Our Commitment</h1>
            <p>Founded in 1962, we added metal stamping to our capabilities in 1965.</p>
        </body></html>
    """,
}


@pytest.fixture()
def sitemap_only_site(tmp_path):
    server, thread, base_url = _serve_files(tmp_path, _SITEMAP_ONLY_SITE_FILES)
    try:
        yield base_url
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture()
def artifact_store(tmp_path):
    return ArtifactStore(base_dir=tmp_path / "artifacts")


@pytest.mark.slow
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

    def test_homepage_footer_text_is_captured(self, local_site, artifact_store):
        collector = SiteCollector(artifact_store=artifact_store)
        result = collector.collect(supplier_id=1, domain=local_site)
        homepage = result.pages[0]
        assert "Acme Trailer Co, 123 Industrial Way, Springfield, IL 62704, USA" in homepage.footer_text

    def test_homepage_facility_photo_is_captured(self, local_site, artifact_store):
        """The homepage fixture's own factory-floor.jpg (alt="factory")
        should be picked up as a facility-photo candidate."""
        collector = SiteCollector(artifact_store=artifact_store)
        result = collector.collect(supplier_id=1, domain=local_site)
        homepage = result.pages[0]
        assert any("factory-floor.jpg" in u for u in homepage.facility_photo_urls)
        assert not any("logo.png" in u for u in homepage.facility_photo_urls)

    def test_page_with_no_footer_has_empty_footer_text(self, local_site, artifact_store):
        collector = SiteCollector(artifact_store=artifact_store)
        result = collector.collect(supplier_id=1, domain=local_site)
        contact_page = next(p for p in result.pages if "contact.html" in p.url)
        assert contact_page.footer_text == ""

    def test_impressum_page_is_followed_and_collected(self, local_site, artifact_store):
        collector = SiteCollector(artifact_store=artifact_store, max_pages=6)
        result = collector.collect(supplier_id=1, domain=local_site)
        urls = {p.url for p in result.pages}
        assert any("impressum.html" in u for u in urls)
        impressum_page = next(p for p in result.pages if "impressum.html" in p.url)
        assert "5 Impressum Str, 10115 Berlin, Germany" in impressum_page.text


@pytest.mark.slow
class TestSitemapDiscovery:
    """Real Playwright + real local HTTP server -- proves a page no
    homepage anchor names usefully (not in index.html's nav at all,
    here) still gets visited when /sitemap.xml lists it. The real gap
    this closes: Mansfield Engineered Components' own "Commitment"
    (company history) page was linked from its homepage nav but neither
    its URL nor anchor text matched any capability keyword, so
    _find_relevant_links alone never surfaced it -- yet it was the only
    page on the whole site that said the company does metal stamping."""

    def test_sitemap_only_page_is_discovered_and_visited(self, sitemap_only_site, artifact_store):
        collector = SiteCollector(artifact_store=artifact_store, max_pages=6)
        result = collector.collect(supplier_id=1, domain=sitemap_only_site)

        assert result.success is True
        urls = {p.url for p in result.pages}
        assert any("commitment.html" in u for u in urls)
        commitment_page = next(p for p in result.pages if "commitment.html" in p.url)
        assert "metal stamping" in commitment_page.text

    def test_sitemap_missing_entirely_is_not_fatal(self, local_site, artifact_store):
        """local_site's fixture files have no sitemap.xml at all -- a
        404 there must not affect collection of the rest of the site."""
        collector = SiteCollector(artifact_store=artifact_store, max_pages=6)
        result = collector.collect(supplier_id=1, domain=local_site)
        assert result.success is True
        assert any("contact.html" in p.url for p in result.pages)


@pytest.mark.slow
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

    def test_extract_footer_text_returns_footer_content(self):
        html = "<html><body><h1>Home</h1><footer><p>Acme Co, 1 Main St, Springfield</p></footer></body></html>"
        assert "Acme Co, 1 Main St, Springfield" in _extract_footer_text(html)

    def test_extract_footer_text_excludes_content_outside_footer(self):
        html = "<html><body><h1>Home page heading</h1><footer><p>Footer only text</p></footer></body></html>"
        text = _extract_footer_text(html)
        assert "Footer only text" in text
        assert "Home page heading" not in text

    def test_extract_footer_text_empty_when_no_footer_tag(self):
        html = "<html><body><h1>Home</h1><p>No footer here</p></body></html>"
        assert _extract_footer_text(html) == ""

    def test_facility_photo_matched_via_alt_text(self):
        html = '<html><body><img src="/img1.jpg" alt="Our factory floor"></body></html>'
        urls = _extract_facility_photo_urls("https://acme.example.com/random-page", html)
        assert urls == ["https://acme.example.com/img1.jpg"]

    def test_facility_photo_matched_via_facility_flavoured_page_url_even_with_no_alt_text(self):
        html = '<html><body><img src="/gallery1.jpg"></body></html>'
        urls = _extract_facility_photo_urls("https://acme.example.com/about-our-factory", html)
        assert urls == ["https://acme.example.com/gallery1.jpg"]

    def test_image_with_no_facility_signal_on_an_unrelated_page_is_excluded(self):
        html = '<html><body><img src="/random.jpg"></body></html>'
        urls = _extract_facility_photo_urls("https://acme.example.com/products", html)
        assert urls == []

    def test_logo_and_icon_images_are_still_excluded_even_on_a_facility_page(self):
        html = '<html><body><img src="/logo.png"><img src="/icon-favicon.png"></body></html>'
        urls = _extract_facility_photo_urls("https://acme.example.com/about-our-factory", html)
        assert urls == []

    def test_relative_urls_are_resolved_to_absolute(self):
        html = '<html><body><img src="factory1.jpg" alt="factory workshop"></body></html>'
        urls = _extract_facility_photo_urls("https://acme.example.com/about/", html)
        assert urls == ["https://acme.example.com/about/factory1.jpg"]

    def test_deduplicates_repeated_image_urls(self):
        html = '<html><body><img src="/f.jpg" alt="factory"><img src="/f.jpg" alt="factory"></body></html>'
        urls = _extract_facility_photo_urls("https://acme.example.com/about", html)
        assert urls == ["https://acme.example.com/f.jpg"]

    def test_capped_at_the_per_page_maximum(self):
        imgs = "".join(f'<img src="/f{i}.jpg" alt="factory">' for i in range(10))
        html = f"<html><body>{imgs}</body></html>"
        urls = _extract_facility_photo_urls("https://acme.example.com/about", html)
        assert len(urls) == 5

    def test_find_relevant_links_excludes_downloads(self):
        html = '<a href="/catalogue.pdf">Catalog PDF</a><a href="/about.html">About</a>'
        links = _find_relevant_links("https://acme.example.com/", html)
        assert links == ["https://acme.example.com/about.html"]

    def test_find_relevant_links_never_follows_off_domain(self):
        html = '<a href="https://other.example.com/about.html">About</a>'
        links = _find_relevant_links("https://acme.example.com/", html)
        assert links == []

    def test_prioritise_relevant_links_moves_contact_and_about_first(self):
        """The real gap this fixes: a genuine contact page reachable in
        one click was consistently losing its slot in the page budget
        to blog/product links that merely appeared earlier in the
        homepage's HTML -- confirmed live against 3erp.com (contact
        page never crawled in 6 pages: blog/ebooks/design-tips filled
        the budget first) and plasticmold.net (same pattern)."""
        links = [
            "https://acme.example.com/blog/",
            "https://acme.example.com/products/",
            "https://acme.example.com/contact-us/",
            "https://acme.example.com/catalog/",
            "https://acme.example.com/about/",
            "https://acme.example.com/impressum/",
        ]
        result = _prioritise_relevant_links(links)
        assert result == [
            "https://acme.example.com/contact-us/",
            "https://acme.example.com/about/",
            "https://acme.example.com/impressum/",
            "https://acme.example.com/blog/",
            "https://acme.example.com/products/",
            "https://acme.example.com/catalog/",
        ]

    def test_prioritise_relevant_links_is_stable_within_each_tier(self):
        """Priority links keep their relative discovery order among
        themselves, and so do non-priority links -- this only ever
        reorders which TIER goes first, never shuffles within a tier."""
        links = [
            "https://acme.example.com/products/",
            "https://acme.example.com/about/",
            "https://acme.example.com/blog/",
            "https://acme.example.com/contact/",
        ]
        result = _prioritise_relevant_links(links)
        assert result == [
            "https://acme.example.com/about/",
            "https://acme.example.com/contact/",
            "https://acme.example.com/products/",
            "https://acme.example.com/blog/",
        ]

    def test_prioritise_relevant_links_never_drops_a_candidate(self):
        links = ["https://acme.example.com/x/", "https://acme.example.com/y/", "https://acme.example.com/contact/"]
        assert set(_prioritise_relevant_links(links)) == set(links)
        assert len(_prioritise_relevant_links(links)) == len(links)

    def test_prioritise_relevant_links_moves_history_tier_first_too(self):
        """The company-history tier (found live: Mansfield Engineered
        Components' "Commitment" page) deserves the same budget
        priority as contact/about/impressum -- it's exactly the kind of
        page that can carry evidence no other page on the site has."""
        links = ["https://acme.example.com/blog/", "https://acme.example.com/commitment/"]
        assert _prioritise_relevant_links(links) == [
            "https://acme.example.com/commitment/",
            "https://acme.example.com/blog/",
        ]

    def test_extract_sitemap_locs_from_a_plain_urlset(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://acme.example.com/about/</loc></url>
<url><loc>https://acme.example.com/contact/</loc></url>
</urlset>"""
        assert _extract_sitemap_locs(xml) == [
            "https://acme.example.com/about/",
            "https://acme.example.com/contact/",
        ]

    def test_extract_sitemap_locs_from_a_sitemap_index(self):
        """Same function, same <loc> tag -- a sitemap INDEX's <sitemap>
        entries use the identical tag, so no special-casing is needed
        to pull out the sub-feed URLs themselves."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://acme.example.com/page-sitemap.xml</loc></sitemap>
<sitemap><loc>https://acme.example.com/post-sitemap.xml</loc></sitemap>
</sitemapindex>"""
        assert _extract_sitemap_locs(xml) == [
            "https://acme.example.com/page-sitemap.xml",
            "https://acme.example.com/post-sitemap.xml",
        ]

    def test_extract_sitemap_locs_empty_for_malformed_xml(self):
        assert _extract_sitemap_locs("not xml at all") == []

    def test_filter_relevant_sitemap_urls_keeps_only_keyword_matches(self):
        urls = [
            "https://acme.example.com/commitment/",
            "https://acme.example.com/blog/2024/whats-new/",
            "https://acme.example.com/about/",
        ]
        assert _filter_relevant_sitemap_urls(urls) == [
            "https://acme.example.com/commitment/",
            "https://acme.example.com/about/",
        ]

    def test_filter_relevant_sitemap_urls_empty_when_nothing_matches(self):
        assert _filter_relevant_sitemap_urls(["https://acme.example.com/blog/random-post/"]) == []

    def test_find_social_links(self):
        html = '<a href="https://linkedin.com/company/acme">LI</a><a href="/about.html">About</a>'
        links = _find_social_links(html)
        assert links == ["https://linkedin.com/company/acme"]

    def test_find_download_links_resolves_relative_urls(self):
        html = '<a href="catalogue.pdf">Catalog</a>'
        links = _find_download_links("https://acme.example.com/products/", html)
        assert links == ["https://acme.example.com/products/catalogue.pdf"]

    def test_find_mailto_emails_extracts_address_from_href(self):
        html = '<a href="mailto:sales@acme.example.com">Click Here</a>'
        assert _find_mailto_emails(html) == ["sales@acme.example.com"]

    def test_find_mailto_emails_strips_subject_query_string(self):
        html = '<a href="mailto:sales@acme.example.com?subject=Enquiry">Email us</a>'
        assert _find_mailto_emails(html) == ["sales@acme.example.com"]

    def test_find_mailto_emails_deduplicates(self):
        html = (
            '<a href="mailto:sales@acme.example.com">Top</a>'
            '<a href="mailto:sales@acme.example.com">Bottom</a>'
        )
        assert _find_mailto_emails(html) == ["sales@acme.example.com"]

    def test_find_mailto_emails_empty_when_no_mailto_links(self):
        html = '<a href="/about.html">About</a>'
        assert _find_mailto_emails(html) == []

    def test_find_tel_phones_extracts_number_from_href(self):
        html = '<a href="tel:+441754880481">Call Us</a>'
        assert _find_tel_phones(html) == ["+441754880481"]

    def test_find_tel_phones_national_format_preserved_as_is(self):
        """No parsing/validation here -- that's
        verification.website_contact_extractor.extract_tel_phones's job
        (it needs a default_region to interpret a national-format
        number like this one). This function only pulls the raw href
        value out, unchanged."""
        html = '<a href="tel:01754880481">Call Us</a>'
        assert _find_tel_phones(html) == ["01754880481"]

    def test_find_tel_phones_empty_when_no_tel_links(self):
        html = '<a href="/about.html">About</a>'
        assert _find_tel_phones(html) == []

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


class _FakeResponse:
    """Mirrors the real Playwright Response object's one attribute
    _visit_and_collect actually reads."""
    def __init__(self, status):
        self.status = status


class _FakeFrame:
    """Mirrors a real Playwright Frame's one method _collect_iframe_html
    actually calls. `raises=True` simulates a frame that's detached/
    mid-navigation by the time it's read -- must be skipped, never
    fatal (see TestIframeContentExtraction)."""
    def __init__(self, html, raises=False):
        self._html = html
        self._raises = raises

    def content(self):
        if self._raises:
            raise RuntimeError("frame is detached")
        return self._html


class _FakePage:
    def __init__(self, working_urls, html="<html><body>ok</body></html>", redirects=None, html_by_url=None,
                 status_by_url=None, iframe_html_by_url=None, frames_raise_for_urls=None):
        self._working_urls = set(working_urls)
        self._html = html
        # url -> list of HTML strings, one per simulated child <iframe>
        # attached to that page (e.g. an embedded Marketo/HubSpot
        # contact-form widget) -- see TestIframeContentExtraction.
        self._iframe_html_by_url = iframe_html_by_url or {}
        # Set of URLs where reading page.frames itself should raise
        # (as opposed to an individual frame's .content() raising) --
        # _collect_iframe_html must degrade to "" rather than crash.
        self._frames_raise_for_urls = set(frames_raise_for_urls or ())
        # A distinct sentinel object per page instance so `frame is
        # page.main_frame` in the real code correctly identifies and
        # skips it, the same identity check Playwright's own
        # page.main_frame supports.
        self._main_frame_sentinel = _FakeFrame("")
        # url -> the URL the page actually ends up at, e.g. a non-www
        # candidate that redirects to www -- see TestRedirectHandling,
        # which regression-tests the real bug this simulates: a same-
        # domain link filter comparing against the pre-redirect URL
        # instead of where the page actually landed.
        self._redirects = redirects or {}
        # url -> the HTML to serve once landed there (keyed by the
        # FINAL, post-redirect URL) -- defaults to `html` for every URL
        # when not given.
        self._html_by_url = html_by_url or {}
        # url -> the HTTP status page.goto() "returns" for it -- defaults
        # to 200 (a normal successful load) for every working URL. See
        # TestBlockedResponseHandling: a URL can be in `working_urls`
        # (goto() doesn't raise) while still status_by_url'd to 403/etc,
        # exactly like a real WAF block page that loads without error.
        self._status_by_url = status_by_url or {}
        self.goto_calls: List[str] = []
        self.goto_wait_until_values: List[Any] = []
        self._current_url = None

    def goto(self, url, timeout=None, wait_until=None):
        self.goto_calls.append(url)
        self.goto_wait_until_values.append(wait_until)
        if url not in self._working_urls:
            raise RuntimeError(f"net::ERR_NAME_NOT_RESOLVED for {url}")
        self._current_url = self._redirects.get(url, url)
        return _FakeResponse(self._status_by_url.get(url, 200))

    @property
    def url(self):
        # Mirrors real Playwright's page.url -- the page's actual
        # current location after navigation (what site_collector.py's
        # _visit_and_collect now reads instead of the pre-navigation
        # `url` parameter, so a redirect is reflected correctly). This
        # fake never simulates a real redirect, so it's just the last
        # successfully-navigated URL -- sufficient for these tests,
        # which are about candidate fallback, not redirect handling.
        return self._current_url

    def wait_for_selector(self, selector, timeout=None):
        pass

    def content(self):
        return self._html_by_url.get(self._current_url, self._html)

    def screenshot(self, full_page=True):
        return b"fake-png-bytes"

    @property
    def main_frame(self):
        return self._main_frame_sentinel

    @property
    def frames(self):
        if self._current_url in self._frames_raise_for_urls:
            raise RuntimeError("page.frames unavailable")
        child_htmls = self._iframe_html_by_url.get(self._current_url, [])
        return [self._main_frame_sentinel] + [
            item if isinstance(item, _FakeFrame) else _FakeFrame(item) for item in child_htmls
        ]


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

    def __init__(self, working_urls, html="<html><body>ok</body></html>", redirects=None, html_by_url=None,
                 status_by_url=None, iframe_html_by_url=None, frames_raise_for_urls=None):
        self.page = _FakePage(working_urls, html=html, redirects=redirects, html_by_url=html_by_url,
                               status_by_url=status_by_url, iframe_html_by_url=iframe_html_by_url,
                               frames_raise_for_urls=frames_raise_for_urls)
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


class TestRedirectHandling:
    """Regression tests for a real bug found via a gap-analysis run
    against 29 confirmed injection-moulding candidates: when the
    literal source_url (a bare non-www domain, e.g. "plasticmold.net")
    is tried first and the site redirects to www, every link on the
    homepage is absolute to the www host -- but the same-domain filter
    in _find_relevant_links was comparing those links against the
    PRE-redirect base_url, so it wrongly treated every single one as
    off-domain and silently capped the crawl at just the homepage.
    Confirmed live: plasticmold.net (0 of 15 relevant links kept vs 15
    when requested directly as www) and hordrt.com (1 of 6 kept)."""

    def test_links_on_a_redirected_homepage_are_still_followed(self, artifact_store):
        homepage_html = (
            '<html><body>'
            '<a href="https://www.plasticmold.net/company/">About Us</a>'
            '<a href="https://www.plasticmold.net/contact-us/">Contact</a>'
            '</body></html>'
        )
        fake = _FakePlaywright(
            working_urls={
                "https://plasticmold.net", "https://www.plasticmold.net/company/",
                "https://www.plasticmold.net/contact-us/",
            },
            redirects={"https://plasticmold.net": "https://www.plasticmold.net"},
            html_by_url={"https://www.plasticmold.net": homepage_html},
        )
        collector = SiteCollector(artifact_store=artifact_store, playwright_factory=lambda: fake)

        result = collector.collect(supplier_id=1, domain="plasticmold.net", source_url="plasticmold.net")

        assert result.success is True
        # Before the fix this was 1 (homepage only) -- every link on the
        # redirected page was wrongly rejected as off-domain.
        assert len(result.pages) == 3
        assert {p.url for p in result.pages} == {
            "https://www.plasticmold.net",
            "https://www.plasticmold.net/company/",
            "https://www.plasticmold.net/contact-us/",
        }

    def test_homepage_collected_page_url_reflects_the_post_redirect_location(self, artifact_store):
        fake = _FakePlaywright(
            working_urls={"https://plasticmold.net"},
            redirects={"https://plasticmold.net": "https://www.plasticmold.net"},
        )
        collector = SiteCollector(artifact_store=artifact_store, playwright_factory=lambda: fake)

        result = collector.collect(supplier_id=1, domain="plasticmold.net", source_url="plasticmold.net")

        assert result.pages[0].url == "https://www.plasticmold.net"

    def test_no_redirect_behaves_exactly_as_before(self, artifact_store):
        """A same-domain homepage with no redirect involved must still
        follow its links normally -- the fix must not regress the
        already-working, no-redirect case."""
        homepage_html = '<html><body><a href="https://www.daroaxle.com/contact/">Contact</a></body></html>'
        fake = _FakePlaywright(
            working_urls={"https://www.daroaxle.com", "https://www.daroaxle.com/contact/"},
            html_by_url={"https://www.daroaxle.com": homepage_html},
        )
        collector = SiteCollector(artifact_store=artifact_store, playwright_factory=lambda: fake)

        result = collector.collect(supplier_id=1, domain="daroaxle.com")

        assert result.success is True
        assert len(result.pages) == 2


class TestBlockedResponseHandling:
    """Real bug found via a gap-analysis run against a UK asphalt-
    supplier batch: northumbrianroads.co.uk's WAF returns a real, tiny
    "403 - Forbidden" HTML page for this collector's traffic --
    page.goto() doesn't raise (there IS a valid document, Playwright
    considers the navigation successful), so the 403 page's own thin
    content was treated as the real homepage. It correctly has zero
    relevant links, so the crawl silently stopped at 1 page and
    reported success with zero contact data, even though the site's
    real contact page (linked from its nav) has three depots' worth of
    phone numbers, an email, and full addresses -- a genuine failure
    disguised as an empty success, not an honest "we're blocked"."""

    def test_homepage_returning_403_is_treated_as_failed_not_empty_success(self, artifact_store):
        fake = _FakePlaywright(
            working_urls={"https://www.northumbrianroads.co.uk"},
            status_by_url={"https://www.northumbrianroads.co.uk": 403},
        )
        collector = SiteCollector(artifact_store=artifact_store, playwright_factory=lambda: fake)

        result = collector.collect(supplier_id=1, domain="northumbrianroads.co.uk")

        assert result.success is False
        assert "could not load homepage" in result.error

    def test_blocked_first_candidate_falls_through_to_a_working_one(self, artifact_store):
        """The existing multi-candidate retry (TestCandidateUrlFallback)
        already tries www/bare/http variants on a network-level
        failure -- a 403 on the first candidate must trigger the exact
        same fallback, not a hard stop."""
        fake = _FakePlaywright(
            working_urls={"https://www.daroaxle.com", "https://daroaxle.com"},
            status_by_url={"https://www.daroaxle.com": 403},
        )
        collector = SiteCollector(artifact_store=artifact_store, playwright_factory=lambda: fake)

        result = collector.collect(supplier_id=1, domain="daroaxle.com")

        assert result.success is True
        assert result.resolved_url == "https://daroaxle.com"

    def test_blocked_secondary_page_is_skipped_not_counted_as_visited(self, artifact_store):
        """A homepage that loads fine but whose contact page 403s (e.g.
        a rate-limited or auth-gated sub-page) must be silently skipped
        -- same as any other unreachable secondary page -- rather than
        counted as a real visited page with a WAF page's irrelevant
        text mixed into extraction."""
        homepage_html = '<html><body><a href="https://www.daroaxle.com/contact/">Contact</a></body></html>'
        fake = _FakePlaywright(
            working_urls={"https://www.daroaxle.com", "https://www.daroaxle.com/contact/"},
            html_by_url={"https://www.daroaxle.com": homepage_html},
            status_by_url={"https://www.daroaxle.com/contact/": 403},
        )
        collector = SiteCollector(artifact_store=artifact_store, playwright_factory=lambda: fake)

        result = collector.collect(supplier_id=1, domain="daroaxle.com")

        assert result.success is True
        assert len(result.pages) == 1  # only the homepage -- the blocked contact page was skipped
        assert result.pages[0].url == "https://www.daroaxle.com"

    def test_a_normal_200_response_is_unaffected(self, artifact_store):
        """Regression guard: the default (no status_by_url given, or an
        explicit 200) must behave exactly as every other test in this
        file already assumes."""
        fake = _FakePlaywright(working_urls={"https://www.daroaxle.com"})
        collector = SiteCollector(artifact_store=artifact_store, playwright_factory=lambda: fake)

        result = collector.collect(supplier_id=1, domain="daroaxle.com")

        assert result.success is True
        assert len(result.pages) == 1


class TestIframeContentExtraction:
    """Real gap found live: nVent's real "Contact Us" page was
    correctly discovered and visited (481 relevant links found, this
    exact page prioritised first) but still produced zero contact info
    and no detected contact form. page.content() only ever returns the
    MAIN document's HTML -- a large modern enterprise site's actual
    contact mechanism is very commonly a third-party form widget
    embedded via <iframe>, a genuinely separate document
    page.content() never sees. _collect_iframe_html closes this by
    reading every child frame Playwright already has attached."""

    def test_mailto_in_an_iframe_is_still_found(self, artifact_store):
        homepage_html = "<html><body>No contact info in the main document at all.</body></html>"
        widget_html = '<html><body><a href="mailto:sales@nvent.com">Email us</a></body></html>'
        fake = _FakePlaywright(
            working_urls={"https://www.daroaxle.com"},
            html_by_url={"https://www.daroaxle.com": homepage_html},
            iframe_html_by_url={"https://www.daroaxle.com": [widget_html]},
        )
        collector = SiteCollector(artifact_store=artifact_store, playwright_factory=lambda: fake)

        result = collector.collect(supplier_id=1, domain="daroaxle.com")

        assert result.success is True
        assert result.pages[0].mailto_emails == ["sales@nvent.com"]

    def test_tel_in_an_iframe_is_still_found(self, artifact_store):
        homepage_html = "<html><body>No contact info in the main document at all.</body></html>"
        widget_html = '<html><body><a href="tel:+15551234567">Call us</a></body></html>'
        fake = _FakePlaywright(
            working_urls={"https://www.daroaxle.com"},
            html_by_url={"https://www.daroaxle.com": homepage_html},
            iframe_html_by_url={"https://www.daroaxle.com": [widget_html]},
        )
        collector = SiteCollector(artifact_store=artifact_store, playwright_factory=lambda: fake)

        result = collector.collect(supplier_id=1, domain="daroaxle.com")

        assert result.pages[0].tel_phones == ["+15551234567"]

    def test_contact_form_inside_an_iframe_is_detected(self, artifact_store):
        """The exact real nVent/Tratos-shaped case: the main document
        has no <form> of its own at all -- the form lives entirely in
        an embedded widget."""
        homepage_html = "<html><body>No form in the main document.</body></html>"
        widget_html = (
            "<html><body><form>"
            '<input type="text" name="name">'
            '<input type="email" name="email">'
            '<textarea name="message"></textarea>'
            "</form></body></html>"
        )
        fake = _FakePlaywright(
            working_urls={"https://www.daroaxle.com"},
            html_by_url={"https://www.daroaxle.com": homepage_html},
            iframe_html_by_url={"https://www.daroaxle.com": [widget_html]},
        )
        collector = SiteCollector(artifact_store=artifact_store, playwright_factory=lambda: fake)

        result = collector.collect(supplier_id=1, domain="daroaxle.com")

        assert result.pages[0].has_contact_form is True

    def test_no_iframes_behaves_exactly_as_before(self, artifact_store):
        """Regression guard: a page with no child frames at all (the
        overwhelming majority of real pages) must be entirely
        unaffected by this change."""
        homepage_html = '<html><body><a href="mailto:info@daroaxle.com">Email</a></body></html>'
        fake = _FakePlaywright(
            working_urls={"https://www.daroaxle.com"},
            html_by_url={"https://www.daroaxle.com": homepage_html},
        )
        collector = SiteCollector(artifact_store=artifact_store, playwright_factory=lambda: fake)

        result = collector.collect(supplier_id=1, domain="daroaxle.com")

        assert result.pages[0].mailto_emails == ["info@daroaxle.com"]

    def test_a_raising_frame_is_skipped_others_still_read(self, artifact_store):
        """A detached/mid-navigation frame (real Playwright can raise
        reading .content() on one) must not lose the OTHER real
        frames' content, and must never crash collection."""
        homepage_html = "<html><body>No contact info in the main document.</body></html>"
        good_widget = '<html><body><a href="mailto:real@nvent.com">Email</a></body></html>'
        fake = _FakePlaywright(
            working_urls={"https://www.daroaxle.com"},
            html_by_url={"https://www.daroaxle.com": homepage_html},
            iframe_html_by_url={
                "https://www.daroaxle.com": [_FakeFrame("<html></html>", raises=True), good_widget],
            },
        )
        collector = SiteCollector(artifact_store=artifact_store, playwright_factory=lambda: fake)

        result = collector.collect(supplier_id=1, domain="daroaxle.com")

        assert result.success is True
        assert result.pages[0].mailto_emails == ["real@nvent.com"]

    def test_page_frames_itself_raising_never_crashes_collection(self, artifact_store):
        """A more severe failure than one bad frame -- reading
        page.frames itself fails (e.g. the page navigated away mid-
        read). Must degrade to "no iframe content", never abort the
        whole collection."""
        homepage_html = '<html><body><a href="mailto:info@daroaxle.com">Email</a></body></html>'
        fake = _FakePlaywright(
            working_urls={"https://www.daroaxle.com"},
            html_by_url={"https://www.daroaxle.com": homepage_html},
            frames_raise_for_urls={"https://www.daroaxle.com"},
        )
        collector = SiteCollector(artifact_store=artifact_store, playwright_factory=lambda: fake)

        result = collector.collect(supplier_id=1, domain="daroaxle.com")

        assert result.success is True
        # main document's own real contact info is still found -- only
        # the iframe-reading step was unavailable, not collection itself.
        assert result.pages[0].mailto_emails == ["info@daroaxle.com"]

    def test_iframe_content_never_leaks_into_image_or_social_link_extraction(self, artifact_store):
        """Deliberately scoped to contact extraction only (see
        _collect_iframe_html's own docstring for why) -- an iframe's
        own images/social links must not appear in the PARENT page's
        image_urls/social_links, since resolving a relative href from
        inside the iframe against the parent page's base URL would be
        actively wrong."""
        homepage_html = "<html><body>No images or social links here.</body></html>"
        widget_html = (
            '<html><body><img src="https://cdn.widget-vendor.com/logo.png">'
            '<a href="https://facebook.com/widgetvendor">Facebook</a></body></html>'
        )
        fake = _FakePlaywright(
            working_urls={"https://www.daroaxle.com"},
            html_by_url={"https://www.daroaxle.com": homepage_html},
            iframe_html_by_url={"https://www.daroaxle.com": [widget_html]},
        )
        collector = SiteCollector(artifact_store=artifact_store, playwright_factory=lambda: fake)

        result = collector.collect(supplier_id=1, domain="daroaxle.com")

        assert result.pages[0].image_urls == []
        assert result.pages[0].social_links == []

    def test_frame_count_beyond_the_cap_is_never_read(self, artifact_store):
        """A pathological page (ad-heavy, many embedded widgets)
        shouldn't be able to blow up the time budget just fetching
        frame content -- verified by making the one frame PAST the cap
        raise if its .content() is ever called at all."""
        homepage_html = "<html><body>No contact info in the main document.</body></html>"
        within_cap = [
            f'<html><body><a href="mailto:frame{i}@daroaxle.com">Email</a></body></html>'
            for i in range(_MAX_IFRAMES_PER_PAGE)
        ]
        beyond_cap = _FakeFrame("<html></html>", raises=True)
        fake = _FakePlaywright(
            working_urls={"https://www.daroaxle.com"},
            html_by_url={"https://www.daroaxle.com": homepage_html},
            iframe_html_by_url={"https://www.daroaxle.com": within_cap + [beyond_cap]},
        )
        collector = SiteCollector(artifact_store=artifact_store, playwright_factory=lambda: fake)

        result = collector.collect(supplier_id=1, domain="daroaxle.com")

        assert result.success is True  # the raising frame past the cap never gets read, so never crashes
        assert len(result.pages[0].mailto_emails) == _MAX_IFRAMES_PER_PAGE


class TestWaitUntilDomContentLoaded:
    """"load" (the Playwright default) waits for every image/tracker on
    the page to finish -- the wrong default for image-heavy factory
    sites, which is why _visit_and_collect passes
    wait_until="domcontentloaded" explicitly. See site_collector.py's
    own comment for the calibration-run failure this was fixing."""

    @pytest.mark.slow
    def test_every_navigation_uses_domcontentloaded_not_load(self, local_site, artifact_store):
        collector = SiteCollector(artifact_store=artifact_store, max_pages=6)
        result = collector.collect(supplier_id=1, domain=local_site)

        assert result.success is True
        # homepage plus at least one followed link -- confirms this
        # isn't just true for the first navigation
        assert len(result.pages) > 1

    def test_domcontentloaded_is_passed_on_every_goto_call(self, artifact_store):
        fake = _FakePlaywright(working_urls={"https://www.daroaxle.com"})
        collector = SiteCollector(artifact_store=artifact_store, playwright_factory=lambda: fake)
        collector.collect(supplier_id=1, domain="daroaxle.com")

        assert fake.page.goto_wait_until_values
        assert all(v == "domcontentloaded" for v in fake.page.goto_wait_until_values)
