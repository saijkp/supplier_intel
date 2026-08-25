"""
discovery/candidate_validator.py

The only LLM call in Discovery Service -- and even here, the model only
ever READS a real fetched page, never generates a company from nothing.
For each candidate domain: fetch the real page, ask the LLM to extract
(only if actually present in the text) the company's self-stated name/
country, then cross-check that extracted name against the ORIGINAL
search result's title/snippet using the same rapidfuzz validation
scrapers.company_website_finder.CompanyWebsiteFinder.find_website()
already uses (reused via deduplication.name_utils.normalise_company_name,
not reimplemented).

A candidate is "validated" only if every gate passes:
1. Real SerpAPI search hit (candidate_extractor.py, upstream of this).
2. The candidate's domain is NOT a known B2B marketplace host
   (goldsupplier.com, alibaba.com, made-in-china.com, indiamart.com,
   tradeindia.com and subdomains thereof, e.g. en.alibaba.com --
   deduplication.domain_utils.PLATFORM_REGISTERED_DOMAINS) -- checked
   before any fetch. candidate_extractor.py already filters these out
   upstream for the serpapi path, but this is a deliberate second,
   independent gate here too: a marketplace storefront page routinely
   contains a real company name and mentions the searched product (it's
   advertising it), so without this check such a candidate could
   otherwise sail through gates 3-5 below and be stored as if it were
   the company's own independently-verified website. A supplier whose
   only web presence is a marketplace storefront is a negative
   manufacturer signal, not a valid company website -- flagged here,
   never passed as verified.
3. Real fetch of the candidate site succeeded.
4. The LLM found a company name explicitly stated in the page text
   (grounded-only prompt discipline -- same "quote-required, omit
   rather than infer" rules verification.capability_extractor.py's
   system prompt already established).
5. That extracted name fuzzy-matches the original search result
   (proves the fetched page is genuinely about the company the search
   surfaced, not an unrelated site that happens to share the domain).
6. The fetched page text actually mentions the searched product term
   -- a second, DETERMINISTIC keyword check, not another LLM call.
   Checks the CORE term only (a trailing "manufacturer"/"supplier"/
   "factory" qualifier -- query_builder.py's own templates always add
   one -- is stripped first, see _core_product_term), and is spelling-
   insensitive for known British/American variants (_SPELLING_VARIANTS,
   e.g. "moulding"/"molding") -- found via a real run that the strict
   original check was rejecting genuine manufacturers on wording
   alone, not a real signal they weren't manufacturers.

Gate 3.5 -- UK Companies House registration, opt-in via
`companies_house_client` (None by default -- every other product
category has no reason to pay this extra free API round-trip, and the
check itself only ever makes sense for a UK-scoped search). When
enabled, runs immediately after gate 3 (a real fetch already
succeeded) and BEFORE gate 4 (the one PAID call in this validator) --
found live on a "material handling equipment manufacturer" +
--country "United Kingdom" run: 4 of 7 discovery-validated candidates
(Indotherm, Elecon, DIPAOWANG, VEVOR) turned out to have no confirmed
UK registration at all once main.py verify-uk-company ran against
them afterward, each having already paid for an OpenAI call it didn't
need. Matches on cheap, deterministic guesses at the company name from
the raw SERP title (_title_name_candidates), NOT the gate-4 LLM
extraction -- that extraction is the very call this gate exists to
avoid paying for. Tries several candidate strings (the whole title,
plus every pipe/dash/colon-separated segment), not one single guess --
found live that a single "pick the longest segment" heuristic guessed
wrong often enough to reject two ALREADY Companies-House-CONFIRMED
suppliers from a real run (Toyota Material Handling UK, Permatt
Forklift Trucks). Also matches at a deliberately LOWER confidence bar
than uk_company_verification_service's own 85 (see
match_company_name_against_companies_house's `min_confidence` doc) --
this gate's failure modes are asymmetric: a false pass here just costs
one already-budgeted LLM call, a false reject silently discards a
candidate that already passed every other real signal. A supplier can
still fail Companies House verification later even after passing this
gate; that downstream check (main.py verify-uk-company, standing rule
9 in CLAUDE.md) remains the authoritative one.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from rapidfuzz import fuzz

from deduplication.domain_utils import PLATFORM_REGISTERED_DOMAINS, is_platform_subdomain
from deduplication.name_utils import normalise_company_name
from discovery.candidate_extractor import Candidate
from llm.client import LLMClient
from verification.uk_company_verification_service import match_company_name_against_companies_house

logger = logging.getLogger(__name__)

# Same threshold scrapers.company_website_finder's own
# _DEFAULT_MIN_NAME_SIMILARITY uses for the analogous "is this really
# the same company" question.
_NAME_MATCH_THRESHOLD = 55.0

# Mechanical, country-agnostic trader exclusion -- the codebase's only
# other trader signal (verification.manufacturer_verifier, via Qichacha
# business-scope data) only exists for China-registered companies, so a
# global "exclude traders/distributors/resellers" filter (sourcing.
# SourcingAgentService's own brief explicitly asks for this) needs a
# second signal that works for any country. Deliberately specific
# self-declarations, not the bare word "trading" -- a genuine
# manufacturer's page can easily mention "trading partners" or similar
# without being a trading company itself, so precision matters more
# than recall here: a false negative just means Qichacha/ManufacturerVerifier
# gets the final say later, a false positive silently drops a real
# manufacturer.
_TRADER_SELF_DECLARATION_PHRASES: tuple = (
    "we are a trading company",
    "we are a professional trading company",
    "we are a distributor",
    "we are a reseller",
    "we are an import and export company",
    "we are a sourcing agent",
    "we are a buying agent",
    "trading company specializing in",
    "we do not manufacture",
    "we don't manufacture",
)


def _find_trader_self_declaration(page_text: str) -> str | None:
    """The first matched phrase from _TRADER_SELF_DECLARATION_PHRASES
    found (case-insensitively) in `page_text`, or None."""
    haystack = page_text.lower()
    for phrase in _TRADER_SELF_DECLARATION_PHRASES:
        if phrase in haystack:
            return phrase
    return None


# query_builder.py's own templates append a trailing role word to every
# product term it builds a query from ("{product} manufacturer",
# "{product} supplier", "{product} factory") -- gate 6 originally
# required that exact tail to appear verbatim on a candidate's own
# page, but no real company writes its own homepage as "we are an
# injection moulding manufacturer" -- it was rejecting genuine
# manufacturers (accurateplastics.net, cadrex.com,
# usainjectionmolding.com, among others found via a real "injection
# moulding manufacturer" run) on that technicality alone, 71% of that
# run's rejections. Gate 6 now checks only the core phrase, this
# qualifier stripped.
_TRAILING_QUALIFIERS: tuple = ("manufacturer", "supplier", "factory")

# British/American spelling pairs confirmed to matter for a real
# candidate -- checked in both directions on the core phrase, since
# either the search term or the page could be written in either
# spelling. Deliberately not a general British/American dictionary --
# only pairs a real rejection has actually proven matter, added to as
# that happens rather than guessed in advance.
_SPELLING_VARIANTS: tuple = (("mould", "mold"),)


# Deliberately lower than uk_company_verification_service's
# _CLEAN_MATCH_THRESHOLD (85) -- see that constant's `min_confidence`
# doc for the asymmetric-risk reasoning. Reuses this file's own
# _NAME_MATCH_THRESHOLD (55.0) rather than inventing a second "is this
# plausible" number -- same bar candidate_validator already trusts for
# the analogous gate-5 question.
_UK_PREFILTER_MATCH_THRESHOLD = _NAME_MATCH_THRESHOLD


# recover()'s own corroboration check, on top of validate()'s existing
# gate 5 -- see recover()'s own docstring for why gate 5 alone isn't
# enough here. Found live: recovering "Apadrecoplastics" (a dead
# domain) matched cleanly onto adrecoplastics.co.uk, the real site of
# an unrelated company (Adreco Plastics, part of the STH Plastics
# Group) -- gate 5 only checks the extracted name against the SAME
# candidate's own SERP snippet (self-consistency), never against the
# ORIGINAL name recover() was asked to find. Tested plain rapidfuzz
# similarity (ratio/token_sort/token_set/partial_ratio) as a fix first:
# "Apadrecoplastics" vs "Adreco Plastics" scores 90.3 on all four --
# HIGHER than even uk_company_verification_service's own strict 85.0
# bar -- so raw fuzzy similarity cannot tell this pair apart from a
# genuine minor variant (e.g. "Beta Bearings Ltd" vs "Beta Bearing
# Ltd" scores similarly). A word-token check does: the two names share
# zero significant words after stripping corporate suffixes
# ("apadrecoplastics" is one mashed token; "adreco"/"plastics" are
# two, neither equal to it), while genuine variants keep at least one
# shared distinctive word.
_GENERIC_NAME_WORDS = frozenset({
    "ltd", "limited", "inc", "incorporated", "llc", "co", "company",
    "corp", "corporation", "group", "plc", "gmbh", "holdings",
    "international", "the", "and",
    # Industry-vocabulary, not legal suffixes -- same failure MODE as
    # the corporate suffixes above (a word so common within one product
    # category that sharing it proves nothing), found live in a second
    # real false match: recovering "Ability Handling" (a dead domain)
    # matched onto the real site of "Grant Handling" -- a completely
    # unrelated company -- because both names include "Handling", which
    # _shares_distinctive_token treated as proof of identity since it
    # wasn't on this list yet. A 2+-shared-words requirement was
    # considered and rejected as the general fix instead of a stoplist:
    # it would have regressed the ALREADY-passing "Beta Bearings Ltd"
    # vs "Beta Bearing Ltd" near-miss case above (verified empirically
    # -- those two names share exactly ONE distinctive word, "beta",
    # since "bearings"/"bearing" don't stem-match as exact tokens), so
    # it trades one false-accept risk for a guaranteed false-reject on
    # a case already proven to matter. Curated, not exhaustive --
    # extended as a real collision is found, same discipline as
    # _SPELLING_VARIANTS/_TRADER_SELF_DECLARATION_PHRASES elsewhere in
    # this module. Confirmed via the real Lift Truck category dataset
    # that produced the Ability/Grant Handling collision: "handling",
    # "forklift(s)", "truck(s)", "lift(s)" and "equipment" all recur
    # across multiple DISTINCT real companies; "plant"/"machinery"/
    # "material(s)" likewise. Plurals listed separately -- this
    # tokeniser does no stemming, so "forklift" and "forklifts" are
    # different tokens.
    "handling", "forklift", "forklifts", "truck", "trucks", "lift", "lifts",
    "equipment", "plant", "machinery", "material", "materials",
})

_UK_COUNTRY_SYNONYMS = frozenset({
    "uk", "united kingdom", "great britain", "england", "scotland",
    "wales", "northern ireland",
})


def _distinctive_tokens(name: str) -> set:
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    return {w for w in words if len(w) >= 4 and w not in _GENERIC_NAME_WORDS}


def _shares_distinctive_token(a: str, b: str) -> bool:
    """True if `a` and `b` share at least one significant word, OR
    either side has no significant words at all to compare (nothing
    distinctive means no basis for a rejection -- don't invent one
    from insufficient signal, same discipline as everywhere else in
    this module)."""
    tokens_a, tokens_b = _distinctive_tokens(a), _distinctive_tokens(b)
    if not tokens_a or not tokens_b:
        return True
    return bool(tokens_a & tokens_b)


def _countries_plausibly_match(a: str, b: str) -> bool:
    """Exact-or-UK-synonym match only, deliberately not fuzzy --
    country names are a small closed set (unlike company names), so
    fuzzy similarity isn't needed and would reintroduce the same
    unreliable-on-short-strings risk _shares_distinctive_token exists
    to avoid."""
    norm_a, norm_b = (a or "").strip().lower(), (b or "").strip().lower()
    if not norm_a or not norm_b:
        return True
    if norm_a == norm_b:
        return True
    return norm_a in _UK_COUNTRY_SYNONYMS and norm_b in _UK_COUNTRY_SYNONYMS


def _title_name_candidates(title: str) -> list[str]:
    """Every plausible company-name string worth trying against
    Companies House from a raw SERP title -- used ONLY for the pre-LLM
    gate (see validate()), never stored or treated as the grounded
    extracted name (gate 4's LLM call still does that properly). The
    pre-LLM gate can afford several free Companies House searches
    (unlike the one paid LLM call it exists to gate), so rather than
    guess a single segment -- found live to pick the WRONG one on a
    real title like "Material Handling Equipment Supplier - UK
    Forklifts & Trucks" (Permatt's own actual name isn't in either
    dash-split segment at all) -- this tries the whole title AND every
    pipe/dash/colon-separated segment, letting the caller keep
    whichever scores best. Order preserved, duplicates dropped."""
    title = (title or "").strip()
    if not title:
        return []
    segments = [p.strip() for p in re.split(r"\s*[|\-–—:]\s*", title) if p.strip()]
    seen: set = set()
    candidates = []
    for c in [title] + segments:
        if c not in seen:
            seen.add(c)
            candidates.append(c)
    return candidates


# Tokens the LLM has been observed to return as the literal string
# VALUE of a JSON field instead of an actual JSON null when it means
# "not stated" -- found live: Principle Fork Lifts Ltd's country field
# came back as the four-character string "null", which then got
# stored on the supplier row as if "null" were a real country. Not a
# guess at the real value either way -- coerced to None (blank), same
# as if the model had returned proper JSON null.
_LLM_NULL_STRINGS: frozenset = frozenset({"null", "none", "n/a", "na"})


def _clean_llm_string_field(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or stripped.lower() in _LLM_NULL_STRINGS:
        return None
    return stripped


def _core_product_term(product_term: str) -> str:
    """Strips one trailing qualifier word (see _TRAILING_QUALIFIERS)
    from `product_term` if present -- "injection moulding
    manufacturer" -> "injection moulding". A term that doesn't end in
    one of the known qualifiers (e.g. already just the core phrase) is
    returned unchanged."""
    words = product_term.strip().split()
    if len(words) > 1 and words[-1].lower() in _TRAILING_QUALIFIERS:
        return " ".join(words[:-1])
    return product_term


def _mentions_product_term(page_text: str, product_term: str) -> bool:
    """True if `page_text` mentions the core product term (see
    _core_product_term), spelling-insensitive for known British/
    American variants (see _SPELLING_VARIANTS)."""
    core = _core_product_term(product_term).lower()
    haystack = (page_text or "").lower()

    candidates = {core}
    for british, american in _SPELLING_VARIANTS:
        candidates.add(core.replace(british, american))
        candidates.add(core.replace(american, british))

    return any(candidate in haystack for candidate in candidates)


# Named, not just inline f-strings, so discovery_service.py can classify
# a ValidationResult's outcome (e.g. "did the website resolve" vs "did the
# content actually match") by checking which gate a candidate reached,
# without duplicating these strings as a separate hidden magic-string
# dependency in a different file. Gate order is fixed and documented in
# this module's own docstring -- reaching gate N's reason implies every
# earlier gate already passed.
REASON_MARKETPLACE_HOST_PREFIX = "candidate domain is a known B2B marketplace host"  # gate 2
REASON_FETCH_EXCEPTION_PREFIX = "fetch failed"                                   # gate 3 (raised)
REASON_FETCH_UNSUCCESSFUL_PREFIX = "could not fetch candidate site"              # gate 3 (no success/no pages)
REASON_EMPTY_PAGE = "fetched page had no readable text"                          # gate 3 (blank text)
REASON_TERM_MISSING_PREFIX = "fetched page text does not mention the searched term"  # gate 6
REASON_TRADER_PREFIX = "page self-identifies as a trading company/distributor"   # gate 7
REASON_SUCCESS_PREFIX = "validated: name corroborated"                           # every gate passed
REASON_UK_NOT_REGISTERED_PREFIX = "no confirmed active UK Companies House registration"  # gate 3.5, opt-in

# recover()'s own search-query exclusions -- general business-listing
# directories/aggregators, not the B2B sourcing marketplaces
# PLATFORM_REGISTERED_DOMAINS already covers (reused directly below,
# not duplicated). Found live: a small pilot recovering 5 real dead-
# domain candidates found only 1/5 -- the other 4's ACTUAL replacement
# domains were independently already known (found manually earlier the
# same night) but never appeared among the top search results at all;
# the log showed the validator spending real fetch attempts on
# machinerytrader.fr and zoominfo.com instead. Excluding these at the
# SEARCH level (Google's own -site: syntax) rather than post-filtering
# means a wasted slot in the (still-bounded) max_candidates window
# never gets spent on a listing that was never going to validate
# anyway -- more of the real search-result budget reaches genuine
# company-site candidates instead. Curated, not exhaustive; extended
# as a real listing site is found crowding out a real recovery, same
# discipline as _SPELLING_VARIANTS/_TRADER_SELF_DECLARATION_PHRASES.
_RECOVERY_DIRECTORY_EXCLUSIONS: tuple = (
    "yell.com", "zoominfo.com", "machinerytrader.com", "machinerytrader.fr",
    "thomasnet.com", "europages.com", "europages.co.uk", "kompass.com",
    "cylex-uk.co.uk", "thomsonlocal.com", "192.com", "scoot.co.uk",
    "checkatrade.com", "trustpilot.com", "facebook.com", "linkedin.com",
    "yelp.com", "indeed.com", "glassdoor.com", "crunchbase.com",
    "bloomberg.com", "opencorporates.com", "wikipedia.org", "dnb.com",
)
# Marketplace + directory exclusions combined -- the full -site: list
# recover()'s own search query applies. sorted(), not just tuple():
# PLATFORM_REGISTERED_DOMAINS is a set, whose iteration order isn't
# guaranteed stable across processes -- a non-deterministic query
# string would make recover()'s own query-construction tests flaky.
# PLATFORM_REGISTERED_DOMAINS is ALSO checked again downstream by
# validate()'s own gate 2 (defence in depth, same as validate() already
# does for the plain discover() path) -- this exclusion just means a
# validate() call is never wasted on one in the first place.
_RECOVERY_EXCLUDED_HOSTS: tuple = tuple(sorted(PLATFORM_REGISTERED_DOMAINS)) + _RECOVERY_DIRECTORY_EXCLUSIONS

SYSTEM_PROMPT = """You are reading the text of a company website. Extract ONLY what is explicitly stated in the text below -- never guess, infer, or fill in based on typical industry patterns or the domain name.

