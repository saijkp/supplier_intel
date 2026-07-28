"""
scrapers/global_directory_scraper.py

Config-driven scraper for B2B supplier directories outside the
China/India coverage the platform already had — addressing the
geographic-expansion gap. Turkey (axle/suspension manufacturing hub),
Vietnam (a major "China+1" relocation destination), and Eastern Europe
(Poland/Czech Republic — often tariff-advantaged and faster-shipping
for UK buyers than China) are configured by default.

Same architecture as scrapers.shanghai_expo_scraper.ShanghaiExpoScraper:
every source-specific detail (base search URL template AND selectors)
lives in DIRECTORY_SOURCES below, so adding a new country/directory, or
fixing one whose site changed, is a config edit, not a rewrite.

IMPORTANT — NOT VERIFIED AGAINST LIVE SITES:
I don't have network access to browse these directories' actual current
HTML structure. DIRECTORY_SOURCES below is a best-effort configuration
in the same shape every other scraper in this codebase uses, written so
it's easy to correct once you (or a scrape run) hit a real page and can
confirm the actual selectors:
  1. Confirm each directory's actual search URL structure — many trade
     body directories require a session/cookie or a different query
     param name than assumed here.
  2. Confirm result-card selectors against the live HTML.
  3. Check each site's terms of service before scraping.
  4. Europages in particular spans many countries in one search — the
     'europages_eastern_europe' entry doesn't hardcode a country_hint
     for this reason (unlike the single-country directories), so
     per-result country text is trusted as scraped rather than assumed.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Any, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper, ScraperResult

logger = logging.getLogger(__name__)


def _iso_country_name(code: Any) -> Optional[str]:
    """'DE' -> 'Germany', matching how country is stored everywhere
    else in this database (full names, e.g. 'United Kingdom', not ISO
    codes) -- storing a bare code here would silently break the exact-
    match country filter search_suppliers_full relies on. pycountry is
    already a project dependency (confirmed unused elsewhere, per
    verification/commercial_scoring.py's own note), so this is the
    natural place to actually use it, not a new dependency.
    """
    if not code or not isinstance(code, str):
        return None
    import pycountry

    country = pycountry.countries.get(alpha_2=code.strip().upper())
    return country.name if country else None

# Each entry fully describes one directory: display name, an optional
# country_hint (stamped onto every result whose card doesn't show an
# explicit country — omit for directories spanning multiple countries),
# the search URL template (`{query}`/`{page}` substituted in), and
# CSS selectors for its result cards.
DIRECTORY_SOURCES: Dict[str, Dict[str, Any]] = {
    "turkey_tim": {
        "name": "Turkish Exporters Assembly (TIM) Member Directory",
        "country_hint": "Turkey",
        "search_url_template": "https://www.tim.org.tr/en/member-search?q={query}&page={page}",
        "selectors": {
            "company_card": ".member-card, [data-testid='member-card']",
            "company_name": ".member-name, h3",
            "country": ".member-country",
            "products": ".sector-tag",
            "website": "a.member-website",
            "profile_link": "a.member-profile",
        },
    },
    "vietnam_vcci": {
        "name": "Vietnam Chamber of Commerce and Industry (VCCI) Directory",
        "country_hint": "Vietnam",
        "search_url_template": "https://en.vcci.com.vn/directory/search?keyword={query}&page={page}",
        "selectors": {
            "company_card": ".company-item, [data-testid='company-item']",
            "company_name": ".company-name",
            "country": ".company-country",
            "products": ".industry-tag",
            "website": "a.company-website",
            "profile_link": "a.company-detail",
        },
    },
    "europages_eastern_europe": {
        "name": "Europages (Eastern Europe manufacturers)",
        "country_hint": "",  # spans multiple countries — trust each card's own country text
        "search_url_template": "https://www.europages.co.uk/companies/{query}.html?page={page}",
        "selectors": {
            "company_card": ".company-card, [data-testid='company-card']",
            "company_name": ".company-name",
            "country": ".company-country",
            "products": ".activity-tag",
            "website": "a.company-website",
            "profile_link": "a.company-link",
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


class GlobalDirectoryScraper(BaseScraper):
    """
    `directory` selects which DIRECTORY_SOURCES entry to use, and
    becomes this scraper's `source_name` — results are saved to
    raw_source_data with source='turkey_tim' (etc.).
    """

    def __init__(
        self,
        directory: str = "turkey_tim",
        http_client: Optional[httpx.Client] = None,
        enable_delays: bool = True,
    ):
        if directory not in DIRECTORY_SOURCES:
            raise ValueError(
                f"Unknown directory '{directory}'. Add it to DIRECTORY_SOURCES "
                f"or choose one of: {list(DIRECTORY_SOURCES)}"
            )
        super().__init__(directory, enable_delays=enable_delays)
        self.directory = directory
        self.config = DIRECTORY_SOURCES[directory]
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
                logger.error("%s page %d failed for '%s': %s", self.directory, page, query, e)
                if page == 1:
                    return [self.error_result(str(e))]
                break

            page_results = self._parse_companies(response.text)
            if not page_results:
                logger.debug("%s: no more results after page %d for '%s'", self.directory, page - 1, query)
                break

            results.extend(page_results)
            self._polite_delay(3.0, 6.0)

        logger.info("%s: %d companies for '%s'", self.directory, len(results), query)
        return results

    def _parse_companies(self, html: str) -> List[ScraperResult]:
        # Prefer JSON-LD structured data over CSS-selector scraping
        # whenever a page actually embeds it: schema.org markup is
        # machine-readable by design and far more stable across a
        # front-end redesign than the page's own CSS classes -- found
        # via a real calibration run against europages, whose current
        # markup is entirely auto-generated Tailwind utility classes
        # with no semantic hooks left for `selectors` to hook onto at
        # all, while the exact same page's JSON-LD block still carries
        # every result's real name/country cleanly. Falls through to
        # the CSS-selector path below for any directory whose page
        # doesn't embed this (which _parse_json_ld_companies reports
        # as simply finding nothing, not an error).
        json_ld_results = self._parse_json_ld_companies(html)
        if json_ld_results:
            return json_ld_results

        selectors = self.config["selectors"]
        country_hint = self.config.get("country_hint", "")
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select(selectors["company_card"])
        results: List[ScraperResult] = []

        for card in cards:
            try:
                country = self._text(card, selectors["country"])
                data = {
                    "company_name": self._text(card, selectors["company_name"]),
                    "country": country or country_hint,
                    "products": [
                        p.get_text(strip=True) for p in card.select(selectors["products"])
                    ],
                    "website": self._href(card, selectors["website"]),
                    "profile_url": self._href(card, selectors["profile_link"]),
                    "directory": self.directory,
                    "directory_name": self.config["name"],
                }
                if data["company_name"]:
                    results.append(ScraperResult(
                        source=self.directory,
                        source_id=data.get("profile_url", ""),
                        raw_data=data,
                        success=True,
                    ))
            except Exception as e:
                logger.warning("Failed to parse a company card (%s): %s", self.directory, e)

        return results

    def _parse_json_ld_companies(self, html: str) -> List[ScraperResult]:
        """Extract schema.org Organization entries from any JSON-LD
        <script> block shaped like an ItemList of ItemList-position
        entries wrapping an Organization -- the shape europages embeds
        on its own search results pages. Returns [] (never raises) for
        any page that doesn't have this, which is exactly what makes
        it safe to try unconditionally ahead of the CSS-selector path
        for every directory, not just europages.

        Deliberately doesn't populate "website": the URL in this JSON-LD
        is the directory's OWN profile page for the company (e.g.
        europages.co.uk/en/company/...), never the company's real
        outbound domain -- populating `website`/`domain` with that
        would collide every company here onto the same fake shared
        domain, silently defeating the UNIQUE constraint on
        suppliers.domain and the domain-exact-match dedup tier along
        with it. A real domain, if one exists, is CompanyWebsiteFinder's
        job (main.py find-websites), same as any other domain-less
        supplier.
        """
        country_hint = self.config.get("country_hint", "")
        soup = BeautifulSoup(html, "html.parser")
        results: List[ScraperResult] = []

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                parsed = json.loads(script.string or "")
            except (TypeError, ValueError):
                continue

            graph = parsed.get("@graph") if isinstance(parsed, dict) else None
            entries = graph if isinstance(graph, list) else [parsed]

            for entry in entries:
                if not isinstance(entry, dict) or entry.get("@type") != "ItemList":
                    continue
                for list_item in entry.get("itemListElement") or []:
                    org = list_item.get("item") if isinstance(list_item, dict) else None
                    if not isinstance(org, dict) or org.get("@type") != "Organization":
                        continue
                    try:
                        name = (org.get("name") or "").strip()
                        if not name:
                            continue
                        address = org.get("address") or {}
                        country = None
                        if isinstance(address, dict):
                            country = _iso_country_name(address.get("addressCountry"))
                        data = {
                            "company_name": name,
                            "country": country or country_hint,
                            "products": [],
                            "profile_url": org.get("url", ""),
                            "directory": self.directory,
                            "directory_name": self.config["name"],
                        }
                        results.append(ScraperResult(
                            source=self.directory,
                            source_id=data["profile_url"],
                            raw_data=data,
                            success=True,
                        ))
                    except Exception as e:
                        logger.warning("Failed to parse a JSON-LD Organization (%s): %s", self.directory, e)

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
