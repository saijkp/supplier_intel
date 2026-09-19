"""
discovery/llm_candidate_source.py

A second candidate-generation source for DiscoveryService, alongside
the SerpAPI path (discovery/query_builder.py + scrapers/
google_search_scraper.py). Where that path only ever reads REAL search
results (see discovery/discovery_service.py's own module docstring),
this one asks gpt-4o-mini directly for manufacturer names it already
has confidence about -- a genuinely different, weaker kind of evidence,
since the model can be wrong or out of date about whether a company
still exists, still makes the product, or exists at all.

This is safe to add without weakening the pipeline's anti-hallucination
guarantee, because nothing this module proposes is ever trusted on its
own: every candidate it returns still goes through the exact same
discovery.candidate_validator.CandidateValidator gate every SerpAPI
candidate does -- real fetch, real page text, deterministic product-term
match, name corroboration against the LLM's own original claim, trader
exclusion. An LLM-proposed company whose "website" doesn't resolve, or
whose real page doesn't actually mention the product or doesn't
corroborate the claimed name, is rejected exactly like a bad SerpAPI
hit would be. See discovery/discovery_service.py for how DiscoveryService
wires this in as source="llm" (candidates provenance-tagged
"llm-discovery", not "discovery", so verification.scorer's provenance
dimension treats a candidate found this way as weaker than one
independently corroborated by a real search hit -- see
verification/scorer.py's SOURCE_QUALITY_WEIGHTS).

Runs several prompt variations per product (English and Mandarin, plus
a country-qualified variant when a country is given) and deduplicates
the results by domain, since any single prompt only ever returns a
narrow slice of what the model actually knows about a given product --
this sourcing domain skews heavily toward Chinese manufacturers (see
this codebase's own supplier data), and a Mandarin-phrased prompt
genuinely surfaces different recall from the same underlying model than
an English one does, not a translation of it.

Website lookup for a name-only proposal: rule 3 originally required the
model to state a website for every company, dropping any company it
couldn't also name a URL for -- a real recall loss found live on an
"agricultural equipment manufacturers UK" run, where the model could
confidently name real, well-known companies (e.g. Spearhead Machinery,
Kverneland) it had genuine knowledge of, without being equally
confident of their exact domain string. Rule 3 now allows `website:
null` for a company the model is still specifically confident exists;
find_candidates() then calls the SAME real search-and-validate lookup
(`scrapers.company_website_finder.CompanyWebsiteFinder.find_website`)
batch/static-list-import already uses for the analogous "we have a
name but no URL" problem, rather than reimplementing it. This is a
REAL extra SerpAPI search + HTTP fetch per name-only proposal, on top
of whatever this module already cost -- and per the module's own
opening paragraph, whatever domain this step finds is still just
another Candidate handed to CandidateValidator.validate(), the exact
same real fetch + grounded-extraction + product-term + trader gate
every other candidate (SerpAPI-sourced or LLM-sourced-with-a-known-
website) already goes through. GPT proposing a name -- with or without
a website -- is never trusted on its own either way.

SYSTEM_PROMPT's rule 5 was rewritten after a real, reproducible finding:
the original wording ("It is completely fine to return an empty list
... an empty list is far better than a guess") caused gpt-4o-mini at
temperature=0 to return an empty array even for well-documented terms
it demonstrably has real, correct knowledge of -- confirmed via direct
ablation (rules 1-4 alone reliably returned real companies like Dexter
Axle/AL-KO for "trailer axle"; adding the original rule 5 back flipped
the exact same prompt to "[]", discarding that same knowledge). Praising
the empty-list outcome as "far better than a guess" right before
generation appears to have anchored the model toward it as the safe
default, not just a permitted fallback for genuine uncertainty. The
rewritten rule keeps the same substantive constraint (no synthesizing a
plausible-sounding company from a pattern, no padding for its own sake)
without ranking silence above a real, confident answer. Verified
two-sided before shipping: well-documented terms (trailer axle,
structural adhesive) return real companies again; a genuinely
thin-knowledge/fabricated-sounding term (a made-up "nano-ceramic
coating" category) still returns an honest empty list, not a padded one
-- the fix moved the failure mode back to "sometimes includes a
reseller among real manufacturers" (etrailer.com alongside Dexter Axle/
Lippert for "trailer axle dust cap"), which is exactly what the
downstream trader-exclusion gate in candidate_validator.py exists to
catch -- this module was never responsible for the manufacturer-vs-
trader distinction, only for not inventing companies outright.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from deduplication.domain_utils import extract_domain
from discovery.candidate_extractor import Candidate
from llm.client import LLMClient
from scrapers.company_website_finder import _is_cloudflare_internal_path, _is_usable_candidate_domain

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a sourcing research assistant helping a procurement team find real manufacturers of a specific product.

Rules, strictly enforced:
1. Only name a company if you have real, specific confidence it exists and actually manufactures the given product. If you are not sure, or you are only guessing based on the industry in general, OMIT that company entirely -- do not include it "just in case."
2. Never invent a company name, website, or location. Every field must be something you actually know, not a plausible-sounding guess.
3. Include the company's real website if you know it. If you don't know its exact website but are still specifically confident the company exists and makes this product, include it anyway with "website" set to null -- do not omit a real, known company just because you're unsure of its exact URL; the website will be looked up separately.
4. Do not repeat the same company more than once in your answer.
5. Do not pad the list with uncertain or pattern-guessed companies just to have entries -- name only companies you are specifically, individually confident about. If a genuinely thin-knowledge product category leaves you with very few or zero such companies, that is an accurate answer; but always report every company you DO have real confidence in first.

Return ONLY a JSON array (no other text, no markdown fences) of objects with exactly these keys:
[
  {
    "company_name": "the company's real name",
    "website": "the company's real website domain or URL",
    "country": "the country it's based in, or null if unsure",
    "city": "the city it's based in, or null if unsure",
    "why_relevant": "one short sentence on why this company makes the product"
  }
]"""


