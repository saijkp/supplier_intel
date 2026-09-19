"""
discovery/query_builder.py

Builds SerpAPI query variants from a product/category/country search --
the entire "AI-assisted web research" grounding source for Discovery
Service (see discovery/discovery_service.py's module docstring for the
full anti-hallucination pipeline this feeds). Purely mechanical string
building -- no LLM call, no freeform "list me suppliers" prompt
anywhere in this module.
"""

from __future__ import annotations

import re
from typing import List, Optional

# Conversational wrapper phrasing a buyer might type verbatim into a
# product/category field instead of a bare product term -- found live
# on a real "find me AGRICULTURAL EQUIPMENT MANUFACTURERS in the UK"
# discovery run: before clean_product_query() existed, that whole
# sentence was passed straight through to build_queries() (producing a
# useless literal-phrase SerpAPI search for the entire sentence) AND to
# discovery.candidate_validator's gate 6 (checking whether a
# candidate's own page mentioned that exact sentence -- which no real
# manufacturer's page ever does), rejecting every genuine candidate
# (Spearhead Machinery, Kverneland, GRIMME, JCB) on wording alone, not
# a real signal they weren't manufacturers. Curated, not a general NLP
# parser -- same discipline as _SPELLING_VARIANTS/
# _TRADER_SELF_DECLARATION_PHRASES in candidate_validator.py: extended
# as a real query shape is found tripping this, not guessed
# exhaustively in advance. Order matters -- a longer, more specific
# prefix (e.g. "help me find") must be tried before a shorter one it
# contains ("find") would otherwise match first and leave a dangling
# "me " behind; clean_product_query() re-applies every pattern to a
# fixpoint, so precise ordering only matters within a single pass.
_FILLER_PREFIX_PATTERNS: tuple = tuple(
    re.compile(pattern, re.I) for pattern in (
        r"^\s*(please\s+)?(can you\s+)?help\s+me\s+find\s+",
        r"^\s*(please\s+)?(can you\s+)?find\s+me\s+",
        r"^\s*(i'?m|i\s+am|we'?re|we\s+are)\s+looking\s+for\s+",
        r"^\s*looking\s+for\s+",
        r"^\s*(please\s+)?search\s+for\s+",
        r"^\s*(please\s+)?show\s+me\s+",
        r"^\s*(i|we)\s+need\s+",
        r"^\s*get\s+me\s+",
        r"^\s*(please\s+)?find\s+",
        r"^\s*(a\s+list\s+of|list\s+of)\s+",
        r"^\s*(suppliers|manufacturers|producers|makers|sources|distributors)\s+of\s+",
    )
)

# A trailing "in <region>" clause -- stripped only for a curated,
# closed set of region names/synonyms (same "small closed set, exact
# match only" discipline discovery.candidate_validator's own
# _UK_COUNTRY_SYNONYMS already uses), never a general geography strip,
# since "in <region>" could in principle be part of a product's own
# name for a category not seen yet.
_TRAILING_REGION_PATTERN = re.compile(
    r"\s+in\s+(the\s+)?(uk|united kingdom|great britain|england|scotland|wales|"
    r"northern ireland|us|usa|united states|eu|europe|china|india)\s*$",
    re.I,
)


def clean_product_query(raw_product: str) -> str:
    """Strips conversational wrapper phrasing from a raw, possibly
    natural-language product/category string (e.g. "find me
    agricultural equipment manufacturers in the UK") down to the core
    phrase build_queries() and candidate_validator's gate 6 actually
    need to match against ("agricultural equipment manufacturers").

    A string with no recognised filler is returned byte-for-byte
    unchanged -- this is a strip, never a rewrite or a re-casing, so a
    caller that already passes a clean term (sourcing.brief_parser.
    BriefParser's own LLM-extracted `product`, or a plain CLI
    --product value) sees no change at all; re-cleaning an
    already-clean term is a guaranteed no-op, so this is safe to call
    unconditionally at every discovery entry point rather than only
    the ones known to receive raw free text.

    Deliberately NOT an LLM call -- this needs to run for free, on
    every query, not gated behind a paid extra round-trip the way
    sourcing.brief_parser.BriefParser's fuller brief parsing already is
    for the sourcing-agent path specifically."""
    text = (raw_product or "").strip()
    if not text:
        return text

    stripped_any = True
    while stripped_any:
        stripped_any = False
        for pattern in _FILLER_PREFIX_PATTERNS:
            new_text = pattern.sub("", text).strip()
            if new_text != text and new_text:
                text = new_text
                stripped_any = True

    text = _TRAILING_REGION_PATTERN.sub("", text).strip()
    return text or raw_product.strip()


