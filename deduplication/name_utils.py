"""
deduplication/name_utils.py

Company name normalisation for deduplication matching. Addresses Phase 1
Gap 3: Chinese company names in English are highly variable —
"Guangzhou ABC Electronics Co Ltd", "ABC Electronics (Guangzhou)", and
"ABC Electronic Co., Ltd Guangzhou" should all normalise close enough to
match via fuzzy string comparison.
"""

from __future__ import annotations

import re
from typing import Optional

# Legal-entity suffixes stripped regardless of market. Order matters:
# later entries are checked after earlier ones have already trimmed the
# string, which is what lets chained suffixes like "Trading Co., Ltd"
# collapse fully in a single pass.
LEGAL_SUFFIXES = (
    r"\bco\.?,?\s*ltd\.?$",
    r"\bltd\.?$",
    r"\bllc\.?$",
    r"\binc\.?$",
    r"\bcorp(oration)?\.?$",
    r"\bcompany$",
    r"\btrading$",
    r"\bmanufacturing$",
    r"\bmanufacturer$",
    r"\bfactory$",
    r"\bgroup$",
    r"\binternational$",
    r"\bimp\s*(and|&)?\s*exp$",
    r"\bimport\s*(and|&)?\s*export$",
    r"\bco$",
)

# Common Chinese province/municipality/major-city names that show up as
# a prefix or suffix in English company names but carry no matching
# signal on their own — nearly every supplier in a province has the
# province name somewhere in its name. Stripping these lets "Guangzhou
# ABC Electronics" and "ABC Electronics (Guangzhou)" collapse to the
# same normalised form. Not exhaustive; extend as real data surfaces
# more variants.
CHINESE_GEO_TOKENS = {
    "guangzhou", "shenzhen", "dongguan", "foshan", "zhongshan", "huizhou",
    "shanghai", "beijing", "tianjin", "chongqing",
    "ningbo", "hangzhou", "wenzhou", "yiwu", "jinhua", "taizhou",
    "suzhou", "wuxi", "nanjing", "changzhou", "xuzhou",
    "qingdao", "jinan", "yantai", "weifang",
    "xiamen", "fuzhou", "quanzhou",
    "wuhan", "changsha", "zhengzhou", "chengdu", "xian", "shenyang",
    "dalian", "harbin", "kunming", "nanning", "hefei", "nanchang",
    "guangdong", "zhejiang", "jiangsu", "shandong", "fujian", "sichuan",
    "hebei", "henan", "hubei", "hunan", "anhui", "jiangxi", "liaoning",
}


def normalise_company_name(name: Optional[str], strip_geo: bool = True) -> str:
    """Lowercase, strip legal suffixes and (optionally) common Chinese
    geo tokens, strip punctuation, collapse whitespace. Returns '' for
    empty input rather than raising, since a supplier missing a name
    should just fail to match rather than crash the pipeline."""
    if not name:
        return ""

    text = name.lower().strip()

    for suffix in LEGAL_SUFFIXES:
        text = re.sub(suffix, "", text).strip()

    # Strip bracketed geo qualifiers like "(Guangzhou)" before the
    # generic punctuation strip, so the parens themselves don't need
    # separate handling.
    text = re.sub(r"\([^)]*\)", " ", text)

    # Strip punctuation, keep word characters and spaces
    text = re.sub(r"[^\w\s]", " ", text)
    text = " ".join(text.split())

    if strip_geo and text:
        tokens = [t for t in text.split() if t not in CHINESE_GEO_TOKENS]
        if tokens:  # never strip a name down to nothing
            text = " ".join(tokens)

    return text.strip()


