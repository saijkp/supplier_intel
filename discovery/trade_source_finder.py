"""
discovery/trade_source_finder.py

Phase 0.5 of discovery.discovery_service.DiscoveryService.discover_to_target()
-- opt-in (check_trade_source=True), runs after Phase 0's free database
check and before Round 1's raw candidate search, never replacing it.
One real SerpAPI search for "<product> trade association UK", then a
cheap single-page fetch (no crawl -- this is a lightweight pre-check,
not a real per-supplier collection) on each of the top few result
URLs, checking whether the page is real, readable content rather than
a parking page or a bot/JS-challenge wall.

The first candidate that passes is surfaced via a DiscoveryProgressEvent
(status="trade_source_found") -- nothing is scraped for member names
and nothing is auto-imported. "This page is real and fetchable" is a
much weaker claim than "this page lists genuine category-relevant
manufacturers" -- that judgment still needs a real per-member pass,
same as every trade-body check done by hand this session (BCGA/Liquid
Gas UK/PVMF all needed a full manual name-by-name read after being
found exactly this way). Expect this to find something usable well
under half the time in practice -- several real trade/exhibition sites
checked this same session (Chinaplas, NPE, SPI, PMA) were bot-walled
outright.

Reuses verification.website_contact_extractor.parking_page_reason for
the parking-page half of the fetchability check (same signature-based
approach, not duplicated) and adds a second, JS/bot-challenge-specific
signature list for a failure mode that check was never built for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import httpx

from scrapers.google_search_scraper import GoogleSearchScraper
from scrapers.own_website_scraper import html_to_text
from verification.website_contact_extractor import parking_page_reason

logger = logging.getLogger(__name__)

# How many of the search's top results to try before giving up --
# small and fixed, matching the "lightweight" cost promise: at most
# this many single-page fetches, never a crawl.
_MAX_RESULTS_TO_TRY = 3

# A real page's stripped text needs at least this many characters to
# count as "readable content" rather than a near-empty JS-rendered
# shell -- a bot-challenge page's own text (e.g. Clesse UK's "Just a
# moment... Enable JavaScript and cookies to continue") is typically
# well under this, so this floor also catches challenge pages whose
# exact wording isn't in _BOT_WALL_TEXT_SIGNATURES below.
_MIN_TEXT_LENGTH = 200

# Bot-challenge/JS-wall page signatures -- a DIFFERENT failure mode
# from parking_page_reason's server-default/for-sale signatures, found
# live this session: Clesse UK ("Just a moment... Enable JavaScript
# and cookies to continue"), Chinaplas (Alibaba Cloud's ESA challenge
# page), NPE/SPI/PMA (Cloudflare "Attention Required" / flat 403).
_BOT_WALL_TEXT_SIGNATURES: tuple = (
    "just a moment",
    "enable javascript and cookies",
    "attention required",
    "checking your browser",
    "ddos protection by",
    "please verify you are a human",
    "cf-browser-verification",
    "captcha",
)


@dataclass
class TradeSourceCandidate:
    domain: str
    title: str
    snippet: str


def _bot_wall_reason(page_text: str) -> Optional[str]:
    """None if `page_text` doesn't look like a bot-challenge/JS-wall
    page; otherwise a human-readable reason it does. Same signature-
    only, no-length-floor shape as parking_page_reason -- the length
    floor lives in the caller (_MIN_TEXT_LENGTH), not here, so this
    function stays a pure text check, testable on its own."""
    haystack = (page_text or "").lower()
    for signature in _BOT_WALL_TEXT_SIGNATURES:
        if signature in haystack:
            return f"page text matches a known bot-challenge/JS-wall signature ('{signature}')"
    return None


def find_candidate_trade_source(
    product: str,
    google_scraper: Optional[GoogleSearchScraper] = None,
    http_client: Optional[httpx.Client] = None,
) -> Optional[TradeSourceCandidate]:
    """One real search, up to _MAX_RESULTS_TO_TRY single-page fetches
    (never a crawl) -- returns the first result whose page is real,
    readable content, or None if the search itself failed/returned
    nothing, or every candidate URL failed the fetchability check.

    Never raises -- a failure here must never block discover_to_target()
    from proceeding to Round 1; every real failure mode (search error,
    fetch error, non-200, parking page, bot wall, too-short text) is
    handled by moving on to the next candidate or returning None."""
    scraper = google_scraper or GoogleSearchScraper()
    query = f"{product} trade association UK"
    try:
        results = scraper.scrape(query, max_results=_MAX_RESULTS_TO_TRY)
    except Exception as e:  # noqa: BLE001 -- a search failure must never block discover_to_target()
        logger.warning("trade_source_finder: search failed for %r: %s", query, e)
        return None

    owns_client = http_client is None
    client = http_client or httpx.Client(follow_redirects=True, timeout=10.0)
    try:
        for result in results[:_MAX_RESULTS_TO_TRY]:
            if not result.success:
                continue
            link = (result.raw_data or {}).get("link")
            if not link:
                continue
            try:
                response = client.get(link, headers={"User-Agent": "Mozilla/5.0"})
            except Exception as e:  # noqa: BLE001 -- one candidate URL failing must never abort the check
                logger.info("trade_source_finder: fetch failed for %s: %s", link, e)
                continue
            if response.status_code != 200:
                continue
            text = html_to_text(response.text)
            if len(text) < _MIN_TEXT_LENGTH:
                continue
            if parking_page_reason(text):
                continue
            if _bot_wall_reason(text):
                continue

            domain = urlparse(str(response.url)).netloc
            return TradeSourceCandidate(
                domain=domain,
                title=(result.raw_data or {}).get("title") or domain,
                snippet=(result.raw_data or {}).get("snippet") or "",
            )
    finally:
        if owns_client:
            client.close()

    return None
