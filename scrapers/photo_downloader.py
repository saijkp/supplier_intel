"""
scrapers/photo_downloader.py

Downloads image bytes for URLs `OwnWebsiteScraper` collected, in the
exact shape `verification.factory_photo_verifier.FactoryPhotoVerifier
.assess_photos()` expects (`{'image_bytes': bytes, 'media_type': str}`).
This is the one piece factory_photo_verifier's own module docstring
named as missing since it was added: "No scraper in this codebase
downloads photos yet... wiring 'fetch the photo, then call this' into
the scraping stage is the natural next increment." This module is
that increment.

Before downloading and re-processing any platform's photos, check that
platform's terms of service -- carried over verbatim from
factory_photo_verifier's own disclosure, since it applies identically
here: this doesn't redistribute images, only reads bytes to pass to a
vision model, but you're still the one making the download.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, List, Optional

import httpx

logger = logging.getLogger(__name__)

_ALLOWED_MEDIA_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")
_MAX_BYTES = 10 * 1024 * 1024  # 10MB -- generous for a web photo, protects against a runaway download


@dataclasses.dataclass
class DownloadedPhoto:
    url: str
    image_bytes: Optional[bytes]
    media_type: Optional[str]
    success: bool
    error: Optional[str] = None

    def as_assessment_input(self) -> dict:
        """The exact shape `FactoryPhotoVerifier.assess_photos` expects."""
        return {"image_bytes": self.image_bytes, "media_type": self.media_type}


class PhotoDownloader:
    """`http_client` is injectable, matching every other scraper in
    this codebase's testability convention -- a real `httpx.Client` is
    built lazily if none is given.
    """

    def __init__(self, http_client: Optional[Any] = None, timeout: float = 15.0):
        self._client = http_client or httpx.Client(timeout=timeout, follow_redirects=True)

    def download(self, url: str) -> DownloadedPhoto:
        try:
            response = self._client.get(url)
            response.raise_for_status()
        except Exception as e:
            logger.warning("photo_downloader: failed to fetch %s: %s", url, e)
            return DownloadedPhoto(url=url, image_bytes=None, media_type=None, success=False, error=str(e))

        content_type = getattr(response, "headers", {}).get("content-type", "").split(";")[0].strip()
        if content_type not in _ALLOWED_MEDIA_TYPES:
            return DownloadedPhoto(
                url=url, image_bytes=None, media_type=None, success=False,
                error=f"not a supported image type: {content_type or 'unknown'}",
            )

        content = response.content
        if len(content) > _MAX_BYTES:
            return DownloadedPhoto(
                url=url, image_bytes=None, media_type=None, success=False,
                error=f"image exceeds {_MAX_BYTES} byte limit ({len(content)} bytes)",
            )
        if len(content) == 0:
            return DownloadedPhoto(url=url, image_bytes=None, media_type=None, success=False, error="empty response")

        return DownloadedPhoto(url=url, image_bytes=content, media_type=content_type, success=True)

    def download_all(self, urls: List[str]) -> List[DownloadedPhoto]:
        return [self.download(url) for url in urls]
