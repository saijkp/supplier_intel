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

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from deduplication.matcher import SupplierMatcher
from discovery.candidate_extractor import extract_candidates
from discovery.candidate_validator import (
    REASON_EMPTY_PAGE,
    REASON_FETCH_EXCEPTION_PREFIX,
    REASON_FETCH_UNSUCCESSFUL_PREFIX,
    REASON_MARKETPLACE_HOST_PREFIX,
    REASON_SUCCESS_PREFIX,
    REASON_TERM_MISSING_PREFIX,
    REASON_TRADER_PREFIX,
    REASON_UK_NOT_REGISTERED_PREFIX,
    CandidateValidator,
)
from discovery.companies_house_sic_source import CompaniesHouseSicSource
from discovery.llm_candidate_source import LLMCandidateSource
from discovery.query_builder import build_queries
from storage.repository import SupplierRepository

logger = logging.getLogger(__name__)

_VALID_SOURCES = ("serpapi", "llm", "1688", "companies_house_sic")

# discover_to_target()'s round-2 default -- broadened past manufacturer-only
# phrasing, see query_builder.py's own extra_role_words docstring and
# discover_to_target()'s own docstring for the real run that motivated this.
_DEFAULT_TARGET_COUNT_ROLE_WORDS = ("dealer", "distributor", "stockist", "reseller")


@dataclass
class DiscoveryOutcome:
    candidates_generated: int = 0  # llm source only -- raw (name, website) pairs the model proposed before any filtering; 0 for serpapi
    candidates_found: int = 0
    candidates_examined: int = 0  # candidates actually run through _process_candidate (fetch+validate attempted) -- distinct from candidates_found (raw search hits collected before any target_count early-stop); this is what actually counts against a discover_to_target() cost ceiling
    candidates_validated: int = 0  # DISTINCT companies validated -- always == len(new_supplier_ids) by construction (see _record_validation_outcome), never inflated by a candidate that re-validates and merges (same-run repeat across discover_to_target()'s rounds, or a match against a pre-existing supplier) -- that's candidates_duplicate's job, not this one
    candidates_rejected: int = 0
    candidates_duplicate: int = 0  # validated AND auto-merged into an existing supplier -- no new row
    duplicate_supplier_ids: List[int] = field(default_factory=list)  # the EXISTING supplier ids a candidate merged into -- a real, validated match, just not a new row; see api/app.py's export endpoint, which combines this with new_supplier_ids for "every real match this run found"
    new_supplier_ids: List[int] = field(default_factory=list)  # a genuinely new row, whether outright ("created") or pending human review ("review_queued")
    review_queued_supplier_ids: List[int] = field(default_factory=list)  # subset of new_supplier_ids also awaiting dedup review
    website_resolved: int = 0  # candidates whose site fetched successfully (validate() gate 2), whether or not they went on to validate
    content_matched: int = 0   # candidates whose fetched page actually mentioned the product term (validate() gate 5) -- see _process_candidate's own comment
    raw_1688_listings: List[Dict[str, Any]] = field(default_factory=list)  # source='1688' only -- see _discover_1688's own docstring for why this stops short of validation/storage


@dataclass
class DiscoveryProgressEvent:
    """One per-candidate snapshot, fired synchronously during discover()
    as soon as that candidate's validation (and dedup resolution)
    completes -- the first per-candidate-detail progress signal this
    codebase has emitted (every other job's progress -- batch upload,
    sourcing agent -- is aggregate-counts-only). `reason` is always the
    full, unmodified ValidationResult.reason sentence; `badge` (see
    _classify_reason_badge) is a display-only label derived from it by
    prefix-matching the same REASON_* constants _process_candidate
    already checks -- never a new stored taxonomy, never a replacement
    for showing the real reason text alongside it."""
    domain: str
    candidate_title: str  # candidate.title from the search hit -- best label before/without a validated name
    extracted_name: Optional[str]
    status: str   # "validated" | "rejected" | "duplicate" | "existing" (Phase 0 database match, round=0 -- see discover_to_target)
    reason: str
    badge: str    # "marketplace" | "trader" | "term_missing" | "uk_not_registered" | "fetch_failed" | "name_mismatch" | "validated" | "duplicate" | "existing" | "other"
    round: int = 1              # stamped by discover_to_target() when orchestrating multiple rounds; 1 for a bare discover() call; 0 is Phase 0's existing-database check, before any fresh-discovery spend
    round_examined: int = 0     # outcome.candidates_examined at the moment this event fired, for the discover() call that produced it (not cumulative across rounds)
    round_validated: int = 0    # outcome.candidates_validated at the moment this event fired, same scope as round_examined


