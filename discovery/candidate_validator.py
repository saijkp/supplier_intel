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
   alone, not a real signal they weren't manufacturers. Default check
   (see _significant_words_all_match) is word-level, not phrase-level:
   every significant word of the term (stopwords like "and"/"for"
   excluded) must appear somewhere on the page, in any order, with a
   small curated per-word synonym table (_WORD_SYNONYMS) for a handful
   of known head-noun substitutions (e.g. "axle" also accepts
   "suspension"/"running gear" -- a real axle-LESS trailer-suspension
   manufacturer can never say the literal word "axle" about its own
   product). _PRODUCT_TERM_SYNONYM_PHRASES's curated exact-phrase/
   word-pair overrides are tried first and still apply unchanged --
   the word-level check is a strictly additive fallback under them,
   not a replacement.

   If the homepage doesn't mention the term, a second fetch is tried
   before rejecting -- the exact URL the search result itself pointed
   at (Candidate.link), when that's a page deeper than the domain root
   (see _fetch_fallback_page_text). Found live: a niche accessory
   category routinely isn't advertised on a homepage even when the
   company clearly makes/stocks it one click deeper -- Trailer
   Engineering's homepage is all bowsers/tankers/generators, no
   "mudguard" anywhere, but the search hit itself
   (.../product/13-plastic-mudguard-single-axle/) is a real, live
   product page for exactly that product; Trailer Stuff's homepage
   foregrounds wheel clamps, but its Mudguards category page (32 real
   products) is exactly what the search surfaced. Gate 6 previously
   only ever fetched the domain root, so both were rejected as false
   non-matches. The deeper fetch reuses the same retry machinery
   (_fetch_candidate_site) and is passed through parking_page_reason()
   before being trusted (CLAUDE.md standing rule 7) -- a parking/for-
   sale/server-default page at that URL must never satisfy this gate.
   When the fallback succeeds, its text is ADDED to the homepage text
   (never replaces it), so the trader self-declaration/soft-signal
   checks below still see everything the homepage already offered.

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
from urllib.parse import urlparse

from rapidfuzz import fuzz

from deduplication.domain_utils import (
    PLATFORM_REGISTERED_DOMAINS,
    domains_match,
    extract_domain,
    is_platform_subdomain,
)
from deduplication.name_utils import (
    _distinctive_tokens,
    _shares_distinctive_token,
    normalise_company_name,
)
from discovery.candidate_extractor import Candidate
from llm.client import LLMClient
from llm.prompts import GROUNDED_COMPANY_NAME_EXTRACTION_SYSTEM_PROMPT
from verification.uk_company_verification_service import match_company_name_against_companies_house
from verification.website_contact_extractor import parking_page_reason

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


