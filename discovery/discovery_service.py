"""
discovery/discovery_service.py

AI-assisted supplier discovery. Two candidate sources feed the same
validation/dedup pipeline (source="serpapi", the default, or
source="llm"):

- serpapi: grounded entirely in real SerpAPI search results -- never a
  freeform "list me suppliers for X" prompt. Every accepted supplier
  traces to: a real SerpAPI search hit (Google, via
  config.settings.SERPAPI_KEY) -> a real successfully-fetched website
  -> that website's own text corroborating the identity found in the
  search result.
- llm: discovery.llm_candidate_source.LLMCandidateSource asks
  gpt-4o-mini directly for manufacturer names it already has
  confidence about -- see that module's own docstring for why this
  doesn't weaken the anti-hallucination guarantee: nothing it proposes
  is ever trusted on its own, it still has to pass the identical
  validation gate below. Written to raw_source_data with
  source="llm-discovery" (not "discovery"), so
  verification.scorer.SOURCE_QUALITY_WEIGHTS treats a candidate found
  this way as weaker provenance than one independently corroborated by
  a real search hit.

Either way, from "candidate found" onward the pipeline is identical:
1. discovery.candidate_validator.CandidateValidator.validate() -- the
   one LLM call in the serpapi path (two, for the llm path -- one to
   generate the candidate, one to validate it, reading a REAL fetched
   page either way); grounded corroboration, never a bare LLM claim
   trusted on its own.
2. deduplication.matcher.SupplierMatcher.resolve_and_store() -- the
   SAME dedup engine already in production, so a rediscovered existing
   supplier merges automatically. Zero new dedup logic.
3. Every candidate -- accepted or rejected -- is written to
   raw_source_data via the existing save_raw()/mark_raw_processed(),
   plus a summary row in discovery_runs. Full evidence trail either
   way, so a rejected candidate is auditable, not silently discarded.

See .claude/plans/deep-wibbling-rivest.md for the original serpapi-path
design rationale.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

from deduplication.matcher import SupplierMatcher
from discovery.candidate_extractor import extract_candidates
from discovery.candidate_validator import (
    REASON_EMPTY_PAGE,
    REASON_FETCH_EXCEPTION_PREFIX,
    REASON_FETCH_UNSUCCESSFUL_PREFIX,
    REASON_MARKETPLACE_HOST_PREFIX,
    REASON_TRADER_PREFIX,
    CandidateValidator,
)
from discovery.llm_candidate_source import LLMCandidateSource
from discovery.query_builder import build_queries
from storage.repository import SupplierRepository

logger = logging.getLogger(__name__)

_VALID_SOURCES = ("serpapi", "llm")


@dataclass
class DiscoveryOutcome:
    candidates_generated: int = 0  # llm source only -- raw (name, website) pairs the model proposed before any filtering; 0 for serpapi
    candidates_found: int = 0
    candidates_validated: int = 0
    candidates_rejected: int = 0
    candidates_duplicate: int = 0  # validated AND auto-merged into an existing supplier -- no new row
    new_supplier_ids: List[int] = field(default_factory=list)  # a genuinely new row, whether outright ("created") or pending human review ("review_queued")
    review_queued_supplier_ids: List[int] = field(default_factory=list)  # subset of new_supplier_ids also awaiting dedup review
    website_resolved: int = 0  # candidates whose site fetched successfully (validate() gate 2), whether or not they went on to validate
    content_matched: int = 0   # candidates whose fetched page actually mentioned the product term (validate() gate 5) -- see _process_candidate's own comment


class DiscoveryService:

    def __init__(
        self,
        repo: Optional[SupplierRepository] = None,
        google_scraper: Optional[Any] = None,
        website_fetcher: Optional[Any] = None,
        candidate_validator: Optional[CandidateValidator] = None,
        matcher: Optional[SupplierMatcher] = None,
        llm_candidate_source: Optional[LLMCandidateSource] = None,
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
        self.llm_candidate_source = llm_candidate_source or LLMCandidateSource()

    def discover(
        self, product: str, category: Optional[str] = None, country: Optional[str] = None,
        max_candidates: int = 20, application: Optional[str] = None,
        key_specifications: Optional[List[str]] = None, source: str = "serpapi",
    ) -> DiscoveryOutcome:
        if source not in _VALID_SOURCES:
            raise ValueError(f"unknown discovery source {source!r} -- expected one of {_VALID_SOURCES}")

        outcome = DiscoveryOutcome()

        if source == "llm":
            all_candidates, generation_stats = self.llm_candidate_source.find_candidates(
                product, country=country, max_candidates=max_candidates,
            )
            outcome.candidates_generated = generation_stats.raw_generated
            raw_source = "llm-discovery"
            outcome.candidates_found = len(all_candidates)
            for candidate in all_candidates:
                self._process_candidate(candidate, product, country, outcome, raw_source=raw_source)
            self.repo.record_discovery_run(
                product_query=product, category=category, country=country,
                candidates_found=outcome.candidates_found, candidates_validated=outcome.candidates_validated,
                candidates_rejected=outcome.candidates_rejected, candidates_duplicate=outcome.candidates_duplicate,
            )
            return outcome

        queries = build_queries(
            product, category=category, country=country,
            application=application, key_specifications=key_specifications,
        )

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
            self._process_candidate(candidate, product, country, outcome, raw_source="discovery")

        self.repo.record_discovery_run(
            product_query=product, category=category, country=country,
            candidates_found=outcome.candidates_found, candidates_validated=outcome.candidates_validated,
            candidates_rejected=outcome.candidates_rejected, candidates_duplicate=outcome.candidates_duplicate,
        )
        return outcome

    def _process_candidate(
        self, candidate, product: str, country: Optional[str], outcome: DiscoveryOutcome,
        raw_source: str = "discovery",
    ) -> None:
        try:
            validation = self.candidate_validator.validate(candidate, product)
        except Exception as e:  # noqa: BLE001 -- one candidate's failure must never abort the whole discovery run
            logger.error("discovery: validation failed for %s: %s", candidate.domain, e)
            self.repo.save_raw(
                source=raw_source,
                raw_data={"title": candidate.title, "link": candidate.link, "domain": candidate.domain},
                source_id=candidate.domain, processing_status="failed",
            )
            outcome.candidates_rejected += 1
            return

        # Website resolved = a fetch was actually attempted and didn't
        # fail (validate() gates 2-3) -- reason-text-matched, since "the
        # site fetched" isn't otherwise exposed on ValidationResult. A
        # marketplace-host rejection (gate 2) never even attempts a
        # fetch, so it must be excluded here too, same as an actual
        # fetch failure -- otherwise a never-fetched candidate would be
        # miscounted as resolved. Content matched = reached and passed
        # the product-term gate (gate 6): `validation.validated` already
        # covers a full success without any string-matching (the
        # actually-contracted field, unlike reason's exact wording --
        # callers/tests are free to use any reason text alongside
        # validated=True); the trader-prefix check only exists for the
        # one case that boolean can't distinguish -- a candidate that
        # reached and passed gate 6 but was then rejected at gate 7
        # (trader self-declaration).
        reason = validation.reason
        website_did_not_resolve = (
            reason.startswith(REASON_MARKETPLACE_HOST_PREFIX)
            or reason.startswith(REASON_FETCH_EXCEPTION_PREFIX)
            or reason.startswith(REASON_FETCH_UNSUCCESSFUL_PREFIX)
            or reason == REASON_EMPTY_PAGE
        )
        if not website_did_not_resolve:
            outcome.website_resolved += 1
            if validation.validated or reason.startswith(REASON_TRADER_PREFIX):
                outcome.content_matched += 1

        raw_id = self.repo.save_raw(
            source=raw_source,
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
            # Grounded, not invented: validation gate #5 in
            # candidate_validator.py already deterministically confirmed
            # `product` appears on the supplier's own fetched page text
            # before a candidate can reach this point. Without this, a
            # discovered supplier is invisible to storage.repository's
            # search_suppliers_full() product-term search (LIKE across
            # canonical_name/product_keywords/primary_categories/
            # trailer_components) unless the company's own name happens
            # to contain the search term -- searching for the exact
            # product just discovered them for would silently return
            # nothing.
            "product_keywords": [product],
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

    def backfill_product_keywords(self) -> dict:
        """One-off repair for suppliers `discover()` created before this
        module started writing `product_keywords` on them -- see
        `_process_candidate`'s own comment on why that field matters.
        Those suppliers are invisible to
        storage.repository.search_suppliers_full()'s product-term search
        unless their own name happens to contain the term.

        Reconstructs the missing value from `pipeline_jobs` history, not
        from a guess: `api.jobs.run_discovery_job`/`main.py discover`
        both record each completed run as a job with
        `query="[discovery] {product}"` and
        `stats["new_supplier_ids"]` (DiscoveryOutcome.new_supplier_ids)
        -- a supplier only ever lands in that list if
        candidate_validator.py already deterministically confirmed
        `product` on that supplier's own fetched page before the
        candidate was accepted, so this is exactly as grounded as the
        live fix in `_process_candidate`.

        Fills gaps only: a supplier that already has product_keywords
        (e.g. re-collected since, or matched from a marketplace listing)
        is left untouched, so this is safe to run more than once.
        """
        prefix = "[discovery] "
        updated: list = []
        already_had_keywords: list = []
        missing_supplier: list = []

        for job in self.repo.list_pipeline_jobs(limit=100_000):
            query = job.get("query") or ""
            if job.get("status") != "completed" or not query.startswith(prefix):
                continue
            product = query[len(prefix):]
            stats = job.get("stats") or {}
            for supplier_id in stats.get("new_supplier_ids") or []:
                supplier = self.repo.get_supplier(supplier_id)
                if supplier is None:
                    missing_supplier.append(supplier_id)
                    continue
                if supplier.get("product_keywords"):
                    already_had_keywords.append(supplier_id)
                    continue
                self.repo.update_supplier_fields_with_history(
                    supplier_id, {"product_keywords": [product]},
                    changed_by="discovery_service",
                    change_reason="backfill: product term this supplier was originally discovered for",
                )
                updated.append(supplier_id)

        return {
            "updated_supplier_ids": updated,
            "already_had_keywords_supplier_ids": already_had_keywords,
            "missing_supplier_ids": missing_supplier,
        }
