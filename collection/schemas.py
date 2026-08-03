"""
collection/schemas.py

Output shapes for collection/site_collector.py. Deliberately duck-type
compatible with scrapers.own_website_scraper's OwnWebsitePage/
OwnWebsiteFetchResult (same `.url`/`.text`/`.image_urls`/
`.has_contact_form` fields) plus new fields Collection Service adds
(`.screenshot_path`, `.social_links`, `.download_links`) -- this is
what lets verification.capability_extractor.CapabilityExtractor.
extract_from_pages() and verification.website_contact_extractor.
extract_contact_details() accept output from EITHER engine unchanged.
See collection/site_collector.py's own module docstring for why
SiteCollector is an additive alternative, not a replacement, for
OwnWebsiteScraper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class CollectedPage:
    url: str
    text: str
    image_urls: List[str] = field(default_factory=list)
    has_contact_form: bool = False
    screenshot_path: Optional[str] = None  # path relative to CollectionResult.artifacts_dir
    html_path: Optional[str] = None
    social_links: List[str] = field(default_factory=list)
    download_links: List[str] = field(default_factory=list)


@dataclass
class CollectionResult:
    domain: str
    pages: List[CollectedPage] = field(default_factory=list)
    success: bool = True
    error: Optional[str] = None
    artifacts_dir: Optional[str] = None  # path relative to config.settings.COLLECTION_ARTIFACTS_DIR
    proxy_provider: Optional[str] = None
