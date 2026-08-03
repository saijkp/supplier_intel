"""
discovery/discovery_service.py

AI-assisted supplier discovery, grounded entirely in real SerpAPI
search results -- never a freeform "list me suppliers for X" prompt.
Every accepted supplier traces to: a real SerpAPI search hit (Google,
via config.settings.SERPAPI_KEY) -> a real successfully-fetched
website -> that website's own text corroborating the identity found in
the search result. The only LLM call anywhere in this pipeline
(discovery.candidate_validator.CandidateValidator) reads a real fetched
document; it never generates a company from nothing. See
.claude/plans/deep-wibbling-rivest.md for the full design rationale.

Pipeline:
1. discovery.query_builder.build_queries() -- mechanical query variants.
2. scrapers.google_search_scraper.GoogleSearchScraper.scrape() -- the
   entire web-research grounding source, already integrated elsewhere
   in this codebase.
3. discovery.candidate_extractor.extract_candidates() -- mechanical,
   no LLM: dedupe to one candidate per registered domain, filtering
   platform/social/directory domains via the same logic
   scrapers.company_website_finder.py already established.
4. discovery.candidate_validator.CandidateValidator.validate() -- the
   one LLM call, reading a real fetched page.
5. deduplication.matcher.SupplierMatcher.resolve_and_store() -- the
   SAME dedup engine already in production, so a rediscovered existing
   supplier merges automatically. Zero new dedup logic.
6. Every candidate -- accepted or rejected -- is written to
   raw_source_data (source='discovery') via the existing
   save_raw()/mark_raw_processed(), plus a summary row in
   discovery_runs. Full evidence trail either way, so a rejected
   candidate is auditable, not silently discarded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

from deduplication.matcher import SupplierMatcher
from discovery.candidate_extractor import extract_candidates
from discovery.candidate_validator import CandidateValidator
from discovery.query_builder import build_queries
from storage.repository import SupplierRepository

logger = logging.getLogger(__name__)


@dataclass
class DiscoveryOutcome:
    candidates_found: int = 0
    candidates_validated: int = 0
    candidates_rejected: int = 0
    candidates_duplicate: int = 0  # validated AND auto-merged into an existing supplier -- no new row
    new_supplier_ids: List[int] = field(default_factory=list)  # a genuinely new row, whether outright ("created") or pending human review ("review_queued")
    review_queued_supplier_ids: List[int] = field(default_factory=list)  # subset of new_supplier_ids also awaiting dedup review


class DiscoveryService:

    def __init__(
        self,
        repo: Optional[SupplierRepository] = None,
        google_scraper: Optional[Any] = None,
        website_fetcher: Optional[Any] = None,
        candidate_validator: Optional[CandidateValidator] = None,
        matcher: Optional[SupplierMatcher] = None,
    ):
        self.repo = repo or SupplierRepository()
        if google_scraper is not None:
            self.google_scraper = google_scraper
        else:
            from scrapers.google_search_scraper import GoogleSearchScraper

            self.google_scraper = GoogleSearchScraper()
        if website_fetcher is not None:
            self.website_fetcher = website_fetcher
        else:
            from scrapers.own_website_scraper import OwnWebsiteScraper

            self.website_fetcher = OwnWebsiteScraper()
        self.candidate_validator = candidate_validator or CandidateValidator(website_fetcher=self.website_fetcher)
        self.matcher = matcher or SupplierMatcher(self.repo)

    def discover(
        self, product: str, category: Optional[str] = None, country: Optional[str] = None,
        max_candidates: int = 20,
    ) -> DiscoveryOutcome:
        outcome = DiscoveryOutcome()
        queries = build_queries(product, category=category, country=country)

        all_candidates = []
        seen_domains: set = set()
        for query in queries:
            if len(all_candidates) >= max_candidates:
                break
            try:
                results = self.google_scraper.scrape(query, max_results=20)
            except Exception as e:
                logger.error("discovery: search failed for %r: %s", query, e)
                continue
            for candidate in extract_candidates(results):
                if candidate.domain in seen_domains:
                    continue
                seen_domains.add(candidate.domain)
                all_candidates.append(candidate)
                if len(all_candidates) >= max_candidates:
                    break

        outcome.candidates_found = len(all_candidates)

        for candidate in all_candidates:
            self._process_candidate(candidate, product, country, outcome)

        self.repo.record_discovery_run(
            product_query=product, category=category, country=country,
            candidates_found=outcome.candidates_found, candidates_validated=outcome.candidates_validated,
            candidates_rejected=outcome.candidates_rejected, candidates_duplicate=outcome.candidates_duplicate,
        )
        return outcome

    def _process_candidate(self, candidate, product: str, country: Optional[str], outcome: DiscoveryOutcome) -> None:
        try:
            validation = self.candidate_validator.validate(candidate, product)
        except Exception as e:  # noqa: BLE001 -- one candidate's failure must never abort the whole discovery run
            logger.error("discovery: validation failed for %s: %s", candidate.domain, e)
            self.repo.save_raw(
                source="discovery",
                raw_data={"title": candidate.title, "link": candidate.link, "domain": candidate.domain},
                source_id=candidate.domain, processing_status="failed",
            )
            outcome.candidates_rejected += 1
            return

        raw_id = self.repo.save_raw(
            source="discovery",
            raw_data={
                "title": candidate.title, "link": candidate.link, "snippet": candidate.snippet,
                "domain": candidate.domain, "extracted_name": validation.extracted_name,
                "extracted_country": validation.extracted_country,
                "name_match_score": validation.name_match_score, "reason": validation.reason,
            },
            source_id=candidate.domain, processing_status="pending",
        )

        if not validation.validated:
            outcome.candidates_rejected += 1
            self.repo.mark_raw_processed(raw_id, status="failed", error_message=validation.reason)
            return

        outcome.candidates_validated += 1
        supplier_data = {
            "canonical_name": validation.extracted_name,
            "domain": candidate.domain,
            "country": validation.extracted_country or country,
            "discovery_source": "discovery_service",
        }
        try:
            result = self.matcher.resolve_and_store(supplier_data)
        except Exception as e:
            logger.error("discovery: resolve_and_store failed for %s: %s", candidate.domain, e)
            self.repo.mark_raw_processed(raw_id, status="failed", error_message=str(e))
            return

        action = result.get("action")
        supplier_id = result.get("supplier_id") or result.get("new_supplier_id")
        self.repo.mark_raw_processed(raw_id, golden_record_id=supplier_id, status="processed")

        if action == "merged":
            # Auto-merged into an existing record -- no new row, real
            # dedup, matches config.settings.DEDUP_AUTO_MERGE_THRESHOLD's
            # own semantics exactly as SupplierMatcher already defines them.
            outcome.candidates_duplicate += 1
        elif supplier_id is not None:
            # "created" or "review_queued" -- both genuinely create a new
            # row (create_golden_record), review_queued just also flags it
            # for human dedup review against a close-but-not-auto-merge match.
            outcome.new_supplier_ids.append(supplier_id)
            if action == "review_queued":
                outcome.review_queued_supplier_ids.append(supplier_id)
