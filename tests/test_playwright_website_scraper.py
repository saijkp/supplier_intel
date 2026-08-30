"""
tests/test_playwright_website_scraper.py

Tests for scrapers.playwright_website_scraper.PlaywrightWebsiteScraper
-- the real-browser fallback fetcher. Uses an injected fake Playwright
factory (same seam/style as tests/test_site_collector.py's own
_FakePlaywright) so these tests never launch a real browser.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from scrapers.playwright_website_scraper import PlaywrightWebsiteScraper


class FakeResponse:
    def __init__(self, status: int):
        self.status = status


class FakePage:
    def __init__(
        self,
        pages_by_url: Dict[str, Tuple[Optional[int], str]],
        redirects: Optional[Dict[str, str]] = None,
        raise_for: Optional[set] = None,
    ):
        # url -> (http_status_or_None, html). status=None simulates a
        # real Playwright navigation whose response object doesn't
        # exist (matches _visit()'s own "response is not None" guard).
        self._pages_by_url = pages_by_url
        self._redirects = redirects or {}
        self._raise_for = raise_for or set()
        self._current_url = None
        self._html = ""
        self.goto_calls: List[str] = []

    def goto(self, url, wait_until=None):
        self.goto_calls.append(url)
        if url in self._raise_for:
            raise RuntimeError(f"net::ERR_NAME_NOT_RESOLVED for {url}")
        if url not in self._pages_by_url:
            raise RuntimeError(f"net::ERR_NAME_NOT_RESOLVED for {url}")
        self._current_url = self._redirects.get(url, url)
        status, html = self._pages_by_url[url]
        self._html = html
        return FakeResponse(status) if status is not None else None

    @property
    def url(self):
        return self._current_url

    def content(self):
        return self._html


class FakeContext:
    def __init__(self, page: FakePage):
        self._page = page
        self.new_context_kwargs: List[Dict[str, Any]] = []

    def new_page(self):
        return self._page

    def set_default_timeout(self, ms):
        pass


class FakeBrowser:
    def __init__(self, context: FakeContext):
        self._context = context
        self.closed = False
        self.new_context_calls: List[Dict[str, Any]] = []

    def new_context(self, **kwargs):
        self.new_context_calls.append(kwargs)
        return self._context

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, browser: FakeBrowser):
        self._browser = browser

    def launch(self, **kwargs):
        return self._browser


class FakePlaywright:
    """Injectable via PlaywrightWebsiteScraper(playwright_factory=...)."""

    def __init__(
        self,
        pages_by_url: Dict[str, Tuple[Optional[int], str]],
        redirects: Optional[Dict[str, str]] = None,
        raise_for: Optional[set] = None,
    ):
        self.page = FakePage(pages_by_url, redirects=redirects, raise_for=raise_for)
        self.context = FakeContext(self.page)
        self.browser = FakeBrowser(self.context)
        self.chromium = FakeChromium(self.browser)


HOMEPAGE_WITH_LINKS = """
<html><body>
<nav>
  <a href="/about-us">About Us</a>
  <a href="/products">Products</a>
  <a href="/contact">Contact Us</a>