@dataclass
class DiscoveryToTargetOutcome:
    """What discover_to_target() returns -- dataclasses.asdict()'able
    the same way DiscoveryOutcome/SourcingOutcome already are."""
    product: str
    target_count: int
    ceiling: int
    rounds_run: int = 0
    candidates_found: int = 0
    candidates_examined: int = 0
    candidates_validated: int = 0
    candidates_rejected: int = 0
    candidates_duplicate: int = 0
    duplicate_supplier_ids: List[int] = field(default_factory=list)
    new_supplier_ids: List[int] = field(default_factory=list)
    review_queued_supplier_ids: List[int] = field(default_factory=list)
    existing_supplier_ids: List[int] = field(default_factory=list)  # Phase 0: already in the database, zero-cost match against product/category -- found BEFORE any fresh-discovery round runs, never blended into new_supplier_ids/duplicate_supplier_ids (those specifically track what fresh discovery spent money on; see discover_to_target()'s own Phase 0 comment). A caller combining every id list must dedupe, same as new_supplier_ids/duplicate_supplier_ids already documents.
    reached_target: bool = False
    used_llm_fallback: bool = False
    stopped_reason: str = ""  # "target_reached" | "target_reached_from_existing_database" | "ceiling_reached" | "no_new_candidates_found" | "budget_exhausted_after_llm_fallback"


def _classify_reason_badge(reason: str) -> str:
    """UI-display-only short label for a ValidationResult.reason string
    -- prefix-matches the same named REASON_* constants _process_candidate
    already imports and checks (see that method's own website_resolved/
    content_matched classification for the established style). Never a
    new stored taxonomy -- see DiscoveryProgressEvent's own docstring.
    ("duplicate" is applied at the call site in _process_candidate, not
    here, since this function only ever sees the validation reason, not
    the dedup matcher's merge decision.)"""
    if reason.startswith(REASON_MARKETPLACE_HOST_PREFIX):
        return "marketplace"
    if (
        reason.startswith(REASON_FETCH_EXCEPTION_PREFIX)
        or reason.startswith(REASON_FETCH_UNSUCCESSFUL_PREFIX)
        or reason == REASON_EMPTY_PAGE
    ):
        return "fetch_failed"
    if reason.startswith(REASON_UK_NOT_REGISTERED_PREFIX):
        return "uk_not_registered"
    if reason.startswith(REASON_TERM_MISSING_PREFIX):
        return "term_missing"
    if reason.startswith(REASON_TRADER_PREFIX):
        return "trader"
    if reason.startswith(REASON_SUCCESS_PREFIX):
        return "validated"
    if "does not match the original search result" in reason:
        return "name_mismatch"
    return "other"


