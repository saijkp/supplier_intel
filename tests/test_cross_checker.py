"""
tests/test_cross_checker.py

Tests for verification_ai/cross_checker.py -- orchestrates existing
verification modules (manufacturer_verifier runs for real, pure/no
external calls) plus injectable fakes for the paid-API-backed ones
(address, LinkedIn), and the new cross-reference checks (phone-country
consistency, own-site name match, certification consistency).
"""

from __future__ import annotations

from types import SimpleNamespace

from verification.facility_address_verifier import AddressVerificationResult
from verification.linkedin_presence import LinkedInPresenceResult
from verification_ai.cross_checker import CrossChecker


class FakeAddressVerifier:
    def __init__(self, result=None):
        self._result = result or AddressVerificationResult(
            verified=True, source="google_places", formatted_address="123 Real St", reason="ok",
        )
        self.calls = []

    def verify(self, address, company_name=""):
        self.calls.append((address, company_name))
        return self._result


class ExplodingAddressVerifier:
    def verify(self, address, company_name=""):
        raise RuntimeError("API down")


class FakeLinkedInChecker:
    def __init__(self, result=None):
        self._result = result or LinkedInPresenceResult(
            company_name="", presence_confirmed=True,
            linkedin_url="https://linkedin.com/company/acme", snippet="500 employees", reason="found",
        )
        self.calls = []

    def check(self, company_name):
        self.calls.append(company_name)
        return self._result


def _page(text):
    return SimpleNamespace(url="https://acme.example.com", text=text)


class TestManufacturerAssessmentSubCheck:

    def test_runs_the_real_manufacturer_verifier(self):
        checker = CrossChecker()
        result = checker.run_checks({
            "canonical_name": "Acme", "business_scope": "manufacturing of trailer axles",
            "iso_9001": True,
        })
        assert result.manufacturer_confidence is not None
        names = {c.name for c in result.sub_checks}
        assert "manufacturer_assessment" in names


class TestFacilityAddressSubCheck:

    def test_verified_address_produces_a_true_verdict(self):
        google = FakeAddressVerifier(AddressVerificationResult(
            verified=True, source="google_places", formatted_address="123 Real St, UK", reason="ok",
        ))
        checker = CrossChecker(google_places_verifier=google, amap_verifier=FakeAddressVerifier())
        result = checker.run_checks({"canonical_name": "Acme", "address": "123 Real St", "country": "United Kingdom"})

        addr_check = next(c for c in result.sub_checks if c.name == "facility_address")
        assert addr_check.verdict is True
        assert result.inconsistencies == []

    def test_unverified_address_produces_false_verdict_and_inconsistency(self):
        google = FakeAddressVerifier(AddressVerificationResult(
            verified=False, source="google_places", formatted_address=None, reason="no match",
        ))
        checker = CrossChecker(google_places_verifier=google, amap_verifier=FakeAddressVerifier())
        result = checker.run_checks({"canonical_name": "Acme", "address": "fake address", "country": "United Kingdom"})

        addr_check = next(c for c in result.sub_checks if c.name == "facility_address")
        assert addr_check.verdict is False
        assert len(result.inconsistencies) == 1

    def test_no_address_on_file_skips_the_check(self):
        google = FakeAddressVerifier()
        checker = CrossChecker(google_places_verifier=google, amap_verifier=FakeAddressVerifier())
        result = checker.run_checks({"canonical_name": "Acme", "address": None})
        assert not any(c.name == "facility_address" for c in result.sub_checks)
        assert google.calls == []

    def test_no_verifiers_configured_skips_gracefully(self):
        checker = CrossChecker()  # no google_places_verifier/amap_verifier given
        result = checker.run_checks({"canonical_name": "Acme", "address": "123 Real St"})
        assert not any(c.name == "facility_address" for c in result.sub_checks)

    def test_verifier_raising_does_not_abort_the_whole_check(self):
        checker = CrossChecker(google_places_verifier=ExplodingAddressVerifier(), amap_verifier=FakeAddressVerifier())
        result = checker.run_checks({"canonical_name": "Acme", "address": "123 Real St"})  # must not raise
        assert not any(c.name == "facility_address" for c in result.sub_checks)

    def test_unavailable_source_is_no_signal_never_a_false_negative(self):
        """Real production bug this guards against: a configured but
        broken check (API key not authorised for the API, a request
        timeout, etc. -- anything AddressVerifier.verify() reports as
        source="unavailable") is NOT evidence the address is fake. Before
        this fix, ANY verified=False result -- including "the check
        itself couldn't run" -- was recorded as a contradicted signal
        and an inconsistency, exactly what happened in production when
        GOOGLE_PLACES_API_KEY was present but not authorised for the
        Places API."""
        google = FakeAddressVerifier(AddressVerificationResult(
            verified=False, source="unavailable", formatted_address=None,
            reason="Google Places API error: REQUEST_DENIED -- key not authorised",
        ))
        checker = CrossChecker(google_places_verifier=google, amap_verifier=FakeAddressVerifier())
        result = checker.run_checks({"canonical_name": "Acme", "address": "123 Real St", "country": "United Kingdom"})

        assert not any(c.name == "facility_address" for c in result.sub_checks)
        assert result.inconsistencies == []


class TestLinkedInSubCheck:

    def test_presence_confirmed_produces_true_verdict(self):
        checker = CrossChecker(linkedin_checker=FakeLinkedInChecker())
        result = checker.run_checks({"canonical_name": "Acme"})
        li_check = next(c for c in result.sub_checks if c.name == "linkedin_presence")
        assert li_check.verdict is True

    def test_no_checker_configured_skips_the_check(self):
        checker = CrossChecker()
        result = checker.run_checks({"canonical_name": "Acme"})
        assert not any(c.name == "linkedin_presence" for c in result.sub_checks)


