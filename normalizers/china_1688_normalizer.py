"""
normalizers/china_1688_normalizer.py

Maps raw items from the 'webdata_labs/1688-scraper' Apify actor
(scrapers.scraper_1688.China1688Scraper) into the supplier_data shape
storage.repository.SUPPLIER_WRITABLE_FIELDS expects.

Confirmed against real output from a live --limit 3 run (2026-08-02,
query "wheel hub"), not assumed from the actor's input-schema docs
(which don't describe the output shape at all). Real shape is FLAT --
companyName, province, city, supplierUrl etc. all sit at the top level
of each raw item, one item per PRODUCT (not per supplier -- unlike
zen-studio/alibaba-scraper's explicit "one profile per supplier" mode,
this actor has no such mode; see scrapers/scraper_1688.py's own
module docstring). The same real company can legitimately appear
across several raw records here; that's deduplication.matcher's job
to collapse, not this normalizer's.

A real bug, found and fixed here after inspecting that live output
--------------------------------------------------------------------
The first version of this normalizer guessed a nested supplier/seller/
shop object (matching zen-studio/alibaba-scraper's shape) that doesn't
exist here, and its flat-shape fallback alias list for "profile_url"
included the generic top-level "url" key -- which in this actor's real
output is the PRODUCT DETAIL PAGE url on detail.1688.com, the same
host for every single seller on the platform. That resolved `domain`
to "detail.1688.com" for every record, which is indistinguishable from
two different real companies happening to share a domain -- and did,
in fact, cause a real false merge (two genuinely different companies,
"瑞安市嘉业汽摩附件有限公司" and "温州市龙湾永中南牧五金加工厂",
auto-merged into one golden record at the domain-exact-match
confidence tier, 0.95) before this was caught and fixed.

`domain` is deliberately never set from anything in this actor's
output as a result: neither the product detail URL (detail.1688.com)
nor the supplier's own 1688 shop URL (supplierUrl, on
winport.m.1688.com) represents the company's real external website --
both are 1688's own marketplace domains, shared across every seller on
the platform, the same category of mistake already avoided for
europages_eastern_europe's JSON-LD profile URLs and
zen-studio/alibaba-scraper's own profile pages. A real external domain,
if one exists, is CompanyWebsiteFinder's job later, same as any other
domain-less supplier. supplierUrl is preserved in `notes` instead --
visible, but never treated as (or confused with) a distinct company
domain for dedup purposes.

province/city come back as Chinese place names ("浙江省", "温州市"),
untranslated -- this codebase has no place-name translation utility,
so they're stored as-is rather than guessed at. Worth fixing before
relying on province/city-based filtering or display for this source
specifically; not attempted here.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from normalizers.base_normalizer import BaseNormalizer

logger = logging.getLogger(__name__)

# our_field -> possible top-level source keys, checked in order, first
# non-empty wins -- kept flexible (rather than a single hardcoded key
# each) in case the actor's own field names shift between builds, same
# defensive convention every other normalizer in this codebase uses.
# Deliberately NO alias here ever points at a URL: see this module's
# own docstring for why domain/profile_url are not derived from
# anything in this actor's output at all.
FIELD_ALIASES: Dict[str, List[str]] = {
    "company_name": ["companyName", "company_name", "shopName"],
    "supplier_url": ["supplierUrl", "sellerUrl"],
    "province": ["province"],
    "city": ["city"],
    "title": ["titleEn", "title"],
}


def _first_present(raw: Dict[str, Any], keys: List[str]) -> Any:
    for key in keys:
        if key in raw and raw[key] not in (None, "", []):
            return raw[key]
    return None


class China1688Normalizer(BaseNormalizer):
    source_name = "china_1688"

    def normalise(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        company_name = self.clean_str(_first_present(raw_data, FIELD_ALIASES["company_name"]))
        if not company_name:
            logger.warning(
                "1688 record missing a company name (offerId=%s) — canonical_name will be "
                "empty; caller should skip creating a golden record.",
                raw_data.get("offerId"),
            )

        title = self.clean_str(_first_present(raw_data, FIELD_ALIASES["title"]))
        product_keywords = [title] if title else []

        merchant_signs = raw_data.get("merchantSigns")
        notes_parts: List[str] = []
        if isinstance(merchant_signs, dict):
            true_signs = [k for k, v in merchant_signs.items() if v is True]
            if true_signs:
                # Self-reported platform badges -- captured as a plain-
                # English note, not mapped onto is_manufacturer, same
                # principle as every other marketplace badge in this
                # codebase (AlibabaNormalizer's verifiedManufacturer,
                # GlobalDirectoryNormalizer's directory listing):
                # manufacturer verification is verification.
                # manufacturer_verifier's own independent job, not
                # something to take a platform's own label for.
                notes_parts.append(f"1688 merchant signs: {', '.join(true_signs)}")

        supplier_url = self.clean_str(_first_present(raw_data, FIELD_ALIASES["supplier_url"]))
        if supplier_url:
            notes_parts.append(f"1688 supplier page: {supplier_url}")

        supplier_data: Dict[str, Any] = {
            "canonical_name": company_name,
            # 1688 is a China-only domestic platform -- every result is
            # a Chinese supplier regardless of whether province/city
            # were extracted.
            "country": "China",
            "province_state": self.clean_str(_first_present(raw_data, FIELD_ALIASES["province"])) or None,
            "city": self.clean_str(_first_present(raw_data, FIELD_ALIASES["city"])) or None,
            "product_keywords": product_keywords,
            "notes": "; ".join(notes_parts) or None,
        }

        return {
            k: v for k, v in supplier_data.items()
            if k == "canonical_name" or v not in (None, "", [])
        }