# Soft/indirect trader signals -- regex, not exact-phrase -- added after
# a real brake-cable-batch failure: none of Auto & Trailer Spares
# (autoandtrailer.com -- "expanded to become Irelands largest trailer
# parts distributor"; "Are you a Retailer, Wholesaler or Manufacturer?
# ... apply for a trade account") or Towing and Trailers
# (towingandtrailers.com -- "One of UK's largest stockists of
# Trailer-Parts"; "This range of parts covers trailers by most
# manufacturers") tripped _TRADER_SELF_DECLARATION_PHRASES above -- both
# are real, buyer-confirmed FAILs in that batch's own tracker, neither
# ever writes the literal sentence "we are a distributor". Confirmed
# live against both real fetched pages before adding these patterns
# (not written from guesswork), and re-checked against the SAME
# regression fixtures TestSelfDeclaredTraderExclusion already proves
# matter ("trading partners" in passing, a genuine in-house
# manufacturer) to confirm no new false positive there.
#
# Same precision-over-recall discipline as the phrase list above: every
# pattern requires the page positioning ITSELF via a reseller-only noun
# (distributor/stockist/wholesaler -- never bare "supplier", which a
# genuine manufacturer routinely uses for its own output) or explicitly
# admitting it carries OTHER manufacturers' output or trade-customer
# structure -- never a bare mention of "distributor" alone, which a
# genuine manufacturer can use describing its own downstream channel
# ("sold through our network of distributors") without being one.
#
# A third real example (Custom Control Cables, cccables.com -- "F.A.S.T.
# distributor" as a standalone trust-badge heading) is a real miss this
# set does NOT catch: a single ambiguous example (an unclear brand/
# program name, no "authorized"/"official" qualifier) isn't enough to
# write a reliable general pattern from yet -- left for a future
# collision, same discipline as everywhere else in this module.
_TRADER_SOFT_SIGNAL_PATTERNS: tuple = (
    # "become Ireland's largest trailer parts distributor"; "One of
    # UK's largest stockists of Trailer-Parts" -- self-positioning via
    # a reseller-only noun.
    re.compile(
        r"\b(largest|leading)\b(?:\s+\S+){0,4}?\s+(distributors?|stockists?|wholesalers?)\b",
        re.I | re.DOTALL,
    ),
    # "we supply products from leading manufacturers"; "covers trailers
    # by most manufacturers" -- explicitly carrying THIRD-PARTY
    # manufacturers' output, not its own.
    re.compile(
        r"\b(from|by)\s+(most|all|leading|top|major|various|multiple|other)\s+manufacturers\b",
        re.I | re.DOTALL,
    ),
    # "authorized distributor of Knott"; "official dealer for..." --
    # explicit branded-distributor/dealer badge language. Deliberately
    # SINGULAR only ("distributor", not "distributors") -- a genuine
    # manufacturer describing its OWN downstream channel almost always
    # uses the plural ("sold through our network of authorized
    # distributors worldwide"), while a self-declaration is almost
    # always singular ("we are AN authorized distributor of X").
    # Confirmed live: the plural form false-positived on exactly that
    # manufacturer-channel phrasing before this was narrowed.
    re.compile(
        r"\b(authou?rized|official|certified)\s+(distributor|dealer|reseller|stockist)\b",
        re.I | re.DOTALL,
    ),
    # "Are you a Retailer, Wholesaler or Manufacturer? ... apply for a
    # trade account" -- structural wholesale-to-trade page: the site is
    # selling AT TRADE PRICES to other retailers/wholesalers (and
    # sometimes manufacturers), not manufacturing itself.
    re.compile(
        r"trade\s+accounts?.{0,80}(retailer|wholesaler)|(retailer|wholesaler).{0,80}trade\s+accounts?",
        re.I | re.DOTALL,
    ),
)


def _find_trader_soft_signal(page_text: str) -> str | None:
    """The first _TRADER_SOFT_SIGNAL_PATTERNS regex match found in
    `page_text` (the matched excerpt itself, for the same kind of
    reviewable REASON_TRADER_PREFIX detail _find_trader_self_declaration
    provides), or None."""
    for pattern in _TRADER_SOFT_SIGNAL_PATTERNS:
        match = pattern.search(page_text)
        if match:
            return match.group(0)
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

