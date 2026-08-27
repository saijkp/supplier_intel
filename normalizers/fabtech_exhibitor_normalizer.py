"""
normalizers/fabtech_exhibitor_normalizer.py

Maps one raw exhibitor record from FABTECH's own A2Z-powered exhibitor
directory (discovery/fabtech_exhibitor_import.py fetches the real
server-rendered list, then a real second fetch per exhibitor's own
SmallWorldLabs profile page for its actual company website) onto the
standard supplier candidate shape, for pipeline.static_list_import.
import_static_supplier_list -- the exact same reusable dedup/merge
infrastructure normalizers/linde_dealer_normalizer.py and
normalizers/automechanika_normalizer.py already use.

Deliberately does NOT run candidates through discovery.candidate_
validator.CandidateValidator (no trader gate, no product-term check),
same reasoning as normalizers/linde_dealer_normalizer.py: exhibiting
at FABTECH under one's own name and profile is a real, self-reported
identity signal from a legitimate industry trade show, not an
unverified search hit needing corroboration.

Field mapping
-------------
- `name` -> `canonical_name`, straight from FABTECH's own exhibitor
  list, unmodified.
- `website` -> `domain`, via the same extract_domain() every other
  normalizer uses. Populated by discovery/fabtech_exhibitor_import.py
  from the exhibitor's OWN profile page's structured "Website" field,
  after a real liveness check -- see that module's own docstring.
- `address` -> the exhibitor profile's own "Address" field, its
  street / city-state-zip / country lines (originally `<br>`-separated
  in the source markup, passed through here newline-joined) rejoined
  with ", ". `country` is parsed out as the address's own LAST line --
  verified against a real sample spanning US, Canadian, and Italian
  addresses before trusting this shape (same "verify the real format
  before parsing" discipline as normalizers/automechanika_normalizer.
  py's own `_parse_country`, just newline-delimited here instead of
  comma-delimited).
- `city` -> the address's second-to-last line, up to its first comma
  (e.g. "Salt Lake City, UT 84101-2378" -> "Salt Lake City") -- only
  extracted when there are at least 3 address lines, so a 2-line
  address (street + country, no separate city line) never
  misattributes the street itself as a city.
- `phone` -> `primary_phone`, unmodified.
- `booth_number`/`pavilion` are NOT mapped to any supplier column --
  not lost, `import_static_supplier_list` saves every raw row to
  `raw_source_data` verbatim before normalising, matching
  normalizers/automechanika_normalizer.py's own precedent for
  exhibitor metadata with no dedicated column.
"""

from __future__ import annotations

from typing import Any, Dict

from deduplication.domain_utils import extract_domain
from normalizers.base_normalizer import BaseNormalizer


class FabtechExhibitorNormalizer(BaseNormalizer):
    source_name = "trade-show-exhibitor-fabtech"

    def normalise(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        name = self.clean_str(raw_data.get("name"))
        website = self.clean_str(raw_data.get("website"))
        address = self.clean_str(raw_data.get("address"))
        phone = self.clean_str(raw_data.get("phone"))

        result: Dict[str, Any] = {"canonical_name": name}

        if website:
            domain = extract_domain(website)
            if domain:
                result["domain"] = domain

        if address:
            lines = [ln.strip() for ln in address.split("\n") if ln.strip()]
            if len(lines) >= 2:
                result["address"] = ", ".join(lines)
                result["country"] = lines[-1]
                if len(lines) >= 3:
                    city = lines[-2].split(",")[0].strip()
                    if city:
                        result["city"] = city
            elif lines:
                result["address"] = lines[0]

        if phone:
            result["primary_phone"] = phone

        result["discovery_source"] = self.source_name

        return result
