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
    footer_text: str = ""  # text of the page's <footer> element, if any -- see site_collector.py's _extract_footer_text
    facility_photo_urls: List[str] = field(default_factory=list)  # heuristic candidate factory/facility photos on this page -- see site_collector.py's _extract_facility_photo_urls; never a verdict, just a manual-review candidate list
    mailto_emails: List[str] = field(default_factory=list)  # raw mailto: href values found on this page -- see site_collector.py's _find_mailto_emails; verification.website_contact_extractor.extract_contact_details reads this via getattr, so OwnWebsitePage (no such field) is unaffected
    tel_phones: List[str] = field(default_factory=list)  # raw tel: href values found on this page -- see site_collector.py's _find_tel_phones; same getattr-based consumption as mailto_emails above


@dataclass
class CertificateDocument:
    """A downloaded certificate/quality-standard document (PDF/doc)
    found among a page's download_links -- see site_collector.py's
    certificate-keyword matching. `artifact_path` is relative to
    CollectionResult.artifacts_dir, same convention as
    CollectedPage.screenshot_path/html_path."""
    url: str
    matched_keyword: str
    filename: str
    artifact_path: str


@dataclass
class CollectionResult:
    domain: str
    pages: List[CollectedPage] = field(default_factory=list)
    success: bool = True
    error: Optional[str] = None
    artifacts_dir: Optional[str] = None  # path relative to config.settings.COLLECTION_ARTIFACTS_DIR
    proxy_provider: Optional[str] = None
    certificate_documents: List[CertificateDocument] = field(default_factory=list)
    # Which candidate base URL actually loaded -- see site_collector.py's
    # _build_candidate_urls (many hosts only resolve on www, or only on
    # http, so the homepage fetch tries several variants in order).
    # None when success is False (nothing loaded) or when `domain` was
    # already a full URL (single-candidate path, nothing to disambiguate).
    resolved_url: Optional[str] = None