# Curated multi-word synonym phrases for a core term -- same discipline
# as _SPELLING_VARIANTS (only pairs a real rejection has actually
# proven matter, not a general synonym dictionary guessed in advance).
# Found live via two real, genuine Material Handling manufacturers
# rejected on "material handling equipment" wording alone: Interroll
# (a real conveyor/warehouse-logistics manufacturer) says "material
# handling" on its own page, never "equipment" attached; Mercia
# Lifting Gear (a real lifting-equipment manufacturer) says "handling
# equipment", never "material" attached -- neither ever uses the full
# compound phrase verbatim, the same "no real company repeats the
# exact query phrasing" pattern _TRAILING_QUALIFIERS already
# established for a trailing role word, just for an interior word here.
# A candidate entry's value can mix bare strings (any one is a match on
# its own) with tuples (every term in the tuple must be present -- an
# AND, for a case where no single loose term is distinctive enough by
# itself). Real case: "metal jacks and propstand" candidates split into
# two vocabularies that are never written together -- "prop"-family
# wording (adjustable prop, scaffolding prop, prop jack) on formwork/
# scaffolding-props sites (nicesteel.shop, baolaisteel.com,
# wm-scaffold.com, lianggongformwork.com), and bare "jack" wording
# (base jack, levelling jack, universal jack) on scaffolding-hardware
# sites that never say "prop" at all (aresscaffolding.com,
# acescaffolduae.com) -- the ("prop", "jack") tuple exists for a page
# that uses both words but never as one of the fixed multi-word phrases
# above (e.g. "prop and jack sales"), not for the bare-"jack"-only
# sites, which match on "metal jack"/"steel jack" instead.
_PRODUCT_TERM_SYNONYM_PHRASES: dict = {
    "material handling equipment": ("material handling", "handling equipment"),
    "metal jacks and propstand": (
        "prop jack", "propstand", "prop stand", "adjustable prop", "scaffolding prop",
        "metal jack", "steel jack", "levelling jack", "leveling jack", "base jack",
        ("prop", "jack"),
    ),
}

# Generalisation of the pattern _PRODUCT_TERM_SYNONYM_PHRASES exists
# for: instead of a NEW curated phrase-tuple entry per full product
# term (which only ever helps the exact term it was written for),
# _significant_words_all_match below treats "no real company repeats
# the exact query phrasing" as the DEFAULT expectation for every
# category, not a per-category exception -- built once after the same
# gap showed up on a second, unrelated category (Material Handling:
# neither Interroll nor Mercia Lifting Gear used the compound phrase;
# trailer axle: Timbren Industries sells axle-LESS suspension systems,
# so "trailer axle" as a literal phrase can never appear on their own
# site even though they are a genuine, obvious manufacturer in this
# exact category).
#
# Keyed by individual WORD, not full phrase -- "axle" now carries its
# synonym set into ANY product term containing that word ("trailer
# axle", "leaf spring axle", etc.), not just one exact historical
# query string. A word with no entry here is still required literally
# (see _significant_words_all_match) -- this is a curated ADDITION to
# precision-limiting cases actually found live, same discipline as
# _SPELLING_VARIANTS, not a general thesaurus guessed in advance.
_WORD_SYNONYMS: dict = {
    "axle": ("axle", "suspension", "running gear", "running-gear"),
    "handling": ("handling", "lifting"),
}

# Small, conservative -- conjunctions/prepositions/articles only, never
# a word that could carry real product-identifying meaning on its own.
_STOPWORDS: frozenset = frozenset({
    "and", "or", "for", "of", "with", "the", "a", "an", "to", "&",
})


def _significant_words(term: str) -> list[str]:
    """Lowercase word tokens from `term` with _STOPWORDS removed --
    "metal jacks and propstand" -> ["metal", "jacks", "propstand"].
    Order is not semantically meaningful here (see
    _significant_words_all_match, which checks presence independently,
    never a contiguous phrase)."""
    return [w for w in re.findall(r"[a-z0-9]+", term.lower()) if w not in _STOPWORDS]


def _word_matches_haystack(word: str, haystack: str) -> bool:
    """True if `word` -- or a simple singular/plural fold of it, or one
    of its curated synonyms (see _WORD_SYNONYMS) -- appears anywhere in
    `haystack`. The plural fold is deliberately naive (just a trailing
    "s", plus the "y"/"ies" pattern -- "assembly"/"assemblies" -- not a
    real lemmatiser) -- enough to cover "jacks" on the query side
    matching "jack" on a real page and vice versa, the exact real
    mismatch _PRODUCT_TERM_SYNONYM_PHRASES's "metal jacks and
    propstand" entry originally had to spell out by hand."""
    forms = {word}
    if word.endswith("ies"):
        forms.add(f"{word[:-3]}y")
    elif word.endswith("y"):
        forms.add(f"{word[:-1]}ies")
    if word.endswith("s"):
        forms.add(word[:-1])
    else:
        forms.add(f"{word}s")
    forms.update(_WORD_SYNONYMS.get(word, ()))
    return any(form in haystack for form in forms)


