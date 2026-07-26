"""
normalizers/global_directory_normalizer.py

Maps raw company records from scrapers.global_directory_scraper.GlobalDirectoryScraper
(Turkey/Vietnam/Eastern Europe directories) into the supplier_data shape
storage.repository.SUPPLIER_WRITABLE_FIELDS expects.

Like HKTDC and exhibition listings, these directories are thin —
typically just a name, country, and a product/sector tag, with no
contact details — but being a listed member of a national trade body
(TIM, VCCI) or a major B2B directory (Europages) is itself a real
signal worth capturing, and serves as an independent corroborating
source for the dedup matcher regardless of how little else it knows
about the company.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from deduplication.domain_utils import extract_domain
from normalizers.base_normalizer import BaseNormalizer

logger = logging.getLogger(__name__)


class GlobalDirectoryNormalizer(BaseNormalizer):
    source_name = "global_directory"

    def normalise(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        company_name = self.clean_str(raw_data.get("company_name"))
        if not company_name:
            logger.warning("Directory record missing a company name — canonical_name will be empty.")

        website = self.clean_str(raw_data.get("website"))
        domain = extract_domain(website) if website else None

        products = raw_data.get("products") or []
        if isinstance(products, str):
            products = [p.strip() for p in products.split(",") if p.strip()]

        directory_name = self.clean_str(raw_data.get("directory_name"))
        notes = f"Listed in {directory_name}" if directory_name else None

        supplier_data: Dict[str, Any] = {
            "canonical_name": company_name,
            "domain": domain,
            "country": self.clean_str(raw_data.get("country")) or None,
            "product_keywords": products,
            "notes": notes,
        }

        return {
            k: v for k, v in supplier_data.items()
            if k == "canonical_name" or v not in (None, "", [])
        }
