"""
normalizers/alibaba_normalizer.py

Maps raw items from either Alibaba Apify actor this codebase has been
configured against into the supplier_data shape
storage.repository.SUPPLIER_WRITABLE_FIELDS expects.

Two shapes, both real, both supported
--------------------------------------
'curious_coder/alibaba-scraper' (the original actor, confirmed dead on
Apify's platform) returned a flat shape -- companyName, country, etc.
at the top level. Its replacement, 'zen-studio/alibaba-scraper' (see
scrapers/alibaba_scraper.py's own docstring for why it replaced the
dead one), nests everything meaningful under raw_data["supplier"] and,
one level deeper, raw_data["supplier"]["profile"] instead -- confirmed
against real output from a live --limit-capped run, not assumed from
the actor's input-schema docs (which don't describe the output shape
at all).

Rather than a hard cutover, normalise() tries the nested shape first
and falls back to the flat one -- the flat path, and every alias in
FIELD_ALIASES, is unchanged from before. This is the same lesson this
module's own original docstring already drew ("Apify actor output
field names aren't perfectly stable across actor versions/forks"), now
proven true a second time by an actual actor replacement rather than
just a version bump. If a third actor ever replaces this one, the
right move is another _normalise_<shape> method and a dispatch check
in normalise(), not a rewrite of either existing path.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from normalizers.base_normalizer import BaseNormalizer

logger = logging.getLogger(__name__)

# our_field -> possible source keys, checked in order, first non-empty wins.
# Used by the flat-shape path only (the old, dead actor's output shape).
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


def _parse_years(value: Any) -> int:
    """'5 yrs' -> 5, '5' -> 5, None/unparseable -> 0. Handles both
    actor shapes' own formatting (the new one always appends " yrs")."""
    if not value:
        return 0
    match = re.search(r"\d+", str(value))
    return int(match.group()) if match else 0


def _detect_certifications(supplier_data: Dict[str, Any], certifications: List[str]) -> None:
    """Shared by both parsing paths: turns a flat list of certification
    names/labels into the boolean columns downstream scoring/
    verification code actually reads. iatf_16949 and iso_ts_16949 both
    exist as separate columns for the same real-world standard (see
    storage/database.py) and are both set together deliberately --
    manufacturer_verifier.py reads the former, scorer.py the latter,
    and neither is populated by any other current normalizer either."""
    if not certifications:
        return
    supplier_data["other_certifications"] = certifications
    cert_text = " ".join(certifications).lower()
    supplier_data["iso_9001"] = "iso 9001" in cert_text or "iso9001" in cert_text
    if "iatf 16949" in cert_text or "iatf16949" in cert_text:
        supplier_data["iatf_16949"] = True
        supplier_data["iso_ts_16949"] = True
    supplier_data["e_mark_certified"] = any(
        token in cert_text for token in ("e-mark", "e mark", "ece")
    )


class AlibabaNormalizer(BaseNormalizer):
    source_name = "alibaba"

    def normalise(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        supplier = raw_data.get("supplier")
        if isinstance(supplier, dict):
            return self._normalise_nested(supplier)
        return self._normalise_flat(raw_data)

    # ── zen-studio/alibaba-scraper (resultType="suppliers") ──────────

    def _normalise_nested(self, supplier: Dict[str, Any]) -> Dict[str, Any]:
        profile = supplier.get("profile") if isinstance(supplier.get("profile"), dict) else {}

        company_name = self.clean_str(supplier.get("name") or profile.get("name"))
        if not company_name:
            logger.warning(
                "Alibaba record missing a company name (companyId=%s) — "
                "canonical_name will be empty; caller should skip creating a golden record.",
                supplier.get("companyId"),
            )

        profile_url = self.clean_str(supplier.get("profileUrl") or supplier.get("homeUrl"))
        domain = self._extract_domain(profile_url or self.clean_str(supplier.get("subDomain")))

        years_gold = _parse_years(supplier.get("goldYears"))
        perf = supplier.get("perf") if isinstance(supplier.get("perf"), dict) else {}
        rating = self.to_float(perf.get("reviewScore"))
        trade_assurance = bool(
            profile.get("tradeAssuranceIsDisplayed") or profile.get("tradeAssuranceIsService")
        )

        main_products = [
            self.clean_str(p.get("name"))
            for p in (supplier.get("mainProducts") or [])
            if isinstance(p, dict) and p.get("name")
        ]

        # Country comes back as an ISO alpha-2 code ("CN") -- converted
        # to the full name to match how country is stored everywhere
        # else in this database (see global_directory_scraper.py's
        # identical note on why: a bare code would silently break
        # search_suppliers_full's exact-match country filter).
        country = self._iso_country_name(supplier.get("country")) or self.clean_str(supplier.get("country")) or None

        supplier_data: Dict[str, Any] = {
            "canonical_name": company_name,
            "domain": domain,
            "country": country,
            "city": self.clean_str(supplier.get("city")) or None,
            "contact_name": self.clean_str(profile.get("contactName")) or None,
            "contact_title": self.clean_str(profile.get("jobTitle")) or None,
            "employee_count": self.clean_str(profile.get("employeesCount") or supplier.get("staffNumber")) or None,
            "product_keywords": main_products,
            "alibaba_url": profile_url or None,
            "alibaba_gold_supplier": years_gold > 0,
            "alibaba_years": years_gold or None,
            "alibaba_trade_assurance": trade_assurance,
            "alibaba_rating": rating,
        }
        # No primary_email/primary_phone here deliberately: Alibaba.com
        # doesn't expose either directly on a search result (only a
        # "contact via message" link, captured nowhere useful to store
        # as a contact channel) -- unlike the old actor's shape, which
        # apparently could. Not a regression to "fix"; it reflects what
        # the live site actually shows to an anonymous search.

        certifications = [
            self.clean_str(c.get("name"))
            for c in (supplier.get("certIconList") or [])
            if isinstance(c, dict) and c.get("name")
        ]
        _detect_certifications(supplier_data, certifications)

        photo_urls = [u for u in (supplier.get("factoryImages") or []) if isinstance(u, str) and u]
        if photo_urls:
            supplier_data["factory_photo_urls"] = photo_urls

        main_markets = profile.get("exportMarketCountries") or []
        if isinstance(main_markets, list) and main_markets:
            supplier_data.update(self.infer_export_flags_from_markets(main_markets))

        return {
            k: v for k, v in supplier_data.items()
            if k == "canonical_name" or v not in (None, "", [])
        }

    # ── curious_coder/alibaba-scraper (dead, flat shape) ──────────────

    def _normalise_flat(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
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
        _detect_certifications(supplier_data, certifications)

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

    @staticmethod
    def _iso_country_name(code: Any) -> Optional[str]:
        if not code or not isinstance(code, str) or len(code) != 2:
            return None
        try:
            import pycountry

            country = pycountry.countries.get(alpha_2=code.strip().upper())
            return country.name if country else None
        except Exception:
            return None
