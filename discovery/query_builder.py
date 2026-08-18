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

from typing import List, Optional

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
    opt-in per call rather than a global behavior change."""
    queries = []
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

    if extra_role_words:
        for role_word in extra_role_words:
            query = f'"{product}" {role_word}'
            if country:
                query = f"{query} {country}"
            queries.append(query)

    return queries
