"""
tests/test_own_website_scraper.py

Tests for scrapers.own_website_scraper.OwnWebsiteScraper. Uses a fake
injected client (no network), following the exact pattern
tests/test_geographic_expansion.py already established for
GlobalDirectoryScraper.
"""

from __future__ import annotations

import httpx

from scrapers.own_website_scraper import OwnWebsiteScraper


class FakeResponse:
    def __init__(self, text, status_code=200, content_type="text/html"):
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class FakeOwnWebsiteClient:
    def __init__(self, pages_by_url):
        self._pages = pages_by_url
        self.requested_urls = []

    def get(self, url):
        self.requested_urls.append(url)
        if url in self._pages:
            return self._pages[url]
        return FakeResponse("<html><body>not found</body></html>", status_code=404)


class FailingClient:
    def get(self, url):
        raise httpx.ConnectError("connection refused")


HOMEPAGE_WITH_LINKS = """
<html><body>
<nav>
  <a href="/about-us">About Us</a>
  <a href="/products">Products</a>
  <a href="/capabilities">Our Capabilities</a>
  <a href="https://other-domain.com/about">Off-domain about link</a>
</nav>
<p>Welcome to Acme Trailer Parts.</p>
</body></html>
"""


class TestFetch:

    def test_fetches_homepage_and_capability_linked_pages(self):
        client = FakeOwnWebsiteClient({
            "https://acme.example.com": FakeResponse(HOMEPAGE_WITH_LINKS),
            "https://acme.example.com/about-us": FakeResponse("<html><body>About Acme.</body></html>"),
            "https://acme.example.com/capabilities": FakeResponse("<html><body>We do injection moulding.</body></html>"),
        })
        scraper = OwnWebsiteScraper(http_client=client, enable_delays=False)

        result = scraper.fetch("acme.example.com")

        assert result.success is True
        urls = {p.url for p in result.pages}
        assert "https://acme.example.com" in urls
        assert "https://acme.example.com/about-us" in urls
        assert "https://acme.example.com/capabilities" in urls
        # /products has no capability-relevant keyword and must not be fetched
        assert "https://acme.example.com/products" not in urls

    def test_never_follows_off_domain_links(self):
        client = FakeOwnWebsiteClient({"https://acme.example.com": FakeResponse(HOMEPAGE_WITH_LINKS)})
        scraper = OwnWebsiteScraper(http_client=client, enable_delays=False)
        scraper.fetch("acme.example.com")
        assert not any("other-domain.com" in url for url in client.requested_urls)

    def test_never_follows_a_mailto_contact_link(self):
        """Real production bug this guards against: a genuine mailto:
        href (no netloc, so the off-domain check alone doesn't catch
        it) was being resolved and attempted as if it were a fetchable
        page, producing a URL-parsing crash from httpx."""
        html = """
        <html><body>
        <a href="mailto:contact@acme.example.com">Contact Us</a>
        <a href="/about-us">About Us</a>
        </body></html>
        """
        client = FakeOwnWebsiteClient({
            "https://acme.example.com": FakeResponse(html),
            "https://acme.example.com/about-us": FakeResponse("<html><body>About Acme.</body></html>"),
        })
        scraper = OwnWebsiteScraper(http_client=client, enable_delays=False)
        scraper.fetch("acme.example.com")
        assert not any("mailto" in url for url in client.requested_urls)

    def test_never_follows_a_tel_contact_link(self):
        html = '<html><body><a href="tel:+15551234567">Call us</a></body></html>'
        client = FakeOwnWebsiteClient({"https://acme.example.com": FakeResponse(html)})
        scraper = OwnWebsiteScraper(http_client=client, enable_delays=False)
        scraper.fetch("acme.example.com")
        assert not any("tel:" in url for url in client.requested_urls)

    def test_bare_domain_is_prefixed_with_https(self):
        client = FakeOwnWebsiteClient({"https://acme.example.com": FakeResponse("<html><body>hi</body></html>")})
        scraper = OwnWebsiteScraper(http_client=client, enable_delays=False)
        result = scraper.fetch("acme.example.com")
        assert result.success is True
        assert result.pages[0].url == "https://acme.example.com"

    def test_full_url_is_used_as_is(self):
        client = FakeOwnWebsiteClient({"http://acme.example.com": FakeResponse("<html><body>hi</body></html>")})
        scraper = OwnWebsiteScraper(http_client=client, enable_delays=False)
        result = scraper.fetch("http://acme.example.com")
        assert result.success is True

    def test_max_pages_caps_total_fetches(self):
        many_links = "<html><body>" + "".join(
            f'<a href="/capability-{i}">Capability {i}</a>' for i in range(10)
        ) + "</body></html>"
        pages = {"https://acme.example.com": FakeResponse(many_links)}
        for i in range(10):
            pages[f"https://acme.example.com/capability-{i}"] = FakeResponse(f"<html><body>Page {i}</body></html>")
        client = FakeOwnWebsiteClient(pages)
        scraper = OwnWebsiteScraper(http_client=client, enable_delays=False, max_pages=3)

        result = scraper.fetch("acme.example.com")
        assert len(result.pages) == 3

    def test_html_is_converted_to_readable_text(self):
        html = (
            "<html><head><style>p{color:red}</style></head>"
            "<body><script>x=1</script><p>Hello &amp; welcome</p></body></html>"
        )
        client = FakeOwnWebsiteClient({"https://acme.example.com": FakeResponse(html)})
        scraper = OwnWebsiteScraper(http_client=client, enable_delays=False)
        result = scraper.fetch("acme.example.com")
        text = result.pages[0].text
        assert "Hello & welcome" in text
        assert "color:red" not in text
        assert "x=1" not in text

    def test_empty_domain_fails_cleanly(self):
        scraper = OwnWebsiteScraper(http_client=FakeOwnWebsiteClient({}), enable_delays=False)
        result = scraper.fetch("")
        assert result.success is False
        assert result.pages == []

    def test_homepage_fetch_failure_is_reported_not_raised(self):
        scraper = OwnWebsiteScraper(http_client=FailingClient(), enable_delays=False)
        result = scraper.fetch("dead-domain.example.com")
        assert result.success is False
        assert result.error is not None

    def test_non_html_content_type_is_skipped(self):
        client = FakeOwnWebsiteClient({
            "https://acme.example.com": FakeResponse("%PDF-1.4 binary", content_type="application/pdf"),
        })
        scraper = OwnWebsiteScraper(http_client=client, enable_delays=False)
        result = scraper.fetch("acme.example.com")
        assert result.success is False

    def test_sub_page_fetch_failure_does_not_abort_the_whole_fetch(self):
        """One broken link on the homepage must not lose the pages that
        did work."""
        client = FakeOwnWebsiteClient({
            "https://acme.example.com": FakeResponse(HOMEPAGE_WITH_LINKS),
            "https://acme.example.com/about-us": FakeResponse("<html><body>About.</body></html>"),
            # /capabilities deliberately absent -> 404 in FakeOwnWebsiteClient
        })
        scraper = OwnWebsiteScraper(http_client=client, enable_delays=False)
        result = scraper.fetch("acme.example.com")
        assert result.success is True
        assert len(result.pages) == 2  # homepage + about-us; capabilities silently skipped


