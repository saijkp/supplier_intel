"""
normalizers/linde_dealer_normalizer.py

Maps one raw entry from Linde Material Handling's own published dealer
network (discovery/linde_dealer_import.py fetches the real
Dealer-Finder-App-Data.json Linde's own site serves) onto the standard
supplier candidate shape, for pipeline.static_list_import.
import_static_supplier_list -- the exact same reusable dedup/merge
infrastructure automechanika_normalizer.py already uses, not a new
import path.

Deliberately does NOT run candidates through discovery.candidate_
validator.CandidateValidator (no trader gate, no product-term check):
Linde's own dealer network membership is a stronger identity/legitimacy
signal than either of those gates exists to approximate -- a company
publishing itself as an authorized dealer FOR a specific OEM already IS
a manufacturer-adjacent supplier for that OEM's product line, and the
name/address/phone/website all come from Linde's own records, not an
unverified third-party claim to corroborate against a search result.
The one real-world check this data still needs -- and gets, in
discovery/linde_dealer_import.py before a raw record ever reaches this
normalizer -- is that the listed website still resolves, catching
stale/closed dealers before they enter the roster.

Field mapping
-------------
- `name` -> `canonical_name`, as Linde has it, unmodified.
- `website` -> `domain`, via the same extract_domain() every other
  normalizer uses.
- `street`/`city`/`zip` -> combined into a single `address` string
  (matching automechanika_normalizer's single-field convention), each
  part included only if present -- Linde's own `street` field
  sometimes already embeds a locality (e.g. "... | Martinez,
  Argentina") distinct from the separate `city` field (e.g. "Buenos
  Aires"); both are kept rather than one overwriting the other, since
  this is Linde's own data verbatim, not this codebase's to reconcile.
- `country` -> Linde's own field is a bare ISO 3166-1 alpha-2 code
  (lowercase, e.g. "ar"), not a name -- converted via pycountry
  (already a project dependency; same exact `pycountry.countries.get(
  alpha_2=...)` pattern scrapers/global_directory_scraper.py's
  `_iso_country_name` and normalizers/alibaba_normalizer.py already
  use), since `country` is stored as a full English name everywhere
  else in this database (search_suppliers_full's exact-match country
  filter relies on this) -- storing the bare code would silently break
  that, not preserve Linde's data more faithfully.
- `phone` -> `primary_phone`, `mail` -> `primary_email`, both
  unmodified strings as Linde provides them (no phonenumbers
  reformatting -- Linde's own international-format numbers are already
  usable as stored).
"""

from __future__ import annotations

from typing import Any, Dict

from deduplication.domain_utils import extract_domain
from normalizers.base_normalizer import BaseNormalizer


def _iso_country_name(code: Any) -> str:
    """'ar' -> 'Argentina'. Same pattern as scrapers/
    global_directory_scraper.py's own _iso_country_name -- duplicated
    rather than imported cross-module, matching how this exact
    conversion is already independently inlined in
    normalizers/alibaba_normalizer.py too."""
    if not code or not isinstance(code, str):
        return ""
    import pycountry

    country = pycountry.countries.get(alpha_2=code.strip().upper())
    return country.name if country else ""


class LindeDealerNormalizer(BaseNormalizer):
    source_name = "linde-oem-dealer-network"

    def normalise(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        name = self.clean_str(raw_data.get("name"))
        website = self.clean_str(raw_data.get("website"))
        street = self.clean_str(raw_data.get("street"))
        city = self.clean_str(raw_data.get("city"))
        zip_code = self.clean_str(raw_data.get("zip"))
        country_code = self.clean_str(raw_data.get("country"))
        phone = self.clean_str(raw_data.get("phone"))
        mail = self.clean_str(raw_data.get("mail"))

        result: Dict[str, Any] = {"canonical_name": name}

        if website:
            domain = extract_domain(website)
            if domain:
                result["domain"] = domain

        address_parts = [part for part in (street, city, zip_code) if part]
        if address_parts:
            result["address"] = ", ".join(address_parts)
        if city:
            result["city"] = city

        if country_code:
            country_name = _iso_country_name(country_code)
            if country_name:
                result["country"] = country_name

        if phone:
            result["primary_phone"] = phone
        if mail:
            result["primary_email"] = mail

        result["discovery_source"] = self.source_name

        return result
