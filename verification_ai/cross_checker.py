"""
verification_ai/cross_checker.py

Orchestrates the EXISTING verification modules
(verification.manufacturer_verifier, facility_address_verifier,
linkedin_presence, trust_signals, and capability findings already on
file) as independent sub-checks, and adds new cross-reference
consistency checks on top of them -- e.g. does the supplier's own
website (Collection Service evidence) actually corroborate the
canonical_name a listing claims, does a certification claimed on the
original listing show up anywhere in what CapabilityExtractor found on
the supplier's own site. Nothing existing is replaced or modified; this
is a synthesis layer on top, per the redesign plan's Verification
Service design (.claude/plans/deep-wibbling-rivest.md) -- recommended
there specifically to avoid throwing away working, already-tested code.

Never raises -- one sub-check failing (a verifier not configured, an
API error) must never abort the whole cross-check; each sub-check is
wrapped individually and a failure just means "no signal" for that one
check, same discipline as every other verification module in this
codebase.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from rapidfuzz import fuzz

from verification.facility_address_verifier import select_address_verifier
from verification.linkedin_presence import LinkedInPresenceChecker
from verification.manufacturer_verifier import ManufacturerVerifier
from verification.trust_signals import check_phone_validity
from verification.website_contact_extractor import country_name_to_region_code

logger = logging.getLogger(__name__)

# Same spirit as scrapers.company_website_finder's own
# _DEFAULT_MIN_NAME_SIMILARITY=55.0 threshold for "is this really the
# same company," applied here to own-site text instead of a fetched
# homepage as a whole.
_NAME_MATCH_THRESHOLD = 60.0


@dataclass
class SubCheckResult:
    name: str
    verdict: Optional[bool]  # True=corroborated, False=contradicted, None=no signal either way
    detail: str


@dataclass
class CrossCheckResult:
    sub_checks: List[SubCheckResult] = field(default_factory=list)
    inconsistencies: List[str] = field(default_factory=list)
    manufacturer_confidence: Optional[int] = None
    is_manufacturer: Optional[bool] = None


class CrossChecker:

    def __init__(
        self,
        manufacturer_verifier: Optional[ManufacturerVerifier] = None,
        google_places_verifier: Optional[Any] = None,
        amap_verifier: Optional[Any] = None,
        linkedin_checker: Optional[LinkedInPresenceChecker] = None,
    ):
        self.manufacturer_verifier = manufacturer_verifier or ManufacturerVerifier()
        # Deliberately NOT defaulted to real implementations the way
        # pipeline.orchestrator's constructor defaults every dependency --
        # address/LinkedIn verification cost real money per call, and a
        # cross-check that's silently missing them should degrade (skip
        # that sub-check) rather than construct a real, chargeable
        # verifier nobody asked for. VerificationService is what wires
        # in real implementations when actually running for real.
        self.google_places_verifier = google_places_verifier
        self.amap_verifier = amap_verifier
        self.linkedin_checker = linkedin_checker

    def run_checks(
        self,
        supplier: Dict[str, Any],
        *,
        collected_pages: Optional[List[Any]] = None,
        capability_findings: Optional[List[Dict[str, Any]]] = None,
    ) -> CrossCheckResult:
        result = CrossCheckResult()
        collected_pages = collected_pages or []
        capability_findings = capability_findings or []

        self._check_manufacturer_assessment(supplier, result)
        self._check_facility_address(supplier, result)
        self._check_linkedin_presence(supplier, result)
        self._check_phone_consistency(supplier, result)
        self._check_own_site_name_match(supplier, collected_pages, result)
        self._check_certification_consistency(supplier, capability_findings, result)
        self._check_export_shipment_evidence(supplier, result)

        return result

    def _check_manufacturer_assessment(self, supplier: Dict[str, Any], result: CrossCheckResult) -> None:
        try:
            assessment = self.manufacturer_verifier.assess(supplier)
            result.manufacturer_confidence = assessment["manufacturer_confidence"]
            result.is_manufacturer = assessment["is_manufacturer"]
            result.sub_checks.append(SubCheckResult(
                name="manufacturer_assessment", verdict=assessment["is_manufacturer"],
                detail=assessment["summary"],
            ))
        except Exception as e:
            logger.warning("cross_checker: manufacturer assessment failed: %s", e)

    def _check_facility_address(self, supplier: Dict[str, Any], result: CrossCheckResult) -> None:
        address = supplier.get("address")
        if not address or not (self.google_places_verifier or self.amap_verifier):
            return
        try:
            verifier = select_address_verifier(
                supplier.get("country"), self.google_places_verifier, self.amap_verifier,
            )
            addr_result = verifier.verify(address, company_name=supplier.get("canonical_name") or "")
            result.sub_checks.append(SubCheckResult(
                name="facility_address", verdict=addr_result.verified,
                detail=addr_result.reason or (addr_result.formatted_address or ""),
            ))
            if not addr_result.verified:
                result.inconsistencies.append(f"Claimed address could not be independently verified: {address}")
        except Exception as e:
            logger.warning("cross_checker: address verification failed: %s", e)

    def _check_linkedin_presence(self, supplier: Dict[str, Any], result: CrossCheckResult) -> None:
        if not self.linkedin_checker or not supplier.get("canonical_name"):
            return
        try:
            li_result = self.linkedin_checker.check(supplier["canonical_name"])
            result.sub_checks.append(SubCheckResult(
                name="linkedin_presence", verdict=li_result.presence_confirmed, detail=li_result.reason,
            ))
        except Exception as e:
            logger.warning("cross_checker: LinkedIn check failed: %s", e)

    def _check_phone_consistency(self, supplier: Dict[str, Any], result: CrossCheckResult) -> None:
        phone = supplier.get("primary_phone")
        if not phone:
            return
        try:
            region_hint = country_name_to_region_code(supplier.get("country"))
            phone_result = check_phone_validity(phone, default_region=region_hint)
            result.sub_checks.append(SubCheckResult(
                name="phone_format", verdict=phone_result.plausible,
                detail=f"{phone} plausible for region {region_hint or 'unknown'}: {phone_result.plausible}",
            ))
            if not phone_result.plausible:
                result.inconsistencies.append(
                    f"Phone number {phone} does not look valid for claimed country {supplier.get('country')}"
                )
        except Exception as e:
            logger.warning("cross_checker: phone validity check failed: %s", e)

    def _check_own_site_name_match(
        self, supplier: Dict[str, Any], collected_pages: List[Any], result: CrossCheckResult,
    ) -> None:
        """NEW cross-reference check -- does the supplier's own website
        (Collection Service or OwnWebsiteScraper evidence, anything
        with a `.text` attribute) actually state a company name that
        corroborates canonical_name?"""
        canonical_name = supplier.get("canonical_name") or ""
        if not canonical_name or not collected_pages:
            return
        try:
            best_score = 0.0
            for page in collected_pages:
                text = getattr(page, "text", "") or ""
                if not text:
                    continue
                best_score = max(best_score, fuzz.partial_ratio(canonical_name.lower(), text.lower()))
            corroborated = best_score >= _NAME_MATCH_THRESHOLD
            result.sub_checks.append(SubCheckResult(
                name="own_site_name_match", verdict=corroborated,
                detail=f"Best fuzzy match of '{canonical_name}' against own-site text: {best_score:.0f}/100",
            ))
            if not corroborated:
                result.inconsistencies.append(
                    f"Supplier's own website text does not clearly corroborate the name '{canonical_name}'"
                )
        except Exception as e:
            logger.warning("cross_checker: own-site name match failed: %s", e)

    def _check_certification_consistency(
        self, supplier: Dict[str, Any], capability_findings: List[Dict[str, Any]], result: CrossCheckResult,
    ) -> None:
        """NEW cross-reference check -- do certifications claimed on the
        original listing (iso_9001/iatf_16949/e_mark_certified booleans)
        show up anywhere in what CapabilityExtractor actually found on
        the supplier's own site? Only flags a mismatch if the own site
        DOES discuss standards at all but omits this specific one --
        silence (own site never discusses standards at all) is not
        itself a contradiction, since most sites simply don't."""
        claimed_certs = []
        if supplier.get("iso_9001"):
            claimed_certs.append("iso 9001")
        if supplier.get("iatf_16949"):
            claimed_certs.append("iatf 16949")
        if supplier.get("e_mark_certified"):
            claimed_certs.append("e-mark")
        if not claimed_certs or not capability_findings:
            return
        try:
            found_terms = {
                (f.get("canonical_term") or "").lower()
                for f in capability_findings if f.get("category") == "standard"
            }
            if not found_terms:
                return
            unconfirmed = [c for c in claimed_certs if c not in found_terms]
            if unconfirmed:
                result.inconsistencies.append(
                    f"Listing claims {', '.join(unconfirmed)}, but the supplier's own website's standards "
                    f"section doesn't mention it"
                )
                result.sub_checks.append(SubCheckResult(
                    name="certification_consistency", verdict=False,
                    detail=f"Unconfirmed on own site: {', '.join(unconfirmed)}",
                ))
            else:
                result.sub_checks.append(SubCheckResult(
                    name="certification_consistency", verdict=True,
                    detail="All claimed certifications corroborated by own-site capability findings",
                ))
        except Exception as e:
            logger.warning("cross_checker: certification consistency check failed: %s", e)

    def _check_export_shipment_evidence(self, supplier: Dict[str, Any], result: CrossCheckResult) -> None:
        """NEW cross-reference check -- does this supplier have real
        UK/US/EU customs shipment records on file (confirmed_shipments_uk/
        eu/us, populated by normalizers/trade_normalizer.py from
        ImportYeti/Volza, previously never read by verification at all)?
        Positive-only signal, deliberately: zero shipment records is NOT
        evidence a company doesn't manufacture -- most manufacturers
        simply won't appear in these specific customs lanes, and
        scrapers/global_trade_scraper.py's own docstring discloses its
        Volza selectors were never verified against a live site, so
        "found nothing" here is exactly as likely to mean "the scraper
        found nothing" as "there was nothing to find." Never contributes
        an inconsistency or a False verdict for that reason."""
        try:
            confirmed = (
                (supplier.get("confirmed_shipments_uk") or 0)
                + (supplier.get("confirmed_shipments_eu") or 0)
                + (supplier.get("confirmed_shipments_us") or 0)
            )
            if confirmed <= 0:
                return
            result.sub_checks.append(SubCheckResult(
                name="export_shipment_evidence", verdict=True,
                detail=f"{confirmed} confirmed shipment record(s) on file (UK/EU/US customs data)",
            ))
        except Exception as e:
            logger.warning("cross_checker: export shipment evidence check failed: %s", e)