class TestPhoneConsistencySubCheck:

    def test_valid_uk_number_for_uk_supplier_is_confirmed(self):
        checker = CrossChecker()
        result = checker.run_checks({
            "canonical_name": "Acme", "primary_phone": "+44 20 7946 0958", "country": "United Kingdom",
        })
        phone_check = next(c for c in result.sub_checks if c.name == "phone_format")
        assert phone_check.verdict is True
        assert result.inconsistencies == []

    def test_implausible_number_is_flagged_as_an_inconsistency(self):
        checker = CrossChecker()
        result = checker.run_checks({
            "canonical_name": "Acme", "primary_phone": "123", "country": "United Kingdom",
        })
        phone_check = next(c for c in result.sub_checks if c.name == "phone_format")
        assert phone_check.verdict is False
        assert len(result.inconsistencies) == 1

    def test_no_phone_on_file_skips_the_check(self):
        checker = CrossChecker()
        result = checker.run_checks({"canonical_name": "Acme", "primary_phone": None})
        assert not any(c.name == "phone_format" for c in result.sub_checks)


class TestOwnSiteNameMatchSubCheck:

    def test_matching_name_on_own_site_is_corroborated(self):
        checker = CrossChecker()
        result = checker.run_checks(
            {"canonical_name": "Acme Trailer Manufacturing Co"},
            collected_pages=[_page("Welcome to Acme Trailer Manufacturing Co, est. 1998.")],
        )
        check = next(c for c in result.sub_checks if c.name == "own_site_name_match")
        assert check.verdict is True
        assert result.inconsistencies == []

    def test_unrelated_site_text_is_not_corroborated(self):
        checker = CrossChecker()
        result = checker.run_checks(
            {"canonical_name": "Acme Trailer Manufacturing Co"},
            collected_pages=[_page("This page is about something else entirely, gardening tips.")],
        )
        check = next(c for c in result.sub_checks if c.name == "own_site_name_match")
        assert check.verdict is False
        assert len(result.inconsistencies) == 1

    def test_no_collected_pages_skips_the_check(self):
        checker = CrossChecker()
        result = checker.run_checks({"canonical_name": "Acme"}, collected_pages=[])
        assert not any(c.name == "own_site_name_match" for c in result.sub_checks)


class TestCertificationConsistencySubCheck:

    def test_claimed_cert_confirmed_by_capability_findings(self):
        checker = CrossChecker()
        result = checker.run_checks(
            {"canonical_name": "Acme", "iso_9001": True},
            capability_findings=[{"canonical_term": "iso 9001", "category": "standard"}],
        )
        check = next(c for c in result.sub_checks if c.name == "certification_consistency")
        assert check.verdict is True
        assert result.inconsistencies == []

    def test_claimed_cert_not_found_on_own_site_flags_inconsistency(self):
        checker = CrossChecker()
        result = checker.run_checks(
            {"canonical_name": "Acme", "iso_9001": True},
            capability_findings=[{"canonical_term": "ce marking", "category": "standard"}],
        )
        check = next(c for c in result.sub_checks if c.name == "certification_consistency")
        assert check.verdict is False
        assert len(result.inconsistencies) == 1

    def test_own_site_never_discusses_standards_is_not_an_inconsistency(self):
        """Silence isn't itself a contradiction -- most sites simply
        don't discuss standards at all."""
        checker = CrossChecker()
        result = checker.run_checks(
            {"canonical_name": "Acme", "iso_9001": True},
            capability_findings=[{"canonical_term": "rotomoulding", "category": "process"}],
        )
        assert not any(c.name == "certification_consistency" for c in result.sub_checks)
        assert result.inconsistencies == []

    def test_no_claimed_certs_skips_the_check(self):
        checker = CrossChecker()
        result = checker.run_checks(
            {"canonical_name": "Acme"},
            capability_findings=[{"canonical_term": "iso 9001", "category": "standard"}],
        )
        assert not any(c.name == "certification_consistency" for c in result.sub_checks)


class TestExportShipmentEvidenceSubCheck:

    def test_confirmed_uk_shipments_produce_a_true_verdict(self):
        checker = CrossChecker()
        result = checker.run_checks({"canonical_name": "Acme", "confirmed_shipments_uk": 3})
        check = next(c for c in result.sub_checks if c.name == "export_shipment_evidence")
        assert check.verdict is True
        assert "3" in check.detail

    def test_shipments_across_multiple_regions_are_summed(self):
        checker = CrossChecker()
        result = checker.run_checks({
            "canonical_name": "Acme",
            "confirmed_shipments_uk": 1, "confirmed_shipments_eu": 2, "confirmed_shipments_us": 4,
        })
        check = next(c for c in result.sub_checks if c.name == "export_shipment_evidence")
        assert "7" in check.detail

    def test_no_shipment_data_produces_no_signal_never_a_false_verdict(self):
        """Absence of UK/EU/US customs data is not evidence a company
        doesn't manufacture -- must never contribute a False verdict or
        an inconsistency, only silence."""
        checker = CrossChecker()
        result = checker.run_checks({"canonical_name": "Acme"})
        assert not any(c.name == "export_shipment_evidence" for c in result.sub_checks)
        assert result.inconsistencies == []

    def test_zero_shipment_counts_produce_no_signal(self):
        checker = CrossChecker()
        result = checker.run_checks({
            "canonical_name": "Acme",
            "confirmed_shipments_uk": 0, "confirmed_shipments_eu": 0, "confirmed_shipments_us": 0,
        })
        assert not any(c.name == "export_shipment_evidence" for c in result.sub_checks)