# Corporate-suffix/industry-vocabulary words common enough within one
# company name or product category that sharing one proves nothing --
# curated, not exhaustive, extended as a real collision is found. Moved
# here (was discovery/candidate_validator.py-private) so
# scrapers/company_website_finder.py can reuse the same distinctive-
# token discipline instead of reimplementing it -- see
# _shares_distinctive_token's own docstring for why raw fuzzy
# similarity alone isn't safe for this comparison. Confirmed live twice
# in candidate_validator.recover(): "Apadrecoplastics" (dead domain)
# fuzzy-matched onto the real site of an unrelated company, Adreco
# Plastics (STH Plastics Group) at 90.3 on every rapidfuzz variant --
# HIGHER than this codebase's own strict UK-verification bar of 85 --
# and separately "Ability Handling" (dead domain) matched onto the real,
# unrelated "Grant Handling" purely because both names contain
# "Handling". A 2+-shared-words requirement was considered and rejected
# instead of a stoplist: it regresses an already-passing near-miss
# ("Beta Bearings Ltd" vs "Beta Bearing Ltd" -- exactly one shared
# distinctive word, "beta", since "bearings"/"bearing" don't
# stem-match) in exchange for guarding a case that hasn't happened yet.
_GENERIC_NAME_WORDS = frozenset({
    "ltd", "limited", "inc", "incorporated", "llc", "co", "company",
    "corp", "corporation", "group", "plc", "gmbh", "holdings",
    "international", "the", "and",
    "handling", "forklift", "forklifts", "truck", "trucks", "lift", "lifts",
    "equipment", "plant", "machinery", "material", "materials",
})


def _distinctive_tokens(name: str) -> set:
    """Words of `name` that are long enough (>=4 characters) and not a
    generic corporate-suffix/industry word (see _GENERIC_NAME_WORDS) to
    carry any real identity signal on their own."""
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    return {w for w in words if len(w) >= 4 and w not in _GENERIC_NAME_WORDS}


def _shares_distinctive_token(a: str, b: str) -> bool:
    """True if `a` and `b` share at least one significant word, OR
    either side has no significant words at all to compare (nothing
    distinctive means no basis for a rejection -- don't invent one from
    insufficient signal, same discipline as everywhere else this
    utility is used)."""
    tokens_a, tokens_b = _distinctive_tokens(a), _distinctive_tokens(b)
    if not tokens_a or not tokens_b:
        return True
    return bool(tokens_a & tokens_b)


# Fallback bar for names_plausibly_corroborate's short-name path --
# fuzz.ratio (whole-string, length-sensitive), not partial_ratio.
# Deliberately reusing matcher.py's own name-vs-name tool (see
# deduplication/matcher.py's fuzz.ratio usage) rather than partial_ratio
# -- partial_ratio is exactly what let "Ashpock" validate against a
# page about "Shpock" in the first place (partial_ratio finds "shpock"
# as a near-perfect ALIGNED SUBSTRING of "ashpock", scoring ~92 even
# though they're different companies), because it only rewards the best
# local alignment and never penalises length/character differences
# elsewhere in either string. ratio scores the two strings as wholes,
# so it doesn't have that blind spot.
_SHORT_NAME_RATIO_THRESHOLD = 70.0


def names_plausibly_corroborate(a: str, b: str) -> bool:
    """True if `a` and `b` plausibly name the same company. Uses
    _shares_distinctive_token's token-overlap check ONLY when BOTH
    sides have a real (>=4-char, non-generic) word to compare on --
    that check's own "insufficient signal, don't reject" rule means it
    auto-passes the moment EITHER side is empty, which is exactly the
    gap that matters here: a short/generic name on one side (e.g. "IK
    Eng Ltd" -- every word is under 4 characters once "Ltd" is
    filtered) would otherwise corroborate against literally any other
    name, including one with real, unrelated distinctive words.
    Confirmed live: "IK Eng Ltd" resolved to easydigitalfiling.com, a
    UK company-formation agent's site whose own stated name -- "Easy
    Digital Filing Ltd", which DOES have distinctive words -- has
    nothing to do with "IK Eng Ltd"; _shares_distinctive_token(a, b)
    directly would still return True here purely because `a` has no
    tokens, never actually comparing the two names. Whenever either
    side lacks a distinctive word, falls back to a strict whole-string
    ratio instead (see _SHORT_NAME_RATIO_THRESHOLD's own docstring for
    why ratio, not partial_ratio)."""
    if _distinctive_tokens(a) and _distinctive_tokens(b):
        return _shares_distinctive_token(a, b)
    from rapidfuzz import fuzz

    return fuzz.ratio(normalise_company_name(a), normalise_company_name(b)) >= _SHORT_NAME_RATIO_THRESHOLD
