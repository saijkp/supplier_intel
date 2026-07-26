"""
tests/test_photo_downloader.py

Tests for scrapers.photo_downloader.PhotoDownloader. Uses a fake
injected client, matching the same pattern
tests/test_own_website_scraper.py already established.
"""

from __future__ import annotations

import httpx

from scrapers.photo_downloader import PhotoDownloader


class FakeResponse:
    def __init__(self, content, status_code=200, content_type="image/jpeg"):
        self.content = content
        self.status_code = status_code
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class FakeClient:
    def __init__(self, responses_by_url):
        self._responses = responses_by_url

    def get(self, url):
        if url in self._responses:
            return self._responses[url]
        return FakeResponse(b"", status_code=404)


class FailingClient:
    def get(self, url):
        raise httpx.ConnectError("connection refused")


class TestDownload:

    def test_successful_download_returns_bytes_and_media_type(self):
        client = FakeClient({"https://acme.example.com/factory.jpg": FakeResponse(b"fake-jpeg-bytes")})
        downloader = PhotoDownloader(http_client=client)
        result = downloader.download("https://acme.example.com/factory.jpg")
        assert result.success is True
        assert result.image_bytes == b"fake-jpeg-bytes"
        assert result.media_type == "image/jpeg"

    def test_as_assessment_input_matches_factory_photo_verifier_shape(self):
        client = FakeClient({"https://acme.example.com/factory.jpg": FakeResponse(b"bytes")})
        downloader = PhotoDownloader(http_client=client)
        result = downloader.download("https://acme.example.com/factory.jpg")
        assert result.as_assessment_input() == {"image_bytes": b"bytes", "media_type": "image/jpeg"}

    def test_non_image_content_type_is_rejected(self):
        client = FakeClient({
            "https://acme.example.com/page.html": FakeResponse(b"<html></html>", content_type="text/html"),
        })
        downloader = PhotoDownloader(http_client=client)
        result = downloader.download("https://acme.example.com/page.html")
        assert result.success is False
        assert "not a supported image type" in result.error

    def test_oversized_image_is_rejected(self):
        big = b"x" * (10 * 1024 * 1024 + 1)
        client = FakeClient({"https://acme.example.com/huge.jpg": FakeResponse(big)})
        downloader = PhotoDownloader(http_client=client)
        result = downloader.download("https://acme.example.com/huge.jpg")
        assert result.success is False
        assert "exceeds" in result.error

    def test_empty_response_is_rejected(self):
        client = FakeClient({"https://acme.example.com/empty.jpg": FakeResponse(b"")})
        downloader = PhotoDownloader(http_client=client)
        result = downloader.download("https://acme.example.com/empty.jpg")
        assert result.success is False
        assert result.error == "empty response"

    def test_404_is_reported_not_raised(self):
        client = FakeClient({})
        downloader = PhotoDownloader(http_client=client)
        result = downloader.download("https://acme.example.com/missing.jpg")
        assert result.success is False

    def test_connection_failure_does_not_raise(self):
        downloader = PhotoDownloader(http_client=FailingClient())
        result = downloader.download("https://dead.example.com/photo.jpg")
        assert result.success is False
        assert result.error is not None

    def test_supported_types_include_png_and_webp(self):
        client = FakeClient({
            "https://a.example.com/1.png": FakeResponse(b"png-bytes", content_type="image/png"),
            "https://a.example.com/2.webp": FakeResponse(b"webp-bytes", content_type="image/webp"),
        })
        downloader = PhotoDownloader(http_client=client)
        assert downloader.download("https://a.example.com/1.png").success is True
        assert downloader.download("https://a.example.com/2.webp").success is True


class TestDownloadAll:

    def test_downloads_every_url_preserving_order(self):
        client = FakeClient({
            "https://a.example.com/1.jpg": FakeResponse(b"one"),
            "https://a.example.com/2.jpg": FakeResponse(b"two"),
        })
        downloader = PhotoDownloader(http_client=client)
        results = downloader.download_all(["https://a.example.com/1.jpg", "https://a.example.com/2.jpg"])
        assert [r.image_bytes for r in results] == [b"one", b"two"]

    def test_one_failure_does_not_abort_the_batch(self):
        client = FakeClient({"https://a.example.com/good.jpg": FakeResponse(b"good")})
        downloader = PhotoDownloader(http_client=client)
        results = downloader.download_all(["https://a.example.com/bad.jpg", "https://a.example.com/good.jpg"])
        assert results[0].success is False
        assert results[1].success is True

    def test_empty_list_returns_empty(self):
        downloader = PhotoDownloader(http_client=FakeClient({}))
        assert downloader.download_all([]) == []
