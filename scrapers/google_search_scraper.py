"""
scrapers/google_search_scraper.py

Google Search via SerpAPI — a complementary discovery source to the
B2B-platform scrapers (Alibaba, HKTDC, etc.), and the foundation of the
photo-search capability. Two distinct capabilities:

1. scrape(query) — general web search for manufacturer websites. Finds
   suppliers who don't list on the big B2B platforms at all, and serves
   as an independent cross-check ("does this company show up anywhere
   else on the web, under its own domain, not just a platform
   storefront").
2. search_by_image_url(image_url) — reverse image search: given a photo
   of the exact product you need (e.g. a trailer rear combination lamp
   assembly), find visually similar listings across the web, not just
   text-matched ones.

Requires a SerpAPI account (config.settings.SERPAPI_KEY). Unlike the
HKTDC/Volza/directory scrapers in this codebase, SerpAPI's own API
(https://serpapi.com/search) is stable, versioned, and well-documented —
so this integration is written directly against real, confirmed
parameter names rather than a best-effort reconstruction. What IS
unverified is the exact shape of result snippets/titles for any given
query, since that depends on what Google actually returns, not on
SerpAPI's contract.

WHY SERPAPI AND NOT SCRAPING GOOGLE DIRECTLY: Google aggressively blocks
and rate-limits direct automated scraping, and prohibits it in their
Terms of Service. SerpAPI is a paid service that handles this legally
and reliably on your behalf.

REVERSE IMAGE SEARCH NEEDS A HOSTED URL, NOT A LOCAL FILE: SerpAPI's
Google Reverse Image / Google Lens engines take an `image_url` — a
publicly-reachable URL — not an uploaded file. A local photo (e.g. one
you took of a part) needs to be hosted somewhere public first (even a
temporary image host) before this can search on it. For matching a
*local* reference photo against scraped candidate photos without
hosting anything, use verification.product_matcher.ProductMatcher
instead (OpenAI vision, works directly on local bytes) — that's
usually the better fit for "is this the right product", while this
scraper is the better fit for "find me more sources of this exact
product across the whole web".
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from config.settings import SERPAPI_KEY
from scrapers.base_scraper import BaseScraper, ScraperResult

logger = logging.getLogger(__name__)

SERPAPI_BASE_URL = "https://serpapi.com/search"


class GoogleSearchScraper(BaseScraper):

    def __init__(self, api_key: Optional[str] = None, http_client: Optional[httpx.Client] = None, enable_delays: bool = True):
        super().__init__("google", enable_delays=enable_delays)
        self.api_key = api_key or SERPAPI_KEY
        self._client = http_client or httpx.Client(timeout=30)

    def scrape(self, query: str, max_results: int = 20, site_filter: Optional[str] = None, **kwargs) -> List[ScraperResult]:
        """General web search. `site_filter` restricts to one domain
        (e.g. site_filter='alibaba.com') the same way typing
        'site:alibaba.com' into Google does — useful for pulling a
        platform's own indexed pages when its own site search is worse
        than Google's index of it, or for excluding platforms entirely
        by searching without a filter to surface independent
        manufacturer websites instead."""
        if not self.api_key:
            return [self.error_result(
                "SERPAPI_KEY is not configured. Add it to your .env file, "
                "or pass api_key= to GoogleSearchScraper() for testing."
            )]

        search_query = f"site:{site_filter} {query}" if site_filter else query
        params = {
            "engine": "google",
            "q": search_query,
            "num": max_results,
            "api_key": self.api_key,
        }

        try:
            response = self._safe_request(self._client.get, SERPAPI_BASE_URL, params=params)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error("Google search failed for '%s': %s", search_query, e)
            return [self.error_result(str(e))]

        organic_results = data.get("organic_results", [])
        results: List[ScraperResult] = []
        for item in organic_results:
            title = (item.get("title") or "").strip()
            link = (item.get("link") or "").strip()
            if not title or not link:
                continue
            results.append(ScraperResult(
                source="google",
                source_id=link,
                raw_data={
                    "title": title,
                    "link": link,
                    "snippet": item.get("snippet", ""),
                    "displayed_link": item.get("displayed_link", ""),
                },
                success=True,
            ))

        logger.info("Google search: %d results for '%s'", len(results), search_query)
        return results

    def search_by_image_url(self, image_url: str, max_results: int = 20) -> List[ScraperResult]:
        """Reverse image search — see the module docstring for why this
        needs a hosted URL rather than a local file. Returns visually
        similar images/pages found across the web."""
        if not self.api_key:
            return [self.error_result(
                "SERPAPI_KEY is not configured. Add it to your .env file, "
                "or pass api_key= to GoogleSearchScraper() for testing."
            )]

        params = {
            "engine": "google_reverse_image",
            "image_url": image_url,
            "api_key": self.api_key,
        }

        try:
            response = self._safe_request(self._client.get, SERPAPI_BASE_URL, params=params)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error("Reverse image search failed for %s: %s", image_url, e)
            return [self.error_result(str(e))]

        image_results = data.get("image_results", [])[:max_results]
        results: List[ScraperResult] = []
        for item in image_results:
            title = (item.get("title") or "").strip()
            link = (item.get("link") or "").strip()
            if not title or not link:
                continue
            results.append(ScraperResult(
                source="google_reverse_image",
                source_id=link,
                raw_data={
                    "title": title,
                    "link": link,
                    "snippet": item.get("snippet", ""),
                    "source_image_url": image_url,
                },
                success=True,
            ))

        logger.info("Reverse image search: %d results for %s", len(results), image_url)
        return results