</nav>
<p>Welcome to Acme Trailer Parts.</p>
</body></html>
"""


class TestFetch:

    def test_no_domain_returns_error_without_launching_a_browser(self):
        scraper = PlaywrightWebsiteScraper(playwright_factory=lambda: FakePlaywright({}))
        result = scraper.fetch("")
        assert result.success is False
        assert "no domain" in result.error

    def test_fetches_homepage_only_when_no_capability_links_present(self):
        fake = FakePlaywright({"https://acme.example.com": (200, "<html><body>Plain homepage.</body></html>")})
        scraper = PlaywrightWebsiteScraper(playwright_factory=lambda: fake)

        result = scraper.fetch("acme.example.com")

        assert result.success is True
        assert len(result.pages) == 1
        assert result.pages[0].url == "https://acme.example.com"
        assert "Plain homepage." in result.pages[0].text

    def test_fetches_homepage_and_capability_linked_pages(self):
        fake = FakePlaywright({
            "https://acme.example.com": (200, HOMEPAGE_WITH_LINKS),
            "https://acme.example.com/about-us": (200, "<html><body>About Acme.</body></html>"),
            "https://acme.example.com/contact": (200, "<html><body>Contact: 1 Main St.</body></html>"),
        })
        scraper = PlaywrightWebsiteScraper(playwright_factory=lambda: fake)

        result = scraper.fetch("acme.example.com")

        urls = {p.url for p in result.pages}
        assert "https://acme.example.com" in urls
        assert "https://acme.example.com/about-us" in urls
        assert "https://acme.example.com/contact" in urls
        # /products has no capability-relevant keyword and must not be fetched
        assert "https://acme.example.com/products" not in urls

    def test_homepage_navigation_failure_returns_unsuccessful(self):
        fake = FakePlaywright({}, raise_for={"https://acme.example.com"})
        scraper = PlaywrightWebsiteScraper(playwright_factory=lambda: fake)

        result = scraper.fetch("acme.example.com")

        assert result.success is False
        assert "could not load homepage" in result.error

    def test_homepage_http_error_status_is_treated_as_a_failure(self):
        fake = FakePlaywright({"https://acme.example.com": (404, "<html><body>Not found</body></html>")})
        scraper = PlaywrightWebsiteScraper(playwright_factory=lambda: fake)

        result = scraper.fetch("acme.example.com")

        assert result.success is False

    def test_sub_page_failure_does_not_abort_the_whole_fetch(self):
        fake = FakePlaywright({
            "https://acme.example.com": (200, HOMEPAGE_WITH_LINKS),
            "https://acme.example.com/contact": (200, "<html><body>Contact us.</body></html>"),
            # /about-us deliberately absent -- goto() raises for it
        })
        scraper = PlaywrightWebsiteScraper(playwright_factory=lambda: fake)

        result = scraper.fetch("acme.example.com")

        assert result.success is True
        urls = {p.url for p in result.pages}
        assert "https://acme.example.com/contact" in urls
        assert "https://acme.example.com/about-us" not in urls

    def test_max_pages_caps_total_fetches(self):
        html = """
        <html><body>
        <a href="/about-us">About</a>
        <a href="/contact">Contact</a>
        <a href="/quality">Quality</a>
        </body></html>
        """
        fake = FakePlaywright({
            "https://acme.example.com": (200, html),
            "https://acme.example.com/about-us": (200, "<html><body>About.</body></html>"),
            "https://acme.example.com/contact": (200, "<html><body>Contact.</body></html>"),
            "https://acme.example.com/quality": (200, "<html><body>Quality.</body></html>"),
        })
        scraper = PlaywrightWebsiteScraper(max_pages=2, playwright_factory=lambda: fake)

        result = scraper.fetch("acme.example.com")

        assert len(result.pages) == 2

    def test_final_url_reflects_a_real_redirect(self):
        fake = FakePlaywright(
            {"https://acme.example.com": (200, "<html><body>Homepage.</body></html>")},
            redirects={"https://acme.example.com": "https://www.acme.example.com/"},
        )
        scraper = PlaywrightWebsiteScraper(playwright_factory=lambda: fake)

        result = scraper.fetch("acme.example.com")

        assert result.pages[0].final_url == "https://www.acme.example.com/"

    def test_browser_is_always_closed_even_on_a_mid_fetch_exception(self):
        class ExplodingContext(FakeContext):
            def new_page(self):
                raise RuntimeError("boom")

        fake = FakePlaywright({"https://acme.example.com": (200, "<html><body>ok</body></html>")})
        fake.context = ExplodingContext(fake.page)
        fake.browser._context = fake.context
        scraper = PlaywrightWebsiteScraper(playwright_factory=lambda: fake)

        result = scraper.fetch("acme.example.com")

        assert result.success is False
        assert fake.browser.closed is True

    def test_unexpected_error_is_caught_never_raised(self):
        class ExplodingChromium:
            def launch(self, **kwargs):
                raise RuntimeError("chromium unavailable")

        class ExplodingPlaywright:
            chromium = ExplodingChromium()

        scraper = PlaywrightWebsiteScraper(playwright_factory=lambda: ExplodingPlaywright())

        result = scraper.fetch("acme.example.com")  # must not raise

        assert result.success is False
        assert "chromium unavailable" in result.error