@dataclass
class GenerationStats:
    """Funnel counters for the LLM candidate-generation step itself,
    before any candidate reaches discovery.candidate_validator --
    DiscoveryOutcome (discovery_service.py) tracks everything from
    validation onward; this covers what happens before that."""
    variations_run: int = 0
    raw_generated: int = 0             # every (name, website) pair the LLM returned, across all variations, before any filtering
    dropped_incomplete: int = 0        # missing/blank company_name (website is no longer required here -- see website_lookup_* below)
    dropped_unusable_domain: int = 0   # platform/social/directory domain, or a Cloudflare-internal path
    website_lookup_attempted: int = 0  # name-only proposals (no website given) that triggered a real CompanyWebsiteFinder search+fetch
    website_lookup_resolved: int = 0   # of those, how many found a validated real website
    dropped_website_not_found: int = 0  # name-only proposals where the lookup found nothing trustworthy
    deduplicated: int = 0              # unique candidates remaining after cross-variation domain dedup


class LLMCandidateSource:
    """Mirrors CandidateValidator's own injectable-LLMClient convention
    exactly (`llm_client: Optional[LLMClient] = None`)."""

    def __init__(self, llm_client: Optional[LLMClient] = None, website_finder: Optional[Any] = None):
        self.llm_client = llm_client or LLMClient()
        # Only needed for a name-only proposal (see SYSTEM_PROMPT's rule
        # 3 and this module's own docstring) -- lazily constructed like
        # every other collaborator in this codebase, safe to build
        # without credentials, never touched otherwise. Real cost:
        # scrapers.company_website_finder.CompanyWebsiteFinder.find_website
        # is one real SerpAPI search + one real HTTP fetch, only spent
        # when the model itself couldn't already name a website.
        #
        # Deferred to the `website_finder` property below, NOT built
        # here -- GoogleSearchScraper()/OwnWebsiteScraper() each carry a
        # real ~0.2-0.4s construction cost of their own (an httpx client
        # setup, not a network call), and DiscoveryService.__init__
        # unconditionally builds one LLMCandidateSource() by default.
        # Building it eagerly here was a real, confirmed regression:
        # every DiscoveryService construction that doesn't override
        # llm_candidate_source (the vast majority of this codebase's own
        # discovery_service tests) paid that cost even when source="llm"
        # is never used, adding ~35s to tests/test_discovery_service.py
        # alone. Lazy construction on first actual use keeps every
        # existing caller's cost unchanged.
        self._website_finder = website_finder

    @property
    def website_finder(self) -> Any:
        if self._website_finder is None:
            from scrapers.company_website_finder import CompanyWebsiteFinder
            from scrapers.google_search_scraper import GoogleSearchScraper
            from scrapers.own_website_scraper import OwnWebsiteScraper

            self._website_finder = CompanyWebsiteFinder(
                google_scraper=GoogleSearchScraper(), own_website_scraper=OwnWebsiteScraper(),
            )
        return self._website_finder

    def _build_prompt_variations(self, product: str, country: Optional[str]) -> List[str]:
        """Mechanical string building, no LLM call here -- mirrors
        discovery.query_builder.build_queries's own "purely mechanical,
        the LLM only ever answers, never decides what to ask" split."""
        variations = [
            f'List real, actual manufacturer companies that make "{product}". '
            f"Only include companies you have real confidence actually exist and make this product.",
        ]
        if country:
            variations.append(
                f'List real, actual manufacturer companies based in {country} that make "{product}". '
                f"Only include companies you have real confidence actually exist and make this product."
            )
        variations.append(
            f'列出实际生产"{product}"的真实制造商公司。只列出你确实有把握真实存在并生产该产品的公司，不要猜测。'
        )
        if country:
            variations.append(
                f'列出位于{country}、实际生产"{product}"的真实制造商公司。只列出你确实有把握真实存在并生产该产品的公司，不要猜测。'
            )
        return variations

    def find_candidates(
        self, product: str, country: Optional[str] = None, max_candidates: int = 20,
    ) -> Tuple[List[Candidate], GenerationStats]:
        stats = GenerationStats()
        prompt_variations = self._build_prompt_variations(product, country)
        stats.variations_run = len(prompt_variations)

        seen_domains: set = set()
        candidates: List[Candidate] = []

        for user_prompt in prompt_variations:
            if len(candidates) >= max_candidates:
                break
            try:
                response = self.llm_client.complete_json(SYSTEM_PROMPT, user_prompt)
            except Exception as e:  # noqa: BLE001 -- one bad variation must never abort the whole discovery run
                logger.error(
                    "llm_candidate_source: generation failed for variation %r: %s", user_prompt[:80], e,
                )
                continue

            if not isinstance(response, list):
                if response is not None:
                    logger.warning(
                        "llm_candidate_source: expected a JSON array, got %s -- skipping this variation",
                        type(response).__name__,
                    )
                continue

            for item in response:
                if len(candidates) >= max_candidates:
                    break
                if not isinstance(item, dict):
                    continue
                stats.raw_generated += 1

                company_name = item.get("company_name")
                if not isinstance(company_name, str) or not company_name.strip():
                    stats.dropped_incomplete += 1
                    continue
                company_name = company_name.strip()

                website = item.get("website")
                link = website.strip() if isinstance(website, str) and website.strip() else None

                if link is None:
                    # Name-only proposal (SYSTEM_PROMPT's rule 3) --
                    # look up a real website the same way batch/static-
                    # list-import already does for a name with no URL,
                    # rather than dropping a company the model is
                    # otherwise specifically confident about. Never
                    # trusted on its own either: find_website() already
                    # does its own real fetch + fuzzy name-match
                    # validation, and whatever it finds still goes
                    # through the exact same CandidateValidator.validate()
                    # gate every other candidate does, below.
                    stats.website_lookup_attempted += 1
                    try:
                        finding = self.website_finder.find_website(company_name, country=country)
                    except Exception as e:  # noqa: BLE001 -- one bad lookup must never abort the whole generation pass
                        logger.warning(
                            "llm_candidate_source: website lookup failed for %r: %s", company_name, e,
                        )
                        stats.dropped_website_not_found += 1
                        continue
                    if not finding.validated or not finding.domain:
                        stats.dropped_website_not_found += 1
                        continue
                    stats.website_lookup_resolved += 1
                    link = finding.candidate_url or finding.domain
                    domain = finding.domain
                else:
                    if _is_cloudflare_internal_path(link):
                        stats.dropped_unusable_domain += 1
                        continue
                    domain = extract_domain(link)
                    if not _is_usable_candidate_domain(domain):
                        stats.dropped_unusable_domain += 1
                        continue

                if domain in seen_domains:
                    continue
                seen_domains.add(domain)

                why_relevant = item.get("why_relevant")
                candidates.append(Candidate(
                    title=company_name,
                    link=link,
                    snippet=why_relevant.strip() if isinstance(why_relevant, str) else "",
                    domain=domain,
                ))

        stats.deduplicated = len(candidates)
        return candidates, stats
