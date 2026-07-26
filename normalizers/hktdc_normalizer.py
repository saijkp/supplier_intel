"""
normalizers/hktdc_normalizer.py

Maps raw supplier cards from scrapers.hktdc_scraper.HKTDCScraper into
the supplier_data shape storage.repository.SUPPLIER_WRITABLE_FIELDS expects.

HKTDC listings are thinner than Alibaba's — no gold-supplier tenure,
trade assurance, or rating — so this normalizer maps considerably fewer
fields. That's expected: HKTDC's value in the pipeline is as a second,
independent confirmation source for dedup matching (see
deduplication.matcher), not as a primary data-rich source.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from deduplication.domain_utils import extract_domain
from normalizers.base_normalizer import BaseNormalizer

logger = logging.getLogger(__name__)


class HKTDCNormalizer(BaseNormalizer):
    source_name = "hktdc"

    def normalise(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        company_name = self.clean_str(raw_data.get("company_name"))
        if not company_name:
            logger.warning("HKTDC record missing a company name — canonical_name will be empty.")

        website = self.clean_str(raw_data.get("website"))
        domain = extract_domain(website) if website else None

        products: Any = raw_data.get("products") or []
        if isinstance(products, str):
            products = [p.strip() for p in products.split(",") if p.strip()]

        supplier_data: Dict[str, Any] = {
            "canonical_name": company_name,
            "domain": domain,
            "country": self.clean_str(raw_data.get("country")) or None,
            "product_keywords": products,
            "hktdc_url": self.clean_str(raw_data.get("hktdc_profile_url")) or None,
            "moq_notes": self.clean_str(raw_data.get("description")) or None,
        }

        return {
            k: v for k, v in supplier_data.items()
            if k == "canonical_name" or v not in (None, "", [])
        }