Rules, strictly enforced:
1. Only report a company name if it is explicitly stated in the text (e.g. in a heading, footer, "About Us" section, or copyright notice).
2. If the company name is not clearly stated, return null for company_name -- do not guess it from the domain or from context.
3. Only report a country if it is explicitly stated (an address, a phone country code mentioned as text, "based in X").
4. Never invent certifications, products, or history not present in the text -- this task only asks for name and country.

Return ONLY a JSON object with exactly these keys, no other text:
{
  "company_name": "the exact company name as stated in the text, or null if not clearly stated",
  "country": "the country as stated in the text, or null if not clearly stated"
}"""


@dataclass
class ValidationResult:
    candidate: Candidate
    validated: bool
    extracted_name: Optional[str]
    extracted_country: Optional[str]
    name_match_score: Optional[float]
    reason: str


class CandidateValidator:

    def __init__(
        self, website_fetcher: Any, llm_client: Optional[LLMClient] = None,
        companies_house_client: Optional[Any] = None,
    ):
        # Anything with `.fetch(domain) -> result with .success/.pages[0].text`
        # -- OwnWebsiteScraper or collection.SiteCollector both qualify,
        # same injectable seam the rest of this codebase already uses.
        self.website_fetcher = website_fetcher
        self.llm_client = llm_client or LLMClient()
        # None by default -- see module docstring's "Gate 3.5" section.
        # Deliberately NOT defaulted to a real CompaniesHouseClient()
        # the way llm_client is defaulted above: unlike the LLM client,
        # this gate must stay opt-in per call site (main.py discover
        # --require-uk-registration), not silently on for every product
        # category just because a key happens to be configured.
        self.companies_house_client = companies_house_client

    def validate(self, candidate: Candidate, product_term: str) -> ValidationResult:
        if is_platform_subdomain(candidate.domain):
            # Checked before any fetch -- a marketplace storefront is a
            # negative signal (this is not an independent company
            # website), not just "no signal yet." Deliberately still
            # checked here even though candidate_extractor.py already
            # filters these out upstream for the serpapi path: a
            # marketplace page routinely contains a real company name
            # and mentions the searched product (it's advertising it),
            # so without this gate such a candidate could otherwise pass
            # every check below and be stored as if it were the
            # company's own verified site.
            return ValidationResult(
                candidate, False, None, None, None,
                f"{REASON_MARKETPLACE_HOST_PREFIX}: {candidate.domain}",
            )

        try:
            fetch_result = self.website_fetcher.fetch(candidate.domain)
        except Exception as e:
            logger.warning("discovery: fetch failed for %s: %s", candidate.domain, e)
            return ValidationResult(candidate, False, None, None, None, f"{REASON_FETCH_EXCEPTION_PREFIX}: {e}")

        if not fetch_result.success or not fetch_result.pages:
            return ValidationResult(
                candidate, False, None, None, None,
                f"{REASON_FETCH_UNSUCCESSFUL_PREFIX}: {getattr(fetch_result, 'error', 'unknown error')}",
            )

        page_text = fetch_result.pages[0].text
        if not page_text or not page_text.strip():
            return ValidationResult(candidate, False, None, None, None, REASON_EMPTY_PAGE)

        if self.companies_house_client is not None:
            name_candidates = _title_name_candidates(candidate.title)
            best_ch_match = {"match_status": "no_clear_match", "confidence": None, "matched_title": None}
            best_ch_name = None
            for name_candidate in name_candidates:
                ch_match = match_company_name_against_companies_house(
                    name_candidate, self.companies_house_client, min_confidence=_UK_PREFILTER_MATCH_THRESHOLD,
                )
                if ch_match["match_status"] == "verified":
                    best_ch_match, best_ch_name = ch_match, name_candidate
                    break  # good enough -- stop spending free Companies House calls
                if (ch_match["confidence"] or -1) > (best_ch_match["confidence"] or -1):
                    best_ch_match, best_ch_name = ch_match, name_candidate

            if best_ch_match["match_status"] != "verified":
                detail = (
                    f" (best Companies House match across {len(name_candidates)} title-derived "
                    f"name guess(es): '{best_ch_match['matched_title']}', confidence={best_ch_match['confidence']}, "
                    f"status={best_ch_match['match_status']}, tried as '{best_ch_name}')"
                    if best_ch_match["matched_title"] else f" (tried {len(name_candidates)} title-derived name guess(es))"
                )
                return ValidationResult(
                    candidate, False, None, None, None,
                    f"{REASON_UK_NOT_REGISTERED_PREFIX}{detail}",
                )

        extracted = self.llm_client.complete_json(SYSTEM_PROMPT, f"Website page content:\n\n{page_text[:20_000]}")
        if not isinstance(extracted, dict):
            return ValidationResult(
                candidate, False, None, None, None, "LLM extraction failed or returned invalid JSON",
            )

        extracted_name = extracted.get("company_name")
        extracted_country = _clean_llm_string_field(extracted.get("country"))
        if not isinstance(extracted_name, str) or not extracted_name.strip():
            return ValidationResult(
                candidate, False, None, extracted_country, None, "no company name found in page text",
            )
        extracted_name = extracted_name.strip()

        haystack = f"{candidate.title} {candidate.snippet}".lower()
        normalised_extracted = normalise_company_name(extracted_name)
        score = fuzz.partial_ratio(normalised_extracted, haystack)
        if score < _NAME_MATCH_THRESHOLD:
            return ValidationResult(
                candidate, False, extracted_name, extracted_country, score,
                f"extracted name '{extracted_name}' does not match the original search result (score={score:.0f})",
            )

        if not _mentions_product_term(page_text, product_term):
            return ValidationResult(
                candidate, False, extracted_name, extracted_country, score,
                f"{REASON_TERM_MISSING_PREFIX} '{product_term}'",
            )

        self_declared_trader = _find_trader_self_declaration(page_text)
        if self_declared_trader:
            return ValidationResult(
                candidate, False, extracted_name, extracted_country, score,
                f"{REASON_TRADER_PREFIX} (matched phrase: "
                f"'{self_declared_trader}') -- excluded, not a manufacturer",
            )

        return ValidationResult(
            candidate, True, extracted_name, extracted_country, score,
            f"{REASON_SUCCESS_PREFIX} (score={score:.0f}), product term found on page",
        )

    def recover(
        self, company_name: str, product_term: str, google_scraper: Any,
        country: Optional[str] = None, max_candidates: int = 5,
        existing_country: Optional[str] = None,
    ) -> Optional[ValidationResult]:
        """Opt-in recovery for a candidate that failed validate() SPECIFICALLY
        because its domain was dead/unreachable -- NOT for a marketplace,
        trader, name-mismatch, or term-missing rejection, all of which are
        correct as given and have nothing to do with a wrong URL. Callers
        are responsible for checking that distinction before calling this
        (see discovery_service.py's own website_did_not_resolve predicate,
        which checks exactly REASON_FETCH_EXCEPTION_PREFIX/
        REASON_FETCH_UNSUCCESSFUL_PREFIX/REASON_EMPTY_PAGE).

        Deliberately does NOT reuse scrapers.company_website_finder.
        CompanyWebsiteFinder.find_website() -- that class bakes in its OWN
        separate, weaker validation (no LLM extraction, no product-term
        check, no trader check), so using it here would mean a recovered
        candidate gets checked by a DIFFERENT, less rigorous gate than
        every other candidate -- exactly the "special trust" this method
        must not grant. Instead: search once, then run each resulting
        candidate through THIS validator's own validate() -- the identical
        gate, zero shortcuts.

        validate() alone isn't sufficient here, though: its gate 5 only
        checks the candidate against ITS OWN search snippet
        (self-consistency), never against `company_name` -- the actual
        original identity recover() is trying to find. A real, self-
        consistent, unrelated company can and does pass that gate (see
        _shares_distinctive_token's own docstring for the live case that
        found this). Two additional checks run on top, in order, BEFORE
        a validated candidate is accepted:
        - _shares_distinctive_token(company_name, result.extracted_name):
          mandatory. Plain rapidfuzz similarity was tested and rejected
          as a fix (scores the known-bad pair HIGHER than the known-good
          near-miss pair) -- see that function's docstring.
        - _countries_plausibly_match(existing_country, result.extracted_country):
          only when a caller supplies `existing_country` (batch_service.py
          passes the supplier's on-file country when recovering an
          existing supplier's dead domain; discovery_service.py has no
          existing supplier yet, so this stays inactive there). A
          different, cheap, independent signal for a different failure
          class (same-name company in a different country) -- NOT a
          Companies House cross-check: that was tested too and rejected,
          since CH's own "no match" response for a genuinely-dead-domain
          company under recovery is indistinguishable from a
          genuinely-different company (it flagged the real Murray
          Plastics recovery as no_clear_match right alongside the actual
          false Apadrecoplastics match), so using it as a hard gate would
          trade the one false positive for a new false negative.

        Tries up to `max_candidates` search results (stopping at the
        first that validates AND corroborates), reusing discovery.
        candidate_extractor.extract_candidates() for the same
        marketplace/directory-host filtering + dedup discover() itself
        already relies on -- no new filtering logic. Returns the first
        passing ValidationResult, or None if nothing recovered (caller
        keeps the original dead-domain rejection as-is; this is a real
        per-call SerpAPI cost, so callers must gate this behind an
        explicit opt-in flag, never call it unconditionally).

        The search query itself excludes _RECOVERY_EXCLUDED_HOSTS via
        Google's own -site: syntax (marketplace hosts + general
        business-listing directories/aggregators -- yell.com,
        zoominfo.com, etc., see that constant's own docstring for the
        live pilot that found this mattering) -- a listing site was
        never going to validate anyway, so excluding it at the search
        level means the (still-bounded) max_candidates window is spent
        on candidates that could actually recover the real company,
        not wasted fetch/LLM attempts on a directory. max_candidates
        defaults to 5, up from an original 2 -- the same live pilot
        found 2 too tight to reach a real company site past several
        listing-site results for a common UK business name; max_results
        widened to match (15, from 10) so the wider candidate window
        has enough raw search results to draw from even after
        exclusions remove some."""
        from discovery.candidate_extractor import extract_candidates

        base_query = f'"{company_name}" {country}' if country else f'"{company_name}"'
        exclusions = " ".join(f"-site:{host}" for host in _RECOVERY_EXCLUDED_HOSTS)
        query = f"{base_query} {exclusions}"
        try:
            results = google_scraper.scrape(query, max_results=15)
        except Exception as e:
            logger.warning("discovery: recovery search failed for %r: %s", company_name, e)
            return None

        for candidate in extract_candidates(results)[:max_candidates]:
            result = self.validate(candidate, product_term)
            if not result.validated:
                continue
            if not _shares_distinctive_token(company_name, result.extracted_name):
                logger.info(
                    "discovery: recovery candidate %s (%r) rejected -- no distinctive word "
                    "overlap with original name %r", candidate.domain, result.extracted_name, company_name,
                )
                continue
            if existing_country and result.extracted_country and not _countries_plausibly_match(
                existing_country, result.extracted_country,
            ):
                logger.info(
                    "discovery: recovery candidate %s rejected -- extracted country %r doesn't "
                    "match on-file country %r", candidate.domain, result.extracted_country, existing_country,
                )
                continue
            return result
        return None