def _significant_words_all_match(product_term: str, page_text: str) -> bool:
    """The generalised default for gate 6: every significant word of
    the (qualifier-stripped) product term must independently appear
    somewhere on the page, in any order -- never a contiguous-phrase
    requirement. Purely ADDITIVE to _mentions_product_term's existing
    exact-phrase/curated-tuple checks (an OR, tried after them) -- can
    only ever accept a real candidate those stricter checks would have
    rejected, never reject one they would have accepted, so this
    cannot regress either of the two already-proven curated cases."""
    words = _significant_words(_core_product_term(product_term))
    if not words:
        return False
    haystack = (page_text or "").lower()
    return all(_word_matches_haystack(word, haystack) for word in words)


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
#
# _GENERIC_NAME_WORDS/_distinctive_tokens/_shares_distinctive_token now
# live in deduplication/name_utils.py (imported above) so
# scrapers/company_website_finder.py can reuse the exact same
# discipline instead of reimplementing it -- see that module's own
# names_plausibly_corroborate for the CompanyWebsiteFinder-specific
# false-match class ("IK Eng Ltd" -> easydigitalfiling.com) this check
# alone doesn't cover.

_UK_COUNTRY_SYNONYMS = frozenset({
    "uk", "united kingdom", "great britain", "england", "scotland",
    "wales", "northern ireland",
})


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
    American variants (see _SPELLING_VARIANTS) and matching a curated
    multi-word synonym phrase (see _PRODUCT_TERM_SYNONYM_PHRASES) when
    one is known for this exact core term. A synonym entry may be a
    tuple instead of a bare string -- every term in that tuple must be
    present (an AND), for a synonym too generic to trust alone (see
    _PRODUCT_TERM_SYNONYM_PHRASES's own doc)."""
    core = _core_product_term(product_term).lower()
    haystack = (page_text or "").lower()

    candidates = {core}
    for british, american in _SPELLING_VARIANTS:
        candidates.add(core.replace(british, american))
        candidates.add(core.replace(american, british))
    candidates.update(_PRODUCT_TERM_SYNONYM_PHRASES.get(core, ()))

    for candidate in candidates:
        if isinstance(candidate, tuple):
            if all(term in haystack for term in candidate):
                return True
        elif candidate in haystack:
            return True

    # Generalised default fallback -- see _significant_words_all_match's
    # own docstring for why this is additive, not a replacement.
    return _significant_words_all_match(product_term, page_text)


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
REASON_OFF_DOMAIN_REDIRECT_PREFIX = "candidate domain redirected off-domain"     # gate 3.6
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

# Moved to llm/prompts.py so scrapers/company_website_finder.py can
# reuse the exact same grounded-extraction prompt without a circular
# import (see that module's own docstring). SYSTEM_PROMPT kept as a
# module-level alias since every existing call site here already
# refers to it by that name.
SYSTEM_PROMPT = GROUNDED_COMPANY_NAME_EXTRACTION_SYSTEM_PROMPT


@dataclass
class ValidationResult:
    candidate: Candidate
    validated: bool
    extracted_name: Optional[str]
    extracted_country: Optional[str]
    name_match_score: Optional[float]
    reason: str
    # Set ONLY when gate 3.6 allowed a same-company domain migration
    # through (see validate()'s own gate 3.6 doc) -- the domain the
    # candidate's own redirect actually landed on (dextergroup.com),
    # not the one originally searched for (dexteraxle.com). None means
    # "no redirect, use candidate.domain as-is," the same as every
    # caller already did before this field existed. Found live: without
    # this, a validated redirect candidate got its golden record stored
    # under the STALE pre-redirect domain, which then broke the exact-
    # domain-match dedup tier against an already-existing supplier under
    # the real, current domain -- a genuine duplicate (two "Dexter
    # Group" golden records, one per domain string). Callers building a
    # supplier record from a validated result must use
    # `resolved_domain or candidate.domain`, never `candidate.domain`
    # alone.
    resolved_domain: Optional[str] = None


class CandidateValidator:

    def __init__(
        self, website_fetcher: Any, llm_client: Optional[LLMClient] = None,
        companies_house_client: Optional[Any] = None,
        playwright_fetcher: Optional[Any] = None,
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
        # Same `.fetch(domain)` interface as website_fetcher above --
        # None (default for a test/fake-only caller) means no retry
        # happens at all, same "constructible without the real thing,
        # opt-in only when actually configured" contract as
        # companies_house_client. Production callers (discovery_service.py,
        # batch_service.py) default this to a real
        # scrapers.playwright_website_scraper.PlaywrightWebsiteScraper()
        # so the retry below is the STANDARD path, not a manually-gated
        # extra step -- see _fetch_candidate_site's own docstring for why.
        self.playwright_fetcher = playwright_fetcher

    def _fetch_candidate_site(self, domain: str) -> tuple[Optional[Any], Optional[str]]:
        """Returns (fetch_result, None) on success, or (None, reason)
        on failure. Tries `website_fetcher` (cheap httpx) first; if it
        fails for exactly the "unreachable"/"no readable text" reasons
        (an exception, no success/no pages, or blank page text) AND a
        `playwright_fetcher` is configured, retries the SAME domain via
        a real headless browser before giving up.

        Found live: several large, obviously-real trailer-axle
        manufacturers (Lippert, across all three of its own domain
        variants, and Dexter Axle/Group) were being lost entirely to
        httpx-level fetch failures -- a bot-challenge page, a WAF
        blocking httpx's own User-Agent/TLS fingerprint, or a JS-only
        page that renders its real content client-side, none of which
        say anything about whether the company is real. A real browser
        routinely gets past exactly this class of failure.

        Deliberately NOT the same thing as recover() below -- this is
        always the SAME domain, just a heavier fetch method; recover()
        searches for a DIFFERENT domain entirely, for when the original
        one is genuinely gone. This retry runs first, automatically,
        for every candidate reaching this gate (not opt-in like
        recover()) -- see playwright_fetcher's own constructor doc for
        why production defaults it to a real fetcher rather than None.

        Preserves the exact REASON_FETCH_EXCEPTION_PREFIX/
        REASON_FETCH_UNSUCCESSFUL_PREFIX/REASON_EMPTY_PAGE reason
        strings on a final failure (the original httpx attempt's
        reason, not the retry's) so discovery_service.py's own
        is_dead_domain/website_did_not_resolve classification -- and
        every existing test asserting on those exact prefixes --
        continues to work unchanged."""
        try:
            primary = self.website_fetcher.fetch(domain)
            primary_reason: Optional[str] = None
        except Exception as e:
            primary = None
            primary_reason = f"{REASON_FETCH_EXCEPTION_PREFIX}: {e}"

        if primary is not None:
            if not primary.success or not primary.pages:
                primary_reason = (
                    f"{REASON_FETCH_UNSUCCESSFUL_PREFIX}: {getattr(primary, 'error', 'unknown error')}"
                )
            elif not (primary.pages[0].text or "").strip():
                primary_reason = REASON_EMPTY_PAGE
            else:
                return primary, None  # httpx fetch succeeded with real text -- no retry needed

        if self.playwright_fetcher is None:
            return None, primary_reason

        logger.info(
            "discovery: retrying %s via Playwright after httpx-level failure (%s)", domain, primary_reason,
        )
        try:
            retry = self.playwright_fetcher.fetch(domain)
        except Exception as e:
            logger.warning("discovery: playwright retry failed for %s: %s", domain, e)
            return None, primary_reason
        if not retry.success or not retry.pages or not (retry.pages[0].text or "").strip():
            return None, primary_reason
        return retry, None

    def _fetch_fallback_page_text(self, candidate: Candidate) -> Optional[str]:
        """Gate 6's second attempt (see the module docstring's "If the
        homepage doesn't mention the term" paragraph): fetches the
        EXACT URL the search result pointed at (`candidate.link`), not
        just the domain root, when that URL is a real deeper page (a
        bare "/" or empty path means the search result already WAS the
        homepage -- nothing new to try, so this returns None without
        an extra fetch).

        Reuses `_fetch_candidate_site` (same httpx-then-Playwright retry
        every other fetch in this class gets) and `parking_page_reason`
        -- the same shared parking/for-sale/server-default check every
        other extraction stage in this codebase runs before trusting
        ANY field from a page (CLAUDE.md standing rule 7). A parking
        page at the deeper URL must never be allowed to satisfy this
        gate just because it happens to 200.

        Returns the fetched page's text, or None if there was nothing
        deeper to try, the fetch failed, the page had no readable text,
        or it turned out to be a parking page."""
        parsed = urlparse(candidate.link)
        if not parsed.path or parsed.path == "/":
            return None

        fetch_result, _ = self._fetch_candidate_site(candidate.link)
        if fetch_result is None or not fetch_result.pages:
            return None
        text = fetch_result.pages[0].text
        if not (text or "").strip():
            return None
        if parking_page_reason(text):
            return None
        return text

    def validate(
        self, candidate: Candidate, product_term: str, skip_soft_trader_signals: bool = False,
    ) -> ValidationResult:
        """`skip_soft_trader_signals`, when True, skips ONLY
        _TRADER_SOFT_SIGNAL_PATTERNS (gate 7b) -- _TRADER_SELF_DECLARATION_PHRASES
        (gate 7a: "we are a distributor", "we do not manufacture", etc.)
        and every other gate still apply unchanged. Default False for
        every existing caller (serpapi/llm discovery, recover()) --
        opt-in only for discovery.companies_house_sic_source.py's
        Material Handling candidates, where a real, multi-brand dealer
        with its own depot/service operation IS the wanted supplier
        type, not a disqualifying signal: this category's own confirmed
        roster (data/source_files/material_handling_14/) was built on
        UK Companies House registration alone, and real ground truth
        from that roster's own stored companies_house_sic_codes shows
        most of its confirmed suppliers are registered as wholesale/
        rental/repair businesses, not SIC 28220 manufacturers. Applying
        the soft-signal gate here rejected 3 of that roster's own real,
        already-confirmed suppliers on exactly this kind of language
        ("we stock forklifts from leading manufacturers") -- a real
        false-reject for THIS category, even though the same language
        is a correct reject for a category wanting direct manufacturers
        only (Injection Moulding, Brake Cable)."""
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

        fetch_result, fetch_failure_reason = self._fetch_candidate_site(candidate.domain)
        if fetch_result is None:
            return ValidationResult(candidate, False, None, None, None, fetch_failure_reason)

        page_text = fetch_result.pages[0].text

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

        # gate 3.6 -- catches a fetch that silently landed on a
        # DIFFERENT real company's site (found live: duraauto.com's own
        # homepage genuinely redirects to durashiloh.com, an unrelated
        # company). `final_url` defaults to `url` on any fetcher/fake
        # that doesn't populate it (OwnWebsitePage.__post_init__), so
        # this degrades to a no-op rather than a false reject when
        # redirect info isn't available.
        #
        # Runs HERE, after the LLM extraction above, not before it (as
        # it originally did) -- moved specifically so a LEGITIMATE
        # same-company domain migration isn't rejected on domain
        # mismatch alone. Found live: dexteraxle.com now forwards to
        # dextergroup.com, the SAME real manufacturer under its current
        # domain, not a hijack -- but a bare domain-mismatch check can't
        # tell that apart from duraauto.com's genuine redirect to an
        # unrelated competitor without reading what the landed page
        # actually says. _shares_distinctive_token(candidate.title,
        # extracted_name) is the exact corroboration check recover()
        # already uses for an analogous problem (a recovered candidate
        # must share a real word with the name it was recovering, not
        # just look self-consistent) -- reused here, not reimplemented.
        # A redirect whose landed page names an unrelated company (no
        # shared distinctive word) is still rejected exactly as before;
        # this only ever narrows the false-reject rate, never loosens
        # the hijack protection itself.
        #
        # Real cost tradeoff: every off-domain-redirect candidate now
        # costs one LLM call regardless of outcome (previously zero --
        # gate 3.6 rejected before the LLM ever ran), since there is no
        # way to distinguish "same company, migrated" from "different
        # company entirely" without reading what the landed page
        # actually says. A redirect is rare enough for this to be a
        # small, bounded increase against the alternative of silently
        # dropping a real, findable manufacturer every time one happens.
        final_url = getattr(fetch_result.pages[0], "final_url", "") or candidate.domain
        final_domain = extract_domain(final_url)
        resolved_domain: Optional[str] = None
        if final_domain and not domains_match(candidate.domain, final_domain):
            if not _shares_distinctive_token(candidate.title, extracted_name):
                return ValidationResult(
                    candidate, False, extracted_name, extracted_country, None,
                    f"{REASON_OFF_DOMAIN_REDIRECT_PREFIX}: {candidate.domain} -> {final_domain}",
                )
            resolved_domain = final_domain

        haystack = f"{candidate.title} {candidate.snippet}".lower()
        normalised_extracted = normalise_company_name(extracted_name)
        score = fuzz.partial_ratio(normalised_extracted, haystack)
        if score < _NAME_MATCH_THRESHOLD:
            return ValidationResult(
                candidate, False, extracted_name, extracted_country, score,
                f"extracted name '{extracted_name}' does not match the original search result (score={score:.0f})",
            )

        used_fallback_page = False
        if not _mentions_product_term(page_text, product_term):
            fallback_text = self._fetch_fallback_page_text(candidate)
            if fallback_text is None or not _mentions_product_term(fallback_text, product_term):
                return ValidationResult(
                    candidate, False, extracted_name, extracted_country, score,
                    f"{REASON_TERM_MISSING_PREFIX} '{product_term}'",
                )
            logger.info(
                "discovery: gate 6 recovered for %s via the search result's own deeper page %s "
                "(homepage didn't mention %r)", candidate.domain, candidate.link, product_term,
            )
            page_text = f"{page_text}\n{fallback_text}"
            used_fallback_page = True

        self_declared_trader = _find_trader_self_declaration(page_text)
        if self_declared_trader:
            return ValidationResult(
                candidate, False, extracted_name, extracted_country, score,
                f"{REASON_TRADER_PREFIX} (matched phrase: "
                f"'{self_declared_trader}') -- excluded, not a manufacturer",
            )

        soft_trader_signal = None if skip_soft_trader_signals else _find_trader_soft_signal(page_text)
        if soft_trader_signal:
            return ValidationResult(
                candidate, False, extracted_name, extracted_country, score,
                f"{REASON_TRADER_PREFIX} (matched soft signal: "
                f"'{soft_trader_signal}') -- excluded, not a manufacturer",
            )

        reason = f"{REASON_SUCCESS_PREFIX} (score={score:.0f}), product term found on page"
        if used_fallback_page:
            reason += " (via the search result's own deeper page, not the homepage)"
        return ValidationResult(
            candidate, True, extracted_name, extracted_country, score,
            reason,
            resolved_domain=resolved_domain,
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
