"""
discovery/companies_house_sic_source.py

A third candidate-generation source for DiscoveryService (source=
"companies_house_sic"), alongside serpapi and llm -- see
discovery/discovery_service.py's own module docstring for how those
two work. This one starts from a genuinely different kind of evidence:
not a search-engine hit, not an LLM guess, but a real, bulk UK
Companies House SIC-code search (verification/companies_house_client.py's
search_by_sic_codes()) -- every candidate this produces is a real,
currently-active, UK-registered company BEFORE any website is even
looked up.

Scoped narrowly on purpose: Companies House is UK-only, so this source
only makes sense for a category that's genuinely UK-scoped. Checked
live against this codebase's own confirmed suppliers before building
this: Material Handling is UK-only by design (CLAUDE.md standing rule
9), but Injection Moulding/Weld Mesh/Metal Pressing/Brake Cable are
overwhelmingly non-UK in practice (only ~3/37, 0/59, 0/93, and 3/100 of
each category's currently-known suppliers are UK-addressed at all) --
this source would only ever supplement a small fraction of those
categories' real candidate pool. No category awareness lives here
either (same discipline as companies_house_client.py/
uk_company_verification_service.py) -- `sic_codes` is just a parameter,
the caller decides which codes fit which category.

SIC classification alone is existence + registration proof, NOT a
"this company manufactures X" signal -- confirmed live against this
codebase's own 31 real, already Companies-House-verified Material
Handling suppliers: only 4 (Jungheinrich UK, Principle Fork Lifts,
Sidetracker Engineering, Narrow Aisle) carry SIC 28220 (the literal
"manufacture of lifting/handling equipment" code) at all. The rest are
registered under wholesale/agent (46140/46690), rental (77120/77320/
77390), or repair (33170/33200) codes instead -- real UK forklift/
material-handling businesses are very often dealer/rental operations
by SIC classification, not manufacturers, even when they're exactly
the right kind of supplier for a sourcing brief. A caller wanting real
recall needs that broader code set, not just the "cleanest" one.

Companies House gives no website at all -- only name, registered
number, address, SIC codes, status. scrapers.company_website_finder.
CompanyWebsiteFinder is reused here PURELY to find a candidate domain
(a name search + one validated-name-match-on-fetched-text check,
already proven for exactly this "have a real name, need a domain" gap
for Alibaba/IndiaMart listings) -- never trusted as the final word.
Every candidate this produces still goes through the exact same
discovery.candidate_validator.CandidateValidator gate every serpapi/
llm candidate does (real fetch, deterministic product-term match, name
corroboration, trader exclusion) before ever being stored as a
supplier -- CompanyWebsiteFinder's own validation only decides whether
a domain is even worth handing to that gate, it is not a substitute
for it.

Cost, honestly: the Companies House search itself is free, but each
company still needs one real SerpAPI search (CompanyWebsiteFinder) to
find a candidate domain, and every domain that passes THAT still costs
a real fetch + OpenAI validation call (CandidateValidator), same as
any other discovery candidate -- this is not a free bulk-import path,
just a different (and for Material Handling, better-targeted) way of
generating candidate NAMES to spend that same real cost on.

Written to raw_source_data with source="companies-house-sic" (not
"discovery" or "llm-discovery") -- see verification/scorer.py's
SOURCE_QUALITY_WEIGHTS for how that provenance is scored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from discovery.candidate_extractor import Candidate
from scrapers.company_website_finder import CompanyWebsiteFinder
from verification.companies_house_client import CompaniesHouseClient

logger = logging.getLogger(__name__)


@dataclass
class SicGenerationStats:
    """Funnel counters before a candidate ever reaches
    discovery.candidate_validator -- mirrors
    discovery.llm_candidate_source.GenerationStats' own shape."""
    companies_found: int = 0    # real CH SIC search hits, before any website lookup
    no_website_found: int = 0   # CompanyWebsiteFinder found no validated domain at all
    deduplicated: int = 0       # unique candidates remaining after domain dedup


class CompaniesHouseSicSource:

    def __init__(
        self, companies_house_client: Optional[CompaniesHouseClient] = None,
        website_finder: Optional[CompanyWebsiteFinder] = None,
    ):
        self.companies_house_client = companies_house_client or CompaniesHouseClient()
        if website_finder is not None:
            self.website_finder = website_finder
        else:
            # Lazy-imported, same convention discovery_service.py's own
            # google_scraper/website_fetcher construction already
            # follows -- real API keys/network only touched when this
            # source is actually used.
            from scrapers.google_search_scraper import GoogleSearchScraper
            from scrapers.own_website_scraper import OwnWebsiteScraper

            self.website_finder = CompanyWebsiteFinder(
                google_scraper=GoogleSearchScraper(), own_website_scraper=OwnWebsiteScraper(),
            )

    def find_candidates(
        self, sic_codes: List[str], max_candidates: int = 20,
    ) -> Tuple[List[Candidate], SicGenerationStats]:
        stats = SicGenerationStats()
        matches = self.companies_house_client.search_by_sic_codes(sic_codes, max_results=max_candidates)
        stats.companies_found = len(matches)

        seen_domains: set = set()
        candidates: List[Candidate] = []
        for match in matches:
            if len(candidates) >= max_candidates:
                break
            try:
                found = self.website_finder.find_website(match.company_name, country="United Kingdom")
            except Exception as e:  # noqa: BLE001 -- one bad lookup must never abort the whole source
                logger.warning(
                    "companies_house_sic_source: website lookup failed for %r: %s", match.company_name, e,
                )
                stats.no_website_found += 1
                continue

            if not found.validated or not found.domain:
                stats.no_website_found += 1
                continue
            if found.domain in seen_domains:
                continue
            seen_domains.add(found.domain)

            sic_display = ", ".join(match.sic_codes) if match.sic_codes else "unknown"
            candidates.append(Candidate(
                title=match.company_name,
                link=found.candidate_url or f"https://{found.domain}",
                snippet=(
                    f"Companies House #{match.company_number}, SIC {sic_display}, "
                    f"{match.registered_office_address or 'UK'}"
                ),
                domain=found.domain,
            ))

        stats.deduplicated = len(candidates)
        return candidates, stats
