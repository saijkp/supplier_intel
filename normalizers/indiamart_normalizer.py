"""
normalizers/indiamart_normalizer.py

Maps raw items from the 'zuzka_mach/indiamart-scraper' Apify actor into
the supplier_data shape storage.repository.SUPPLIER_WRITABLE_FIELDS expects.

Like the Alibaba normalizer, Apify actor field names aren't perfectly
stable across versions/forks, so this checks several likely key
spellings per field via `_first_present`.

India-specific modelling notes:
  - GSTIN (India's GST registration number) is the closest analogue to
    China's USCC, but the schema's dedicated `uscc` column enforces an
    18-character China-specific format elsewhere in the pipeline
    (verification.uscc_validator). GSTIN is stored in the generic
    `company_reg_number` field instead, not `uscc`.
  - There's no dedicated `indiamart_trustseal` column in the schema
    (unlike `alibaba_trade_assurance`). Rather than a schema migration
    for one boolean, TrustSEAL status is recorded as a tagged entry in
    `other_certifications`, consistent with how non-ISO/E-mark
    certifications are already handled for Alibaba. This means
    TrustSEAL status doesn't feed verification_score the way Alibaba's
    trade assurance flag feeds platform_score — worth a schema
    migration to add first-class IndiaMART columns if/when supplier
    volume from this source grows enough to matter for scoring.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from normalizers.base_normalizer import BaseNormalizer

logger = logging.getLogger(__name__)

FIELD_ALIASES: Dict[str, List[str]] = {
    "company_name": ["companyName", "company_name", "name", "sellerName"],
    "profile_url": ["sellerUrl", "profileUrl", "url"],
    "city": ["city"],
    "state": ["state", "province"],
    "country": ["country"],
    "trustseal": ["trustSealVerified", "isTrustSeal", "trustSeal"],
    "member_since": ["memberSince", "registeredSince", "yearsInBusiness"],
    "main_products": ["mainProducts", "products", "categories"],
    "contact_name": ["contactPerson", "contactName"],
    "contact_title": ["contactTitle", "designation"],
    "email": ["email", "contactEmail"],
    "phone": ["mobile", "phone", "contactNumber"],
    "whatsapp": ["whatsapp", "whatsApp"],
    "gstin": ["gstNumber", "gstin", "GSTIN"],
    "nature_of_business": ["natureOfBusiness", "businessType"],
}


def _first_present(raw: Dict[str, Any], keys: List[str]) -> Any:
    for key in keys:
        if key in raw and raw[key] not in (None, "", []):
            return raw[key]
    return None


class IndiaMartNormalizer(BaseNormalizer):
    source_name = "indiamart"

    def normalise(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        company_name = self.clean_str(_first_present(raw_data, FIELD_ALIASES["company_name"]))
        if not company_name:
            logger.warning(
                "IndiaMART record missing a company name (sellerId=%s) — "
                "canonical_name will be empty; caller should skip creating a golden record.",
                raw_data.get("sellerId"),
            )

        profile_url = self.clean_str(_first_present(raw_data, FIELD_ALIASES["profile_url"]))
        domain = self._extract_domain(profile_url)

        # IndiaMART lists are, almost without exception, India-based —
        # default to it when a raw record simply omits the field, rather
        # than leaving country blank and losing an easy dedup signal.
        country = self.clean_str(_first_present(raw_data, FIELD_ALIASES["country"])) or "India"

        main_products = _first_present(raw_data, FIELD_ALIASES["main_products"]) or []
        if isinstance(main_products, str):
            main_products = [p.strip() for p in main_products.split(",") if p.strip()]

        year_established = self._extract_year(_first_present(raw_data, FIELD_ALIASES["member_since"]))
        gstin = self.clean_str(_first_present(raw_data, FIELD_ALIASES["gstin"]))

        supplier_data: Dict[str, Any] = {
            "canonical_name": company_name,
            "domain": domain,
            "country": country,
            "city": self.clean_str(_first_present(raw_data, FIELD_ALIASES["city"])) or None,
            "province_state": self.clean_str(_first_present(raw_data, FIELD_ALIASES["state"])) or None,
            "contact_name": self.clean_str(_first_present(raw_data, FIELD_ALIASES["contact_name"])) or None,
            "contact_title": self.clean_str(_first_present(raw_data, FIELD_ALIASES["contact_title"])) or None,
            "primary_email": self.clean_str(_first_present(raw_data, FIELD_ALIASES["email"])) or None,
            "primary_phone": self.clean_str(_first_present(raw_data, FIELD_ALIASES["phone"])) or None,
            "whatsapp": self.clean_str(_first_present(raw_data, FIELD_ALIASES["whatsapp"])) or None,
            "company_reg_number": gstin or None,
            "product_keywords": main_products,
            "indiamart_url": profile_url or None,
            "year_established": year_established,
        }

        is_manufacturer = self._infer_is_manufacturer(
            _first_present(raw_data, FIELD_ALIASES["nature_of_business"])
        )
        if is_manufacturer is not None:
            supplier_data["is_manufacturer"] = is_manufacturer

        if self.to_bool(_first_present(raw_data, FIELD_ALIASES["trustseal"])):
            supplier_data["other_certifications"] = ["IndiaMART TrustSEAL Verified"]

        return {
            k: v for k, v in supplier_data.items()
            if k == "canonical_name" or v not in (None, "", [])
        }

    @staticmethod
    def _extract_domain(url: str) -> Optional[str]:
        if not url:
            return None
        try:
            if not url.startswith("http"):
                url = "https://" + url
            netloc = urlparse(url).netloc.lower()
            return re.sub(r"^www\.", "", netloc) or None
        except Exception:
            return None

    @staticmethod
    def _extract_year(value: Any) -> Optional[int]:
        if not value:
            return None
        text = str(value)
        digits = "".join(c for c in text[:4] if c.isdigit())
        return int(digits) if len(digits) == 4 else None

    @staticmethod
    def _infer_is_manufacturer(value: Any) -> Optional[bool]:
        """Heuristic only, from IndiaMART's free-text 'nature of
        business' field — treat as a signal to combine with other
        sources, not ground truth."""
        if not value:
            return None
        text = str(value).lower()
        if "manufactur" in text or "producer" in text:
            return True
        if any(w in text for w in ("trader", "wholesaler", "distributor", "reseller", "supplier only")):
            return False
        return None