class TestImageCollection:

    def test_collects_absolute_image_urls_from_the_page(self):
        html = '<html><body><img src="/photos/factory-floor.jpg"><img src="https://cdn.example.com/prod.jpg"></body></html>'
        client = FakeOwnWebsiteClient({"https://acme.example.com": FakeResponse(html)})
        scraper = OwnWebsiteScraper(http_client=client, enable_delays=False)
        result = scraper.fetch("acme.example.com")
        assert result.pages[0].image_urls == [
            "https://acme.example.com/photos/factory-floor.jpg",
            "https://cdn.example.com/prod.jpg",
        ]

    def test_off_domain_cdn_images_are_kept_unlike_off_domain_links(self):
        """Deliberately different from link-following: photos are
        commonly CDN-hosted and must not be excluded the way
        off-domain navigation links are."""
        html = '<html><body><img src="https://images.cdn-provider.com/photo.jpg"></body></html>'
        client = FakeOwnWebsiteClient({"https://acme.example.com": FakeResponse(html)})
        scraper = OwnWebsiteScraper(http_client=client, enable_delays=False)
        result = scraper.fetch("acme.example.com")
        assert result.pages[0].image_urls == ["https://images.cdn-provider.com/photo.jpg"]

    def test_logos_and_icons_are_excluded(self):
        html = (
            '<html><body>'
            '<img src="/logo.png"><img src="/favicon.ico"><img src="/icons/menu-icon.svg">'
            '<img src="/photos/workshop.jpg">'
            '</body></html>'
        )
        client = FakeOwnWebsiteClient({"https://acme.example.com": FakeResponse(html)})
        scraper = OwnWebsiteScraper(http_client=client, enable_delays=False)
        result = scraper.fetch("acme.example.com")
        assert result.pages[0].image_urls == ["https://acme.example.com/photos/workshop.jpg"]

    def test_image_count_is_capped(self):
        html = "<html><body>" + "".join(f'<img src="/photo-{i}.jpg">' for i in range(20)) + "</body></html>"
        client = FakeOwnWebsiteClient({"https://acme.example.com": FakeResponse(html)})
        scraper = OwnWebsiteScraper(http_client=client, enable_delays=False)
        result = scraper.fetch("acme.example.com")
        assert len(result.pages[0].image_urls) == 5

    def test_page_with_no_images_has_an_empty_list_not_none(self):
        client = FakeOwnWebsiteClient({"https://acme.example.com": FakeResponse("<html><body>text only</body></html>")})
        scraper = OwnWebsiteScraper(http_client=client, enable_delays=False)
        result = scraper.fetch("acme.example.com")
        assert result.pages[0].image_urls == []

    def test_duplicate_image_urls_are_deduplicated(self):
        html = '<html><body><img src="/photo.jpg"><img src="/photo.jpg"></body></html>'
        client = FakeOwnWebsiteClient({"https://acme.example.com": FakeResponse(html)})
        scraper = OwnWebsiteScraper(http_client=client, enable_delays=False)
        result = scraper.fetch("acme.example.com")
        assert result.pages[0].image_urls == ["https://acme.example.com/photo.jpg"]


