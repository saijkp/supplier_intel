"""
discovery/candidate_extractor.py

Purely mechanical extraction of unique candidate company domains from
real SerpAPI search results -- NO LLM call in this step (see
discovery/discovery_service.py's module docstring for why: the entire
anti-hallucination discipline rests on AI only ever reading a real
fetched page, never generating candidates from search-result text
alone). Reuses the exact same platform/social/directory domain
filtering scrapers.company_website_finder.py already established for
the identical "is this a company's own site" question.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List

from deduplication.domain_utils import extract_domain
from scrapers.company_website_finder import _is_usable_candidate_domain


@dataclass
class Candidate:
    title: str
    link: str
    snippet: str
    domain: str


def extract_candidates(search_results: List[Any]) -> List[Candidate]:
    """`search_results` is a list of ScraperResult-shaped objects (as
    returned by scrapers.google_search_scraper.GoogleSearchScraper.scrape)
    -- deduped to one candidate per registered domain, first-seen wins."""
    seen_domains: set = set()
    candidates: List[Candidate] = []
    for result in search_results:
        if not getattr(result, "success", True):
            continue
        raw = result.raw_data or {}
        link = raw.get("link")
        if not link:
            continue
        domain = extract_domain(link)
        if not _is_usable_candidate_domain(domain):
            continue
        if domain in seen_domains:
            continue
        seen_domains.add(domain)
        candidates.append(Candidate(
            title=raw.get("title", "") or "", link=link,
            snippet=raw.get("snippet", "") or "", domain=domain,
        ))
    return candidates
