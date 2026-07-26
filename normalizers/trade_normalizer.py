"""
normalizers/trade_normalizer.py

Normalizes trade-data records (ImportYeti today; Panjiva once/if API
access is negotiated — see Phase 1 Gap 2) into two separate outputs:

1. normalise() -> a partial supplier_data dict for the *shipper* (the
   exporter), suitable for dedup matching / golden record creation via
   the same repository.create_golden_record / merge_into_golden path
   every other normalizer uses.
2. to_shipment_record() -> a shipment_records row, linked to a
   supplier_id once the shipper's golden record has been resolved by
   the dedup matcher (Phase 3).

Trade data is inherently about a shipment, not a company profile, so the
supplier fields inferable here are thin (name, export destinations,
shipment counts) compared to what Alibaba/HKTDC normalizers produce.
Repeated merges across many shipments are what enrich these records
over time.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any, Dict, Optional

from normalizers.base_normalizer import BaseNormalizer

logger = logging.getLogger(__name__)

# Flat name lookups rather than a geo library dependency for Phase 2.
# Expand as real data surfaces additional country-name variants.
UK_NAMES = {
    "united kingdom", "uk", "gb", "great britain",
    "england", "wales", "scotland", "northern ireland",
}
US_NAMES = {"united states", "usa", "us", "united states of america"}
EU_COUNTRIES = {
    "germany", "france", "italy", "spain", "netherlands", "belgium", "poland",
    "sweden", "austria", "ireland", "denmark", "finland", "portugal", "greece",
    "czech republic", "romania", "hungary", "bulgaria", "slovakia", "croatia",
    "lithuania", "slovenia", "latvia", "estonia", "cyprus", "luxembourg", "malta",
}

DATE_FORMATS = (
    # Order matters for ambiguous DD/MM vs MM/DD slashed dates: UK-style
    # DD/MM/YYYY is tried first since IK Eng Ltd / IWT are UK businesses.
    # A date like "05/01/2026" therefore parses as 5 January, not 1 May.
    "%Y-%m-%d", "%d %b %Y", "%b %d, %Y", "%B %d, %Y", "%d/%m/%Y", "%m/%d/%Y",
)

# ImportYeti indexes US customs bills of lading, so a shipment scraped
# from there always has a US consignee even if the scraper couldn't
# extract an explicit consignee_country field from the page.
DEFAULT_CONSIGNEE_COUNTRY = {
    "importyeti": "United States",
}


class TradeNormalizer(BaseNormalizer):
    source_name = "trade"  # covers importyeti / panjiva

    def normalise(self, raw_data: Dict[str, Any], source: str = "importyeti") -> Dict[str, Any]:
        """Supplier-side view of a shipment record: the shipper is the
        candidate supplier, the consignee becomes a known buyer."""
        shipper_name = self.clean_str(raw_data.get("shipper_name"))
        consignee_name = self.clean_str(raw_data.get("consignee_name"))
        consignee_country = self.clean_str(raw_data.get("consignee_country"))
        if not consignee_country:
            consignee_country = DEFAULT_CONSIGNEE_COUNTRY.get(source, "")

        shipment_date = self._parse_date(raw_data.get("shipment_date"))

        country_lower = consignee_country.lower()
        is_uk = country_lower in UK_NAMES
        is_us = country_lower in US_NAMES
        is_eu = country_lower in EU_COUNTRIES

        supplier_data: Dict[str, Any] = {"canonical_name": shipper_name}

        if consignee_name:
            supplier_data["known_buyers"] = [consignee_name]
        if is_uk:
            supplier_data["exports_to_uk"] = True
            supplier_data["confirmed_shipments_uk"] = 1
        if is_us:
            supplier_data["exports_to_us"] = True
            supplier_data["confirmed_shipments_us"] = 1
        if is_eu:
            supplier_data["exports_to_eu"] = True
            supplier_data["confirmed_shipments_eu"] = 1
        if shipment_date:
            supplier_data["last_shipment_date"] = shipment_date

        return supplier_data

    def to_shipment_record(
        self,
        raw_data: Dict[str, Any],
        supplier_id: Optional[int] = None,
        source: str = "importyeti",
    ) -> Dict[str, Any]:
        """Build a row ready for SupplierRepository.add_shipment_record().
        Pass supplier_id once the dedup matcher has resolved which
        golden record this shipment's shipper belongs to."""
        consignee_country = self.clean_str(raw_data.get("consignee_country"))
        if not consignee_country:
            consignee_country = DEFAULT_CONSIGNEE_COUNTRY.get(source, "") or None

        return {
            "supplier_id": supplier_id,
            "source": source,
            "shipper_name": self.clean_str(raw_data.get("shipper_name")) or None,
            "consignee_name": self.clean_str(raw_data.get("consignee_name")) or None,
            "consignee_country": consignee_country,
            "shipment_date": self._parse_date(raw_data.get("shipment_date")),
            "hs_code": self.clean_str(raw_data.get("hs_code")) or None,
            "product_desc": self.clean_str(raw_data.get("product_desc")) or None,
            "quantity": self.clean_str(raw_data.get("quantity")) or None,
            "weight_kg": self._parse_number(raw_data.get("weight_raw") or raw_data.get("weight_kg")),
            "value_usd": self._parse_number(raw_data.get("value_raw") or raw_data.get("value_usd")),
            "origin_port": self.clean_str(raw_data.get("origin_port")) or None,
            "destination_port": self.clean_str(raw_data.get("destination_port")) or None,
            "raw_record": raw_data,
        }

    @staticmethod
    def _parse_date(value: Any) -> Optional[str]:
        if not value:
            return None
        if isinstance(value, (date, datetime)):
            return value.isoformat()[:10]
        text = str(value).strip()
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue
        logger.debug("Could not parse shipment date: %r", text)
        return None

    @staticmethod
    def _parse_number(value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = re.sub(r"[^\d.]", "", str(value))
        try:
            return float(text) if text else None
        except ValueError:
            return None
