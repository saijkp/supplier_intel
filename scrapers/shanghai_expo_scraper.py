"""
scrapers/shanghai_expo_scraper.py

Scraper for trade exhibition directories — originally built for
Shanghai-based auto parts / trailer component expos (CIAPE, Auto
Shanghai), now also covering Automechanika Frankfurt, the largest
international automotive/commercial-vehicle trade fair. The module name
still says "shanghai_expo" for historical reasons (it was the first
exhibition source built) but the architecture was never China-specific —
any exhibition directory fits the same config shape. These directories
are far less standardised than Alibaba/HKTDC: each exhibition typically
runs on its own site, and the URL and HTML structure can change
completely between yearly/biennial editions — sometimes an entire new
site for the new edition.

Per Phase 1 Gap 1, everything source-specific here — not just CSS
selectors, but the base search URL template too — lives in
EXHIBITION_SOURCES below. Adding a newly-discovered exhibition
directory, or fixing one whose site was redesigned for this year's
edition, should only ever require editing that one config dict, never
this file's scraping logic.
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import Any, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper, ScraperResult

logger = logging.getLogger(__name__)

# Each entry fully describes one exhibition directory: how to build a
# search URL for it (`{query}` and `{page}` are substituted in), and
# how to find exhibitor cards in its HTML. Add new exhibitions here.
EXHIBITION_SOURCES: Dict[str, Dict[str, Any]] = {
    "ciape": {
        "name": "China International Auto Parts Expo",
        "country_hint": "China",
        "search_url_template": "https://www.ciape.com.cn/en/exhibitor-search?keyword={query}&page={page}",
        "selectors": {
            "exhibitor_card": ".exhibitor-card, [data-testid='exhibitor']",
            "company_name": ".exhibitor-name, h4",
            "booth_number": ".booth-no",
            "country": ".exhibitor-country",
            "products": ".product-tag",
            "website": "a.exhibitor-website",
            "profile_link": "a.exhibitor-profile",
        },
    },
    "auto_shanghai": {
        "name": "Auto Shanghai",
        "country_hint": "China",
        "search_url_template": "https://www.autoshanghai.org.cn/en/exhibitors?q={query}&page={page}",
        "selectors": {
            "exhibitor_card": ".exhibitor-item",
            "company_name": ".company-name",
            "booth_number": ".booth",
            "country": ".country",
            "products": ".category-tag",
            "website": "a.website-link",
            "profile_link": "a.detail-link",
        },
    },
    "automechanika_frankfurt": {
        # The largest international trade fair for the automotive/
        # commercial-vehicle service industry, held biennially in
        # Frankfurt, Germany — a genuinely major sourcing venue that
        # spans far more than China-based exhibitors, including
        # European and Turkish trailer-component manufacturers. Added
        # on direct suggestion; URL/selectors below are an unverified
        # best guess at Messe Frankfurt's exhibitor-directory structure
        # (same caveat as every other config-driven scraper in this
        # codebase) — confirm against the live site before relying on it.
        "name": "Automechanika Frankfurt",
        "country_hint": "",  # international exhibitors — trust each card's own country text
        "search_url_template": (
            "https://automechanika-frankfurt.messefrankfurt.com/frankfurt/en/exhibitor-search.html"
            "?search={query}&page={page}"
        ),
        "selectors": {
            "exhibitor_card": ".exhibitor-result, [data-testid='exhibitor-result']",
            "company_name": ".exhibitor-title, h3",
            "booth_number": ".hall-stand, .booth-number",
            "country": ".exhibitor-country",
            "products": ".product-group-tag",
            "website": "a.exhibitor-website-link",
            "profile_link": "a.exhibitor-detail-link",
        },
    },
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}


class ShanghaiExpoScraper(BaseScraper):
    """
    `exhibition` selects which entry in EXHIBITION_SOURCES to scrape.
    Defaults to 'ciape' as the exhibition most directly relevant to
    trailer-component sourcing; pass a different key (or add a new
    entry to EXHIBITION_SOURCES) to cover others.
    """

    def __init__(
        self,
        exhibition: str = "ciape",
        http_client: Optional[httpx.Client] = None,
        enable_delays: bool = True,
    ):
        super().__init__("shanghai_expo", enable_delays=enable_delays)
        if exhibition not in EXHIBITION_SOURCES:
            raise ValueError(
                f"Unknown exhibition '{exhibition}'. Add it to EXHIBITION_SOURCES "
                f"or choose one of: {list(EXHIBITION_SOURCES)}"
            )
        self.exhibition = exhibition
        self.config = EXHIBITION_SOURCES[exhibition]
        self._client = http_client or httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True)

    def scrape(self, query: str, max_pages: int = 5, **kwargs) -> List[ScraperResult]:
        results: List[ScraperResult] = []
        template = self.config["search_url_template"]

        for page in range(1, max_pages + 1):
            url = template.format(query=urllib.parse.quote(query), page=page)
            try:
                response = self._safe_request(self._client.get, url)
                response.raise_for_status()
            except Exception as e:
                logger.error("%s page %d failed for '%s': %s", self.exhibition, page, query, e)
                if page == 1:
                    return [self.error_result(str(e))]
                break

            page_results = self._parse_exhibitors(response.text)
            if not page_results:
                logger.debug("%s: no more results after page %d for '%s'", self.exhibition, page - 1, query)
                break

            results.extend(page_results)
            self._polite_delay(3.0, 6.0)

        logger.info("%s: %d exhibitors for '%s'", self.exhibition, len(results), query)
        return results

    def _parse_exhibitors(self, html: str) -> List[ScraperResult]:
        selectors = self.config["selectors"]
        country_hint = self.config.get("country_hint", "")
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select(selectors["exhibitor_card"])
        results: List[ScraperResult] = []

        for card in cards:
            try:
                country = self._text(card, selectors["country"])
                data = {
                    "company_name": self._text(card, selectors["company_name"]),
                    "booth_number": self._text(card, selectors.get("booth_number", "")),
                    "country": country or country_hint,
                    "products": [
                        p.get_text(strip=True) for p in card.select(selectors["products"])
                    ],
                    "website": self._href(card, selectors["website"]),
                    "profile_url": self._href(card, selectors["profile_link"]),
                    "exhibition": self.exhibition,
                    "exhibition_name": self.config["name"],
                }
                if data["company_name"]:
                    results.append(ScraperResult(
                        # BUG FIX: this used to hardcode source="shanghai_expo"
                        # regardless of which exhibition was actually being
                        # scraped, which — combined with the pipeline only
                        # ever registering one exhibition under that one fixed
                        # key (see pipeline/orchestrator.py) — meant Auto
                        # Shanghai (and now Automechanika Frankfurt) were
                        # fully built and unit-tested in isolation but never
                        # actually reachable through a real pipeline run.
                        source=self.exhibition,
                        source_id=data.get("profile_url", ""),
                        raw_data=data,
                        success=True,
                    ))
            except Exception as e:
                logger.warning("Failed to parse an exhibitor card (%s): %s", self.exhibition, e)

        return results

    @staticmethod
    def _text(element, selector: str) -> str:
        if not selector:
            return ""
        el = element.select_one(selector)
        return el.get_text(strip=True) if el else ""

    @staticmethod
    def _href(element, selector: str) -> str:
        if not selector:
            return ""
        el = element.select_one(selector)
        return el.get("href", "") if el else ""