class TestContactPageIsFollowed:

    def test_a_contact_link_is_now_followed(self):
        """Previously "contact" wasn't in the capability-link keyword
        list at all -- a dedicated Contact Us page could be sitting
        right there and the scraper would never visit it."""
        html = '<html><body><a href="/contact-us">Contact Us</a></body></html>'
        client = FakeOwnWebsiteClient({
            "https://acme.example.com": FakeResponse(html),
            "https://acme.example.com/contact-us": FakeResponse("<html><body>Email us</body></html>"),
        })
        scraper = OwnWebsiteScraper(http_client=client, enable_delays=False)
        result = scraper.fetch("acme.example.com")
        urls = {p.url for p in result.pages}
        assert "https://acme.example.com/contact-us" in urls


class TestContactFormDetection:

    def test_form_with_email_field_is_detected(self):
        html = '<html><body><form><input type="email" name="email"><input type="submit"></form></body></html>'
        client = FakeOwnWebsiteClient({"https://acme.example.com": FakeResponse(html)})
        scraper = OwnWebsiteScraper(http_client=client, enable_delays=False)
        result = scraper.fetch("acme.example.com")
        assert result.pages[0].has_contact_form is True

    def test_form_with_message_textarea_is_detected(self):
        html = '<html><body><form><input name="name"><textarea name="message"></textarea></form></body></html>'
        client = FakeOwnWebsiteClient({"https://acme.example.com": FakeResponse(html)})
        scraper = OwnWebsiteScraper(http_client=client, enable_delays=False)
        result = scraper.fetch("acme.example.com")
        assert result.pages[0].has_contact_form is True

    def test_page_with_no_form_is_false(self):
        client = FakeOwnWebsiteClient({"https://acme.example.com": FakeResponse("<html><body>text only</body></html>")})
        scraper = OwnWebsiteScraper(http_client=client, enable_delays=False)
        result = scraper.fetch("acme.example.com")
        assert result.pages[0].has_contact_form is False

    def test_unrelated_form_like_a_search_box_is_not_detected(self):
        html = '<html><body><form><input type="text" name="q" placeholder="Search..."></form></body></html>'
        client = FakeOwnWebsiteClient({"https://acme.example.com": FakeResponse(html)})
        scraper = OwnWebsiteScraper(http_client=client, enable_delays=False)
        result = scraper.fetch("acme.example.com")
        assert result.pages[0].has_contact_form is False