class DiscoveryService:

    def __init__(
        self,
        repo: Optional[SupplierRepository] = None,
        google_scraper: Optional[Any] = None,
        website_fetcher: Optional[Any] = None,
        candidate_validator: Optional[CandidateValidator] = None,
        matcher: Optional[SupplierMatcher] = None,
        llm_candidate_source: Optional[LLMCandidateSource] = None,
        china_1688_scraper: Optional[Any] = None,
        companies_house_client: Optional[Any] = None,
        companies_house_sic_source: Optional[CompaniesHouseSicSource] = None,
        playwright_fetcher: Optional[Any] = None,
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
        # Defaults to a real fetcher (not None) -- unlike
        # companies_house_client below, this retry is meant to be the
        # STANDARD path for every discovery run, not an opt-in a caller
        # has to remember to request. See CandidateValidator's own
        # playwright_fetcher constructor doc / _fetch_candidate_site's
        # docstring for the real candidates (Lippert, Dexter Axle) lost
        # to httpx-level fetch failures this recovers.
        if playwright_fetcher is not None:
            self.playwright_fetcher = playwright_fetcher
        else:
            from scrapers.playwright_website_scraper import PlaywrightWebsiteScraper

            self.playwright_fetcher = PlaywrightWebsiteScraper()
        # companies_house_client stays None unless the caller explicitly
        # passes one (main.py discover --require-uk-registration) --
        # see candidate_validator.CandidateValidator's own "Gate 3.5"
        # docstring for why this must not default to a real client the
        # way website_fetcher/llm_client do above.
        self.candidate_validator = candidate_validator or CandidateValidator(
            website_fetcher=self.website_fetcher, companies_house_client=companies_house_client,
            playwright_fetcher=self.playwright_fetcher,
        )
        self.matcher = matcher or SupplierMatcher(self.repo)
        self.llm_candidate_source = llm_candidate_source or LLMCandidateSource()
        if china_1688_scraper is not None:
            self.china_1688_scraper = china_1688_scraper
        else:
            # Reused as-is, unmodified -- Apify-actor-backed (real cost
            # per call), so only constructed when actually needed, same
            # lazy-import convention as google_scraper/website_fetcher
            # above. See scrapers/scraper_1688.py's own module docstring
            # for why this goes through Apify rather than a direct
            # fetch: 1688's search pages are behind an active anti-bot
            # CAPTCHA wall (confirmed directly -- a real headless
            # Chromium hitting the search page on the first request gets
            # redirected to Alibaba's own "/_____tmd_____/punish"
            # endpoint), so there is no direct-fetch alternative here.
            from scrapers.scraper_1688 import China1688Scraper

            self.china_1688_scraper = China1688Scraper()
        # Reuses the SAME companies_house_client instance as gate 3.5
        # above when one was explicitly passed (one API key, one client)
        # -- otherwise CompaniesHouseSicSource constructs its own real
        # client lazily, same "safe to construct without credentials"
        # discipline as every other integration in this codebase.
        self.companies_house_sic_source = companies_house_sic_source or CompaniesHouseSicSource(
            companies_house_client=companies_house_client,
        )

    def discover(
        self, product: str, category: Optional[str] = None, country: Optional[str] = None,
        max_candidates: int = 20, application: Optional[str] = None,
        key_specifications: Optional[List[str]] = None, source: str = "serpapi",
        domain_tld_bias: Optional[str] = None, extra_role_words: Optional[List[str]] = None,
        target_count: Optional[int] = None,
        progress_callback: Optional[Callable[[DiscoveryProgressEvent], None]] = None,
        recover_dead_domains: bool = False,
        sic_codes: Optional[List[str]] = None,
        sic_name_keywords: Optional[List[str]] = None,
    ) -> DiscoveryOutcome:
        """`target_count`, `progress_callback`, `recover_dead_domains`,
        and `sic_codes` are all additive, default-None/False/unset --
        every existing caller (main.py discover, sourcing.sourcing_agent.
        SourcingAgentService, every existing test) is unaffected.

        `sic_codes`, required when `source="companies_house_sic"` (see
        discovery/companies_house_sic_source.py's own docstring), is a
        list of UK SIC 2007 codes ORed together in one real Companies
        House bulk search -- every real match is then a candidate whose
        website still needs finding (real SerpAPI cost) and validating
        (real fetch + OpenAI cost), same as any other source. Candidates
        from this source skip ONLY the soft-signal trader gate (a real,
        multi-brand dealer is the wanted supplier type for a category
        sourced this way, not a disqualifying one -- see
        candidate_validator.CandidateValidator.validate()'s own
        `skip_soft_trader_signals` docstring); the hard self-declaration
        phrase list and every other gate still apply unchanged.
        `sic_name_keywords`, also only meaningful with this source, is
        passed straight through to CompaniesHouseSicSource.find_candidates'
        own `name_keywords` -- a free pre-filter on the company name
        Companies House itself returned, before any paid call. See that
        method's own docstring for the real, quantified false-negative
        tradeoff (checked against this codebase's own confirmed Material
        Handling suppliers) before using this on a new category.

        `target_count`, when given, is an early-stop on VALIDATION, not
        on candidate collection: the raw-candidate-gathering loop below
        still respects `max_candidates` exactly as before, but the
        validation loop stops as soon as `outcome.candidates_validated
        >= target_count`, saving the real fetch+LLM cost of validating
        candidates beyond what's actually needed. See
        discover_to_target() for the round-based orchestrator built on
        top of this for a genuine "keep going until N found" search.

        `progress_callback`, when given, is fired once per candidate
        (see _process_candidate) with a DiscoveryProgressEvent -- the
        first per-candidate-detail progress signal this codebase emits
        anywhere (see DiscoveryProgressEvent's own docstring).

        `recover_dead_domains`, when True, gives a candidate that failed
        validation SPECIFICALLY for a dead/unreachable domain one real
        retry via candidate_validator.CandidateValidator.recover() --
        never for a marketplace/trader/name-mismatch/term-missing
        rejection, those stay as-is. A real extra SerpAPI+fetch+LLM cost
        per dead candidate -- opt-in, never on by default."""
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
                self._process_candidate(
                    candidate, product, country, outcome, raw_source=raw_source,
                    progress_callback=progress_callback, recover_dead_domains=recover_dead_domains,
                )
                if target_count is not None and outcome.candidates_validated >= target_count:
                    break
            self.repo.record_discovery_run(
                product_query=product, category=category, country=country,
                candidates_found=outcome.candidates_found, candidates_validated=outcome.candidates_validated,
                candidates_rejected=outcome.candidates_rejected, candidates_duplicate=outcome.candidates_duplicate,
            )
            return outcome

        if source == "companies_house_sic":
            if not sic_codes:
                raise ValueError("source='companies_house_sic' requires sic_codes")
            all_candidates, generation_stats = self.companies_house_sic_source.find_candidates(
                sic_codes, max_candidates=max_candidates, name_keywords=sic_name_keywords,
            )
            outcome.candidates_generated = generation_stats.companies_found
            raw_source = "companies-house-sic"
            outcome.candidates_found = len(all_candidates)
            for candidate in all_candidates:
                self._process_candidate(
                    candidate, product, country, outcome, raw_source=raw_source,
                    progress_callback=progress_callback, recover_dead_domains=recover_dead_domains,
                    skip_soft_trader_signals=True,
                )
                if target_count is not None and outcome.candidates_validated >= target_count:
                    break
            self.repo.record_discovery_run(
                product_query=product, category=category, country=country,
                candidates_found=outcome.candidates_found, candidates_validated=outcome.candidates_validated,
                candidates_rejected=outcome.candidates_rejected, candidates_duplicate=outcome.candidates_duplicate,
            )
            return outcome

        if source == "1688":
            return self._discover_1688(product, category, max_candidates, outcome)

        queries = build_queries(
            product, category=category, country=country,
            application=application, key_specifications=key_specifications,
            domain_tld_bias=domain_tld_bias, extra_role_words=extra_role_words,
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
            self._process_candidate(
                candidate, product, country, outcome, raw_source="discovery",
                progress_callback=progress_callback, recover_dead_domains=recover_dead_domains,
            )
            if target_count is not None and outcome.candidates_validated >= target_count:
                break

        self.repo.record_discovery_run(
            product_query=product, category=category, country=country,
            candidates_found=outcome.candidates_found, candidates_validated=outcome.candidates_validated,
            candidates_rejected=outcome.candidates_rejected, candidates_duplicate=outcome.candidates_duplicate,
        )
        return outcome

    def discover_to_target(
        self, product: str, target_count: int, category: Optional[str] = None,
        country: Optional[str] = None, application: Optional[str] = None,
        key_specifications: Optional[List[str]] = None, domain_tld_bias: Optional[str] = None,
        extra_role_words: Optional[List[str]] = None, max_multiplier: int = 5,
        allow_llm_fallback: bool = True,
        progress_callback: Optional[Callable[[DiscoveryProgressEvent], None]] = None,
        recover_dead_domains: bool = False,
    ) -> DiscoveryToTargetOutcome:
        """Round-based "keep going until N validated suppliers are
        found, but stop well before unbounded spend" orchestrator built
        entirely on top of the existing discover() -- no new
        candidate-finding or validation logic, just a cost-bounded loop
        over it. `max_multiplier=5` matches
        sourcing.sourcing_agent.SourcingAgentService's own
        DEFAULT_MAX_MULTIPLIER, so the two mechanisms share the same
        cost-governance philosophy even though they're separate code
        paths (this one, unlike SourcingAgentService, has no chat-brief
        parsing or dossier generation -- it's the plain "find me suppliers"
        case).

        Phase 0 (before any round): check the existing database first,
        via the same zero-cost mechanism sourcing.sourcing_agent.
        SourcingAgentService.run()'s own "Phase 1" already uses -- see
        the inline comment right above where it runs, below, for the
        full reasoning. Every round after this one targets target_count
        MINUS whatever Phase 0 already found, not the original
        target_count.

        Round 1: today's default discover() -- base manufacturer/
        supplier/factory templates only, source='serpapi'.

        Round 2 (only if round 1 fell short, found at least one raw
        candidate, and budget remains): the SAME discover(), but with
        `extra_role_words` populated (dealer/distributor/stockist/
        reseller by default) -- broadened phrasing, not a re-query of
        the same terms. See query_builder.py's own docstring: a real
        Material Handling discovery run went from a 0% to a 25% hit
        rate from exactly this kind of broadening, and query_builder.py
        now runs these queries FIRST specifically so this round can't
        be starved out by the base templates re-filling the budget.

        Round 3 (last resort only, opt-out via allow_llm_fallback=False):
        falls back to source='llm' -- gpt-4o-mini's own knowledge rather
        than a real search hit, weaker provenance (see this module's own
        docstring on verification.scorer.SOURCE_QUALITY_WEIGHTS), tried
        only after both real-search rounds have been exhausted.

        Each round's progress events are stamped with their round number
        before being forwarded to `progress_callback`, so a live UI can
        show which round a given candidate came from."""
        role_words = extra_role_words if extra_role_words is not None else list(_DEFAULT_TARGET_COUNT_ROLE_WORDS)
        ceiling = target_count * max_multiplier
        result = DiscoveryToTargetOutcome(product=product, target_count=target_count, ceiling=ceiling)

        def _stamped_callback(round_number: int) -> Optional[Callable[[DiscoveryProgressEvent], None]]:
            if not progress_callback:
                return None

            def _callback(event: DiscoveryProgressEvent) -> None:
                event.round = round_number
                progress_callback(event)

            return _callback

        def _merge(outcome: DiscoveryOutcome) -> None:
            result.candidates_found += outcome.candidates_found
            result.candidates_examined += outcome.candidates_examined
            result.candidates_validated += outcome.candidates_validated
            result.candidates_rejected += outcome.candidates_rejected
            result.candidates_duplicate += outcome.candidates_duplicate
            result.duplicate_supplier_ids.extend(outcome.duplicate_supplier_ids)
            result.new_supplier_ids.extend(outcome.new_supplier_ids)
            result.review_queued_supplier_ids.extend(outcome.review_queued_supplier_ids)

        # Phase 0: check the existing database first -- zero-cost
        # compared to fresh AI Discovery (no SerpAPI search, no
        # candidate-validation LLM call), the same mechanism
        # sourcing.sourcing_agent.SourcingAgentService.run()'s own
        # "Phase 1" already uses (SupplierRepository.
        # search_suppliers_full(product_query=...)). No formal
        # category tag involved or needed: product_query is a plain-
        # text LIKE match against canonical_name/product_keywords/
        # primary_categories/trailer_components, and product_keywords
        # is populated with the exact product string on every prior
        # discovery run (see _record_validation_outcome's own comment
        # below) -- so a plain product-term search here is already
        # precisely the right shape of match, no new tagging concept
        # required. `category`, when given, is OR'd in as a second
        # independent term via search_suppliers_full's category_query.
        # Unlike sourcing_agent.py's _is_stale-gated skip, nothing here
        # re-collects or re-verifies a matched supplier -- this method
        # has no per-candidate capability-requirement gate to satisfy,
        # so an existing row is surfaced exactly as it already stands,
        # keeping this phase genuinely zero-cost.
        try:
            existing = self.repo.search_suppliers_full(
                product_query=product, category_query=category, country=country, limit=target_count,
            )
        except Exception as e:  # noqa: BLE001 -- must never block falling through to fresh discovery
            logger.error("discover_to_target: existing-database search failed for product=%r: %s", product, e)
            existing = []

        result.existing_supplier_ids = [s["id"] for s in existing]
        if progress_callback:
            for supplier in existing:
                progress_callback(DiscoveryProgressEvent(
                    domain=supplier.get("domain") or "",
                    candidate_title=supplier.get("canonical_name") or "",
                    extracted_name=supplier.get("canonical_name"),
                    status="existing",
                    reason="already in the database -- matched via product/category, zero cost",
                    badge="existing",
                    round=0,
                ))

        # Every subsequent round targets this REDUCED count, not the
        # original target_count -- same pattern round 2/3 already use
        # to shrink their own target by what round 1 already validated,
        # just applied one step earlier. The ceiling below stays
        # anchored to the original target_count deliberately: it's a
        # hard cap on fresh-candidate EXAMINATION spend, not something
        # a free database hit should shrink.
        remaining_target = max(0, target_count - len(result.existing_supplier_ids))
        if remaining_target == 0:
            result.reached_target = True
            result.stopped_reason = "target_reached_from_existing_database"
            return result

        # Round 1
        outcome1 = self.discover(
            product, category=category, country=country, max_candidates=ceiling,
            application=application, key_specifications=key_specifications,
            domain_tld_bias=domain_tld_bias, source="serpapi",
            target_count=remaining_target, progress_callback=_stamped_callback(1),
            recover_dead_domains=recover_dead_domains,
        )
        result.rounds_run = 1
        _merge(outcome1)

        if result.candidates_validated >= remaining_target:
            result.reached_target = True
            result.stopped_reason = "target_reached"
            return result
        if result.candidates_examined >= ceiling:
            result.stopped_reason = "ceiling_reached"
            return result
        if outcome1.candidates_found == 0:
            result.stopped_reason = "no_new_candidates_found"
            return result

        # Round 2 -- role-word-broadened queries, remaining budget only.
        remaining = ceiling - result.candidates_examined
        outcome2 = self.discover(
            product, category=category, country=country, max_candidates=remaining,
            application=application, key_specifications=key_specifications,
            domain_tld_bias=domain_tld_bias, source="serpapi", extra_role_words=role_words,
            target_count=remaining_target - result.candidates_validated, progress_callback=_stamped_callback(2),
            recover_dead_domains=recover_dead_domains,
        )
        result.rounds_run = 2
        _merge(outcome2)

        if result.candidates_validated >= remaining_target:
            result.reached_target = True
            result.stopped_reason = "target_reached"
            return result
        if result.candidates_examined >= ceiling or not allow_llm_fallback:
            result.stopped_reason = "ceiling_reached"
            return result

        # Round 3 -- last resort, weaker provenance.
        remaining = ceiling - result.candidates_examined
        outcome3 = self.discover(
            product, category=category, country=country, max_candidates=remaining,
            source="llm", target_count=remaining_target - result.candidates_validated,
            progress_callback=_stamped_callback(3), recover_dead_domains=recover_dead_domains,
        )
        result.rounds_run = 3
        result.used_llm_fallback = True
        _merge(outcome3)
        result.reached_target = result.candidates_validated >= remaining_target
        result.stopped_reason = "target_reached" if result.reached_target else "budget_exhausted_after_llm_fallback"
        return result

    def _discover_1688(
        self, product: str, category: Optional[str], max_candidates: int, outcome: DiscoveryOutcome,
    ) -> DiscoveryOutcome:
        """Deliberately does NOT run candidates through
        CandidateValidator/SupplierMatcher the way serpapi/llm do, and
        creates no suppliers rows -- China1688Scraper's real output has
        no independent company-website field to validate against at all
        (see normalizers/china_1688_normalizer.py's own docstring: both
        URLs it returns, the product-detail page and the seller's 1688
        shop page, are 1688's own marketplace domains, shared across
        every seller on the platform -- the exact thing
        discovery.candidate_validator's marketplace-host gate exists to
        reject, not something to validate a company's identity against).
        Nor is it yet confirmed which raw field(s) distinguish a
        生产加工 (manufacturing) listing from a 经销批发
        (distribution/trading) one -- china_1688_normalizer.py's own
        confirmed field list (from a prior real run) covers company
        name/province/city/title/merchantSigns, not that distinction
        specifically. Fetches real data and preserves it as
        raw_source_data evidence (same discipline every other source
        follows), but stops there until a real run's actual field
        coverage is inspected and a trustworthy classification/storage
        design can be built on top of confirmed fields, not guessed ones.
        """
        try:
            results = self.china_1688_scraper.scrape(
                product, max_results=max_candidates, require_super_factory=False,
            )
        except Exception as e:
            logger.error("discovery: 1688 scrape failed for %r: %s", product, e)
            results = []

        for result in results:
            if not result.success:
                logger.warning("discovery: 1688 scraper returned an error result: %s", result.raw_data)
                continue
            raw = result.raw_data or {}
            outcome.raw_1688_listings.append(raw)
            self.repo.save_raw(
                source="china_1688", raw_data=raw,
                source_id=result.source_id or "", processing_status="pending",
            )

        outcome.candidates_found = len(outcome.raw_1688_listings)
        self.repo.record_discovery_run(
            product_query=product, category=category, country="China",
            candidates_found=outcome.candidates_found, candidates_validated=0,
            candidates_rejected=0, candidates_duplicate=0,
        )
        return outcome

    def _fire_progress(
        self, progress_callback: Optional[Callable[[DiscoveryProgressEvent], None]],
        *, domain: str, candidate_title: str, extracted_name: Optional[str],
        status: str, reason: str, round_examined: int, round_validated: int,
    ) -> None:
        """Shared by every exit path in _process_candidate. Callback
        exceptions are caught and logged, never abort discovery -- same
        discipline as sourcing/sourcing_agent.py's own on_progress
        handling."""
        if not progress_callback:
            return
        try:
            progress_callback(DiscoveryProgressEvent(
                domain=domain, candidate_title=candidate_title, extracted_name=extracted_name,
                status=status, reason=reason, badge=_classify_reason_badge(reason),
                round_examined=round_examined, round_validated=round_validated,
            ))
        except Exception as e:  # noqa: BLE001 -- a progress-reporting glitch must never abort discovery
            logger.warning("discovery: progress_callback failed: %s", e)

    def _process_candidate(
        self, candidate, product: str, country: Optional[str], outcome: DiscoveryOutcome,
        raw_source: str = "discovery",
        progress_callback: Optional[Callable[[DiscoveryProgressEvent], None]] = None,
        recover_dead_domains: bool = False,
        skip_soft_trader_signals: bool = False,
    ) -> None:
        outcome.candidates_examined += 1
        try:
            validation = self.candidate_validator.validate(
                candidate, product, skip_soft_trader_signals=skip_soft_trader_signals,
            )
        except Exception as e:  # noqa: BLE001 -- one candidate's failure must never abort the whole discovery run
            logger.error("discovery: validation failed for %s: %s", candidate.domain, e)
            self.repo.save_raw(
                source=raw_source,
                raw_data={"title": candidate.title, "link": candidate.link, "domain": candidate.domain},
                source_id=candidate.domain, processing_status="failed",
            )
            outcome.candidates_rejected += 1
            self._fire_progress(
                progress_callback, domain=candidate.domain, candidate_title=candidate.title,
                extracted_name=None, status="rejected", reason=f"validation raised an exception: {e}",
                round_examined=outcome.candidates_examined, round_validated=outcome.candidates_validated,
            )
            return

        is_dead_domain = self._record_validation_outcome(
            candidate, validation, product, country, outcome, raw_source, progress_callback,
        )

        # Recovery: ONLY for a dead/unreachable domain (never marketplace,
        # trader, name-mismatch, or term-missing -- those rejections are
        # correct as given). Opt-in (real per-candidate SerpAPI cost), and
        # explicitly one level deep -- recover_dead_domains=False on the
        # recursive call below so a recovered candidate that's ALSO dead
        # doesn't chain into a third search. The recovered candidate gets
        # its own raw_source_data row and progress event (via
        # _record_validation_outcome directly, NOT a recursive
        # _process_candidate call -- recover() already ran validate()
        # once internally to know it passed; calling _process_candidate
        # here would call validate() a SECOND time on the same candidate,
        # paying for the LLM call twice for nothing) rather than
        # overwriting the original's -- "full evidence trail either way"
        # applies to the original dead-domain attempt too, not just
        # accepted/rejected candidates. candidates_examined is bumped
        # manually here since _record_validation_outcome doesn't do that
        # itself (only _process_candidate's own top does, once per real
        # validate() call it makes).
        if is_dead_domain and recover_dead_domains:
            recovery = self.candidate_validator.recover(
                candidate.title, product, self.google_scraper, country=country,
            )
            if recovery is not None:
                outcome.candidates_examined += 1
                self._record_validation_outcome(
                    recovery.candidate, recovery, product, country, outcome, raw_source, progress_callback,
                )

    def _record_validation_outcome(
        self, candidate, validation, product: str, country: Optional[str],
        outcome: DiscoveryOutcome, raw_source: str,
        progress_callback: Optional[Callable[[DiscoveryProgressEvent], None]],
    ) -> bool:
        """Turns one real (candidate, ValidationResult) pair into
        raw_source_data + (if validated) resolve_and_store + a progress
        event -- the shared tail every candidate goes through exactly
        once, whether it's the original candidate or one found via
        recover() (_process_candidate calls this, never validate() a
        second time for the same result). Returns True if this candidate
        was rejected SPECIFICALLY for a dead/unreachable domain -- the
        only case recovery applies to (not marketplace/trader/name-
        mismatch/term-missing, which are correct as given).

        Website resolved = a fetch was actually attempted and didn't
        fail (validate() gates 2-3) -- reason-text-matched, since "the
        site fetched" isn't otherwise exposed on ValidationResult. A
        marketplace-host rejection (gate 2) never even attempts a
        fetch, so it must be excluded here too, same as an actual
        fetch failure -- otherwise a never-fetched candidate would be
        miscounted as resolved. Content matched = reached and passed
        the product-term gate (gate 6): `validation.validated` already
        covers a full success without any string-matching (the
        actually-contracted field, unlike reason's exact wording --
        callers/tests are free to use any reason text alongside
        validated=True); the trader-prefix check only exists for the
        one case that boolean can't distinguish -- a candidate that
        reached and passed gate 6 but was then rejected at gate 7
        (trader self-declaration)."""
        reason = validation.reason
        # Recovery-eligible: a real fetch never even succeeded. Deliberately
        # excludes REASON_MARKETPLACE_HOST_PREFIX -- a marketplace-host
        # rejection is a policy exclusion, not a wrong-URL problem; a fresh
        # search would just find the same (correctly excluded) marketplace
        # listing again.
        is_dead_domain = (
            reason.startswith(REASON_FETCH_EXCEPTION_PREFIX)
            or reason.startswith(REASON_FETCH_UNSUCCESSFUL_PREFIX)
            or reason == REASON_EMPTY_PAGE
        )
        website_did_not_resolve = is_dead_domain or reason.startswith(REASON_MARKETPLACE_HOST_PREFIX)
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
            self._fire_progress(
                progress_callback, domain=candidate.domain, candidate_title=candidate.title,
                extracted_name=validation.extracted_name, status="rejected", reason=validation.reason,
                round_examined=outcome.candidates_examined, round_validated=outcome.candidates_validated,
            )
            return is_dead_domain

        supplier_data = {
            "canonical_name": validation.extracted_name,
            # validation.resolved_domain, when set, is the domain a
            # same-company redirect actually landed on (see
            # candidate_validator.ValidationResult's own doc) --
            # candidate.domain alone would store the STALE pre-redirect
            # domain, breaking the exact-domain-match dedup tier against
            # an already-existing supplier under the real, current
            # domain. Found live: dexteraxle.com -> dextergroup.com
            # created a genuine duplicate golden record before this.
            "domain": validation.resolved_domain or candidate.domain,
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
            self._fire_progress(
                progress_callback, domain=candidate.domain, candidate_title=candidate.title,
                extracted_name=validation.extracted_name, status="rejected",
                reason=f"validated, but saving the candidate failed: {e}",
                round_examined=outcome.candidates_examined, round_validated=outcome.candidates_validated,
            )
            return False

        action = result.get("action")
        supplier_id = result.get("supplier_id") or result.get("new_supplier_id")
        self.repo.mark_raw_processed(raw_id, golden_record_id=supplier_id, status="processed")

        if action == "merged":
            # Auto-merged into an existing record -- no new row, real
            # dedup, matches config.settings.DEDUP_AUTO_MERGE_THRESHOLD's
            # own semantics exactly as SupplierMatcher already defines them.
            # Deliberately NOT counted in candidates_validated: a merge --
            # whether into a supplier that already existed before this run,
            # or one this SAME run already created in an earlier round --
            # is zero new distinct companies found. Found live: discover_to_
            # target()'s round 2 (role-word-broadened queries) can re-surface
            # a domain round 1 already validated and created; re-validating
            # it (a real second fetch+LLM call) and merging it back into
            # itself was inflating "N validated" past the true distinct-
            # company count -- a live run showed "8 validated" for only 4
            # actual distinct companies.
            outcome.candidates_duplicate += 1
            if supplier_id is not None:
                # May already be in new_supplier_ids (a merge into a
                # supplier THIS SAME run created in an earlier round --
                # see the comment above) -- callers combining this with
                # new_supplier_ids must dedupe, never assume disjoint.
                outcome.duplicate_supplier_ids.append(supplier_id)
        elif supplier_id is not None:
            # "created" or "review_queued" -- both genuinely create a new
            # row (create_golden_record), review_queued just also flags it
            # for human dedup review against a close-but-not-auto-merge match.
            # candidates_validated increments ONLY here, in lockstep with
            # new_supplier_ids -- see that field's own comment: "N
            # validated" must mean N distinct real companies this run
            # actually added.
            outcome.new_supplier_ids.append(supplier_id)
            outcome.candidates_validated += 1
            if action == "review_queued":
                outcome.review_queued_supplier_ids.append(supplier_id)

        self._fire_progress(
            progress_callback, domain=candidate.domain, candidate_title=candidate.title,
            extracted_name=validation.extracted_name,
            status="duplicate" if action == "merged" else "validated", reason=validation.reason,
            round_examined=outcome.candidates_examined, round_validated=outcome.candidates_validated,
        )
        return False

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

    def export_for_batch_upload(
        self, product: str, output_path: Optional[str] = None, discovery_source: str = "discovery_service",
    ) -> tuple[Path, int]:
        """CSV bridge from discover() to batch/: every supplier
        discover() has ever created for `product` (via
        self.repo.find_discovered_suppliers -- see that method's own
        docstring for why it reads persistent supplier-row state rather
        than pipeline_jobs/discovery_runs, neither of which reliably
        tracks a CLI-invoked run's created ids), written as a minimal
        "Company Name" / "Website" CSV -- exactly the two headers
        batch.csv_parser's fuzzy header matching recognises, so the
        output of this function is valid input to `main.py batch-upload`
        unchanged.

        Why this bridge exists at all: discover() only ever sets
        canonical_name/domain/country/product_keywords on a new
        supplier -- never address, phone, or email. Feeding the result
        back through batch-upload's existing enrichment path (real
        collection + grounded address extraction) is the smallest way
        to get the rest, reusing the exact same pipeline an external
        list (e.g. an exhibitor spreadsheet) already goes through,
        rather than building a second enrichment path for
        discovery-found suppliers specifically.

        `domain` is written bare (no scheme, e.g. "acme.com") -- exactly
        how discover() stored it -- rather than a full URL; batch's own
        collection.site_collector.SiteCollector already tries the
        https://www.X / https://X / http://www.X variants for a bare
        domain, so this needs no extra formatting here.

        Returns (path, count) -- count is 0 for an empty result (still
        writes a header-only CSV, same "never raise on nothing found"
        discipline every other export in this codebase follows).
        """
        suppliers = self.repo.find_discovered_suppliers(product, discovery_source=discovery_source)

        if output_path:
            path = Path(output_path)
        else:
            slug = re.sub(r"[^a-z0-9]+", "_", product.lower()).strip("_") or "suppliers"
            path = Path(f"discovered_{slug}.csv")
        path.parent.mkdir(parents=True, exist_ok=True)

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["Company Name", "Website"])
        for supplier in suppliers:
            writer.writerow([supplier.get("canonical_name") or "", supplier.get("domain") or ""])

        path.write_text(buffer.getvalue(), encoding="utf-8-sig", newline="")
        return path, len(suppliers)