_QUERY_TEMPLATES: tuple = (
    '"{product}" manufacturer',
    '"{product}" supplier',
    '"{product}" factory',
    # Unquoted fallback -- real bug found live: an exact-phrase search
    # for a less common product phrasing (e.g. "wheel bearing units")
    # can return almost nothing, since most real listings use slightly
    # different wording ("wheel bearing", "wheel hub bearing unit",
    # etc.) that never matches a literal quoted phrase. Placed last so
    # discover()'s own early-stop-once-max_candidates loop only reaches
    # it when the three precise, quoted variants above didn't already
    # find enough -- no extra SerpAPI cost on a query that was already
    # well served.
    "{product} manufacturer",
)


def build_queries(
    product: str, category: Optional[str] = None, country: Optional[str] = None,
    application: Optional[str] = None, key_specifications: Optional[List[str]] = None,
    domain_tld_bias: Optional[str] = None, extra_role_words: Optional[List[str]] = None,
) -> List[str]:
    """One query variant per template, each optionally qualified by
    country, PLUS one additional variant each for `application`,
    `key_specifications` (sourcing.SourcingAgentService's own richer
    briefs), and `domain_tld_bias` when given -- still purely mechanical
    string building, no LLM call. `category` is accepted but not yet
    folded into the query text beyond `product` -- kept on the
    signature for the CLI/API surface (`main.py discover --category
    ...`) and for discovery_runs.category, which records it regardless
    of whether the query text itself uses it yet.

    `domain_tld_bias` (e.g. ".co.uk") adds ONE extra `site:`-restricted
    variant -- a cheap first-pass filter for a country-scoped discovery
    run, found live: `--country "United Kingdom"` only ever qualifies
    the query TEXT (Google can still return India/China-based results
    that happen to mention "United Kingdom" somewhere), it doesn't
    restrict results to UK-domiciled sites at all. Deliberately
    additive, not a replacement for the unbiased templates above -- a
    real UK manufacturer just as often sits on a plain .com (e.g.
    permatt.com, confirmed Companies House-verified) as a .co.uk, so a
    hard site: restriction across every variant would silently drop
    those. This is a bias toward more UK-domiciled candidates, not a
    filter for them -- the actual UK-registration check is
    verification.uk_company_verification_service, run downstream.

    `extra_role_words` (e.g. ["dealer", "distributor", "stockist"])
    adds one quoted-phrase variant per word, ON TOP OF the fixed
    manufacturer/supplier/factory templates above (never replacing
    them) -- found live: a real "material handling equipment
    manufacturer" run kept surfacing established dealers/distributors
    (Toyota Material Handling UK, Permatt, Jungheinrich UK) rather than
    raw manufacturers, and a buyer brief asking for "suppliers" has no
    reason to only search manufacturer-framed phrasing. Deliberately
    NOT baked into the default _QUERY_TEMPLATES tuple -- "dealer"/
    "distributor" framing is wrong for most other product categories
    this same builder serves (e.g. injection moulding), so this stays
    opt-in per call rather than a global behavior change.

    `extra_role_words` queries are placed FIRST in the returned list,
    ahead of the base templates -- deliberately, not incidentally.
    `discover()`'s own candidate-collection loop stops as soon as
    `max_candidates` raw candidates accumulate (see discovery_service.py),
    processing queries in this list's order; a caller that explicitly
    asked for role-word-broadened queries needs them to actually run,
    not get silently starved out by the base "manufacturer"/"supplier"
    templates filling the budget first -- the same class of bug already
    fixed once in this codebase for crawl-link ordering (see
    collection/site_collector.py's _prioritise_relevant_links). No
    effect when extra_role_words is empty/None -- output is unchanged
    for every existing caller."""
    queries = []

    if extra_role_words:
        for role_word in extra_role_words:
            query = f'"{product}" {role_word}'
            if country:
                query = f"{query} {country}"
            queries.append(query)

    for template in _QUERY_TEMPLATES:
        query = template.format(product=product)
        if country:
            query = f"{query} {country}"
        queries.append(query)

    if application:
        query = f'"{product}" manufacturer {application}'
        if country:
            query = f"{query} {country}"
        queries.append(query)

    if key_specifications:
        query = f'"{product}" manufacturer {" ".join(key_specifications)}'
        if country:
            query = f"{query} {country}"
        queries.append(query)

    if domain_tld_bias:
        query = f'"{product}" manufacturer site:{domain_tld_bias}'
        if country:
            query = f"{query} {country}"
        queries.append(query)

    return queries
