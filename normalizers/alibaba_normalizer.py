"""
normalizers/alibaba_normalizer.py

Maps raw items from the 'curious_coder/alibaba-scraper' Apify actor into
the supplier_data shape storage.repository.SUPPLIER_WRITABLE_FIELDS expects.

Apify actor output field names aren't perfectly stable across actor
versions/forks, so this normalizer checks several likely key spellings
per field via `_first_present` rather than hardcoding one exact key. If
your actor run uses different keys, add them to FIELD_ALIASES below —
the parsing logic itself shouldn't need to change.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from normalizers.base_normalizer import BaseNormalizer

logger = logging.getLogger(__name__)

# our_field -> possible source keys, checked in order, first non-empty wins.
FIELD_ALIASES: Dict[str, List[str]] = {
    "company_name": ["companyName", "company_name", "name", "storeName"],
    "profile_url": ["companyUrl", "storeUrl", "url", "profileUrl"],
    "country": ["country", "companyCountry", "location"],
    "city": ["city", "companyCity"],
    "years_gold": ["yearsAsGoldSupplier", "goldYears", "years"],
    "trade_assurance": ["tradeAssurance", "hasTradeAssurance"],
    "rating": ["rating", "reviewScore", "supplierRating"],
    "main_products": ["mainProducts", "products", "categories"],
    "contact_name": ["contactPerson", "contactName", "salesManager"],
    "contact_title": ["contactTitle", "position"],
    "email": ["email", "contactEmail"],
    "phone": ["phone", "telephone", "contactPhone"],
    "whatsapp": ["whatsapp", "whatsApp"],
    "employee_count": ["employees", "staffCount", "employeeCount"],
    "year_established": ["establishedYear", "yearEstablished"],
    "certifications": ["certifications", "certs"],
    # Factory/facility photo URLs, if the actor exposes them — captured
    # here (as factory_photo_urls) even though nothing in this codebase
    # downloads or analyses them yet; see verification.factory_photo_verifier.
    "factory_photos": ["factoryImages", "images", "productImages", "photos"],
    # Self-reported export destinations — a weaker signal than a
    # confirmed shipment record, see BaseNormalizer.infer_export_flags_from_markets.
    "main_markets": ["mainMarkets", "exportMarkets", "targetMarkets"],
}


def _first_present(raw: Dict[str, Any], keys: List[str]) -> Any:
    for key in keys:
        if key in raw and raw[key] not in (None, "", []):
            return raw[key]
    return None


class AlibabaNormalizer(BaseNormalizer):
    source_name = "alibaba"

    def normalise(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        company_name = self.clean_str(_first_present(raw_data, FIELD_ALIASES["company_name"]))
        if not company_name:
            logger.warning(
                "Alibaba record missing a company name (supplierId=%s) — "
                "canonical_name will be empty; caller should skip creating a golden record.",
                raw_data.get("supplierId"),
            )

        profile_url = self.clean_str(_first_present(raw_data, FIELD_ALIASES["profile_url"]))
        domain = self._extract_domain(profile_url)

        years_gold = self.to_int(_first_present(raw_data, FIELD_ALIASES["years_gold"]), default=0)
        rating = self.to_float(_first_present(raw_data, FIELD_ALIASES["rating"]))
        trade_assurance = self.to_bool(_first_present(raw_data, FIELD_ALIASES["trade_assurance"]))

        main_products = _first_present(raw_data, FIELD_ALIASES["main_products"]) or []
        if isinstance(main_products, str):
            main_products = [p.strip() for p in main_products.split(",") if p.strip()]

        year_established = self.to_int(_first_present(raw_data, FIELD_ALIASES["year_established"]), default=0)

        supplier_data: Dict[str, Any] = {
            "canonical_name": company_name,
            "domain": domain,
            "country": self.clean_str(_first_present(raw_data, FIELD_ALIASES["country"])) or None,
            "city": self.clean_str(_first_present(raw_data, FIELD_ALIASES["city"])) or None,
            "contact_name": self.clean_str(_first_present(raw_data, FIELD_ALIASES["contact_name"])) or None,
            "contact_title": self.clean_str(_first_present(raw_data, FIELD_ALIASES["contact_title"])) or None,
            "primary_email": self.clean_str(_first_present(raw_data, FIELD_ALIASES["email"])) or None,
            "primary_phone": self.clean_str(_first_present(raw_data, FIELD_ALIASES["phone"])) or None,
            "whatsapp": self.clean_str(_first_present(raw_data, FIELD_ALIASES["whatsapp"])) or None,
            "employee_count": self.clean_str(_first_present(raw_data, FIELD_ALIASES["employee_count"])) or None,
            "year_established": year_established or None,
            "product_keywords": main_products,
            "alibaba_url": profile_url or None,
            "alibaba_gold_supplier": years_gold > 0,
            "alibaba_years": years_gold or None,
            "alibaba_trade_assurance": trade_assurance,
            "alibaba_rating": rating,
        }

        certifications = _first_present(raw_data, FIELD_ALIASES["certifications"]) or []
        if isinstance(certifications, str):
            certifications = [c.strip() for c in certifications.split(",") if c.strip()]
        if certifications:
            supplier_data["other_certifications"] = certifications
            cert_text = " ".join(certifications).lower()
            supplier_data["iso_9001"] = "iso 9001" in cert_text or "iso9001" in cert_text
            supplier_data["e_mark_certified"] = any(
                token in cert_text for token in ("e-mark", "e mark", "ece")
            )

        photos = _first_present(raw_data, FIELD_ALIASES["factory_photos"]) or []
        if isinstance(photos, str):
            photos = [p.strip() for p in photos.split(",") if p.strip()]
        # Actor items sometimes return photo objects ({'url': ...}) rather
        # than bare URL strings — normalise either shape to a flat list
        # of URLs, since that's all factory_photo_urls stores.
        photo_urls = [
            (p.get("url") if isinstance(p, dict) else p) for p in photos
        ]
        photo_urls = [u for u in photo_urls if u]
        if photo_urls:
            supplier_data["factory_photo_urls"] = photo_urls

        main_markets = _first_present(raw_data, FIELD_ALIASES["main_markets"]) or []
        if isinstance(main_markets, str):
            main_markets = [m.strip() for m in main_markets.split(",") if m.strip()]
        if main_markets:
            supplier_data.update(self.infer_export_flags_from_markets(main_markets))

        # Keep canonical_name even if empty (caller needs to be able to
        # detect a missing name explicitly); drop every other empty/None field.
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
            domain = re.sub(r"^www\.", "", netloc)
            return domain or None
        except Exception:
            return None
