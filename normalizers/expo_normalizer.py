"""
normalizers/expo_normalizer.py

Maps raw exhibitor records from scrapers.shanghai_expo_scraper into the
supplier_data shape storage.repository.SUPPLIER_WRITABLE_FIELDS expects.

Exhibition directories are the thinnest data source in the pipeline —
often just a name, country, booth number, and maybe a product category
tag, with no contact details at all. But exhibiting at a major trade
show (which costs real money and typically requires a registered
business to book a booth) is itself a meaningful positive signal worth
capturing, even without contact info — it's exactly the kind of
independent confirmation the dedup matcher uses to corroborate a
supplier already found via Alibaba/HKTDC/IndiaMART.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from deduplication.domain_utils import extract_domain
from normalizers.base_normalizer import BaseNormalizer

logger = logging.getLogger(__name__)


class ExpoNormalizer(BaseNormalizer):
    source_name = "shanghai_expo"

    def normalise(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        company_name = self.clean_str(raw_data.get("company_name"))
        if not company_name:
            logger.warning("Exhibition record missing a company name — canonical_name will be empty.")

        website = self.clean_str(raw_data.get("website"))
        domain = extract_domain(website) if website else None

        products = raw_data.get("products") or []
        if isinstance(products, str):
            products = [p.strip() for p in products.split(",") if p.strip()]

        exhibition_name = self.clean_str(raw_data.get("exhibition_name"))
        booth = self.clean_str(raw_data.get("booth_number"))

        # `notes` is a plain-text scalar column, not a JSON array, so
        # repeated exhibition appearances across years/sources will only
        # ever populate it the first time a merge sees it empty (see
        # SupplierRepository.merge_into_golden's non-clobbering scalar
        # rule) — a future enhancement worth considering is a dedicated
        # JSON `exhibition_history` column if this signal turns out to
        # matter a lot for scoring.
        note_parts = []
        if exhibition_name:
            note_parts.append(f"Exhibitor at {exhibition_name}")
        if booth:
            note_parts.append(f"booth {booth}")

        supplier_data: Dict[str, Any] = {
            "canonical_name": company_name,
            "domain": domain,
            "country": self.clean_str(raw_data.get("country")) or None,
            "product_keywords": products,
            "notes": "; ".join(note_parts) or None,
        }

        return {
            k: v for k, v in supplier_data.items()
            if k == "canonical_name" or v not in (None, "", [])
        }
