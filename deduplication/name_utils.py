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
