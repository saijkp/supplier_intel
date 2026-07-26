"""
tests/test_phase4.py

Phase 4 test suite: USCC validation, Qichacha verification (mocked HTTP),
certificate expiry monitoring, and the HKTDC scraper/normalizer (mocked
HTTP for the scraper).
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest

from verification.uscc_validator import (
    has_valid_format,
    has_valid_checksum,
    is_valid_uscc,
    compute_check_character,
    region_code,
)
from verification.qichacha import QichachaVerifier, QichachaError
from verification.cert_checker import CertChecker, validate_e_mark_format, ISO_9001_VALIDITY_YEARS
from scrapers.hktdc_scraper import HKTDCScraper, HKTDC_SELECTORS
from normalizers.hktdc_normalizer import HKTDCNormalizer
from storage.database import initialise_schema
from storage.repository import SupplierRepository

# Checksum-valid USCC computed via the standard GB 32100-2015 algorithm
# (prefix "91440101MA5ABCDE1" -> check char "M").
VALID_USCC = "91440101MA5ABCDE1M"


# ═════════════════════════════════════════════════════════════
# USCC validator
# ═════════════════════════════════════════════════════════════

class TestUSCCValidator:

    def test_valid_uscc_passes_format_and_checksum(self):
        assert has_valid_format(VALID_USCC) is True
        assert has_valid_checksum(VALID_USCC) is True
        assert is_valid_uscc(VALID_USCC) is True

    def test_tampered_check_digit_fails_checksum(self):
        tampered = VALID_USCC[:-1] + ("A" if VALID_USCC[-1] != "A" else "B")
        assert has_valid_format(tampered) is True  # still well-formed
        assert has_valid_checksum(tampered) is False
        assert is_valid_uscc(tampered) is False

    def test_tampered_middle_character_fails_checksum(self):
        tampered = VALID_USCC[:5] + ("9" if VALID_USCC[5] != "9" else "8") + VALID_USCC[6:]
        assert is_valid_uscc(tampered) is False

    def test_wrong_length_fails_format(self):
        assert has_valid_format(VALID_USCC[:-1]) is False
        assert has_valid_format(VALID_USCC + "1") is False
        assert is_valid_uscc(VALID_USCC[:-1]) is False

    def test_invalid_characters_fail_format(self):
        # 'I', 'O', 'S', 'V', 'Z' are excluded from the USCC alphabet
        assert has_valid_format("9144O1O1MA5ABCDE1M") is False

    def test_lowercase_input_normalised(self):
        assert is_valid_uscc(VALID_USCC.lower()) is True

    def test_empty_and_none_input(self):
        assert is_valid_uscc("") is False
        assert is_valid_uscc(None) is False
        assert has_valid_format("") is False

    def test_compute_check_character_matches_known_value(self):
        assert compute_check_character(VALID_USCC[:17]) == VALID_USCC[17]

    def test_region_code_extraction(self):
        assert region_code(VALID_USCC) == "440101"

    def test_region_code_none_for_invalid_input(self):
        assert region_code("not-a-uscc") is None


# ═════════════════════════════════════════════════════════════
# QichachaVerifier — fake HTTP client
# ═════════════════════════════════════════════════════════════

class FakeQichachaResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class FakeQichachaClient:
    def __init__(self, response_json, status_code=200):
        self._response_json = response_json
        self._status_code = status_code
        self.last_request = None

    def get(self, url, params=None, headers=None):
        self.last_request = {"url": url, "params": params, "headers": headers}
        return FakeQichachaResponse(self._response_json, self._status_code)


FOUND_RESPONSE = {
    "Status": "200",
    "Message": "OK",
    "Result": {
        "Name": "Shenzhen LED Masters Co Ltd",
        "CreditCode": VALID_USCC,
        "StartDate": "2015-03-12",
        "StaffSize": "100-500",
        "EconKind": "Manufacturing Enterprise",
        "ShortStatus": "Active",
        "Address": "Shenzhen, Guangdong",
    },
}

NOT_FOUND_RESPONSE = {"Status": "201", "Message": "No matching enterprise found", "Result": None}


class TestQichachaVerifier:

    def test_verify_rejects_malformed_uscc_without_http_call(self):
        client = FakeQichachaClient(FOUND_RESPONSE)
        verifier = QichachaVerifier(app_key="k", app_secret="s", http_client=client)

        result = verifier.verify("not-a-real-uscc")

        assert result == {"uscc_verified": False, "error": "invalid_uscc_format"}
        assert client.last_request is None  # never even attempted the HTTP call

    def test_verify_raises_without_credentials(self):
        verifier = QichachaVerifier(app_key=None, app_secret=None, http_client=FakeQichachaClient(FOUND_RESPONSE))
        with pytest.raises(QichachaError, match="QICHACHA_API_KEY"):
            verifier.verify(VALID_USCC)

    def test_verify_parses_found_response(self):
        client = FakeQichachaClient(FOUND_RESPONSE)
        verifier = QichachaVerifier(app_key="k", app_secret="s", http_client=client)

        result = verifier.verify(VALID_USCC)

        assert result["uscc_verified"] is True
        assert result["company_reg_number"] == VALID_USCC
        assert result["year_established"] == 2015
        assert result["is_manufacturer"] is True
        assert result["employee_count"] == "100-500"

    def test_verify_handles_not_found(self):
        client = FakeQichachaClient(NOT_FOUND_RESPONSE)
        verifier = QichachaVerifier(app_key="k", app_secret="s", http_client=client)

        result = verifier.verify(VALID_USCC)
        assert result == {"uscc_verified": False}

    def test_verify_handles_transport_error(self):
        class FailingClient:
            def get(self, *args, **kwargs):
                raise httpx.ConnectError("connection refused")

        verifier = QichachaVerifier(app_key="k", app_secret="s", http_client=FailingClient())
        result = verifier.verify(VALID_USCC)

        assert result["uscc_verified"] is False
        assert "connection refused" in result["error"]

    def test_auth_headers_sent_with_request(self):
        client = FakeQichachaClient(FOUND_RESPONSE)
        verifier = QichachaVerifier(app_key="k", app_secret="s", http_client=client)
        verifier.verify(VALID_USCC)

        assert "Token" in client.last_request["headers"]
        assert "Timespan" in client.last_request["headers"]
        assert client.last_request["params"]["keyword"] == VALID_USCC

    def test_trading_econ_kind_maps_to_not_manufacturer(self):
        response = {**FOUND_RESPONSE, "Result": {**FOUND_RESPONSE["Result"], "EconKind": "Trading Co"}}
        client = FakeQichachaClient(response)
        verifier = QichachaVerifier(app_key="k", app_secret="s", http_client=client)

        result = verifier.verify(VALID_USCC)
        assert result["is_manufacturer"] is False

    def test_verify_output_compatible_with_repository_update(self, tmp_path):
        db_path = tmp_path / "test.db"
        initialise_schema(db_path)
        repo = SupplierRepository(db_path=db_path)
        supplier_id = repo.create_golden_record({"canonical_name": "Foo Co", "uscc": VALID_USCC, "country": "China"})

        client = FakeQichachaClient(FOUND_RESPONSE)
        verifier = QichachaVerifier(app_key="k", app_secret="s", http_client=client)
        result = verifier.verify(VALID_USCC)

        repo.update_verification(supplier_id, result)
        supplier = repo.get_supplier(supplier_id)
        assert supplier["uscc_verified"] == 1
        assert supplier["year_established"] == 2015


# ═════════════════════════════════════════════════════════════
# CertChecker
# ═════════════════════════════════════════════════════════════

@pytest.fixture()
def repo(tmp_path):
    db_path = tmp_path / "test.db"
    initialise_schema(db_path)
    return SupplierRepository(db_path=db_path)


@pytest.fixture()
def cert_checker(repo):
    return CertChecker(repo)


class TestCertChecker:

    def test_not_certified(self, cert_checker):
        assert cert_checker.iso_9001_status({"iso_9001": 0}) == "not_certified"

    def test_certified_without_expiry_is_unknown(self, cert_checker):
        assert cert_checker.iso_9001_status({"iso_9001": 1}) == "unknown"

    def test_certified_valid(self, cert_checker):
        future = (date.today() + timedelta(days=400)).isoformat()
        assert cert_checker.iso_9001_status({"iso_9001": 1, "iso_9001_expiry": future}) == "valid"

    def test_certified_expiring_soon(self, cert_checker):
        soon = (date.today() + timedelta(days=30)).isoformat()
        assert cert_checker.iso_9001_status({"iso_9001": 1, "iso_9001_expiry": soon}, days_ahead=90) == "expiring_soon"

    def test_certified_expired(self, cert_checker):
        past = (date.today() - timedelta(days=10)).isoformat()
        assert cert_checker.iso_9001_status({"iso_9001": 1, "iso_9001_expiry": past}) == "expired"

    def test_malformed_expiry_date_is_unknown(self, cert_checker):
        assert cert_checker.iso_9001_status({"iso_9001": 1, "iso_9001_expiry": "not-a-date"}) == "unknown"

    def test_suggest_iso_9001_expiry(self, cert_checker):
        certified = date(2024, 3, 12)
        expiry = cert_checker.suggest_iso_9001_expiry(certified)
        assert expiry == date(2024 + ISO_9001_VALIDITY_YEARS, 3, 12)

    def test_suggest_iso_9001_expiry_handles_leap_day(self, cert_checker):
        certified = date(2024, 2, 29)  # 2024 is a leap year
        expiry = cert_checker.suggest_iso_9001_expiry(certified)
        assert expiry == date(2027, 2, 28)  # 2027 is not a leap year

    def test_get_suppliers_needing_recheck(self, repo, cert_checker):
        expired_id = repo.create_golden_record({
            "canonical_name": "Expired Co", "iso_9001": True,
            "iso_9001_expiry": (date.today() - timedelta(days=5)).isoformat(),
        })
        valid_id = repo.create_golden_record({
            "canonical_name": "Valid Co", "iso_9001": True,
            "iso_9001_expiry": (date.today() + timedelta(days=800)).isoformat(),
        })
        unknown_id = repo.create_golden_record({"canonical_name": "Unknown Expiry Co", "iso_9001": True})
        repo.create_golden_record({"canonical_name": "Not Certified Co", "iso_9001": False})

        needing_recheck = cert_checker.get_suppliers_needing_recheck()
        ids = {s["id"] for s in needing_recheck}

        assert expired_id in ids
        assert unknown_id in ids
        assert valid_id not in ids

    def test_validate_e_mark_format_accepts_typical_numbers(self):
        assert validate_e_mark_format("e11*10R-011952*00") is True
        assert validate_e_mark_format("E4 021234") is True
        assert validate_e_mark_format("e1-12345") is True

    def test_validate_e_mark_format_rejects_garbage(self):
        assert validate_e_mark_format("") is False
        assert validate_e_mark_format(None) is False
        assert validate_e_mark_format("totally not a mark") is False

    def test_get_suppliers_with_malformed_e_mark(self, repo, cert_checker):
        good_id = repo.create_golden_record({
            "canonical_name": "Good Co", "e_mark_certified": True,
            "e_mark_numbers": ["e11*10R-011952*00"],
        })
        bad_id = repo.create_golden_record({
            "canonical_name": "Bad Co", "e_mark_certified": True,
            "e_mark_numbers": ["garbage-not-a-mark"],
        })
        missing_id = repo.create_golden_record({
            "canonical_name": "Missing Numbers Co", "e_mark_certified": True,
        })

        flagged = cert_checker.get_suppliers_with_malformed_e_mark()
        flagged_ids = {s["id"] for s in flagged}

        assert bad_id in flagged_ids
        assert missing_id in flagged_ids
        assert good_id not in flagged_ids


# ═════════════════════════════════════════════════════════════
# HKTDCScraper — fake httpx client
# ═════════════════════════════════════════════════════════════

def _hktdc_card_html(name, country="China", products=("LED marker lights",)):
    product_tags = "".join(f'<span class="product-tag">{p}</span>' for p in products)
    return f"""
    <div class="supplier-list-item">
        <h3 class="company-name">{name}</h3>
        <div class="country">{country}</div>
        {product_tags}
        <a class="company-link" href="https://{name.lower().replace(' ', '')}.com">Website</a>
        <div class="description">Manufacturer of trailer components</div>
        <a class="profile-link" href="/profile/{name.lower().replace(' ', '-')}">Profile</a>
    </div>
    """


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class FakeHKTDCClient:
    def __init__(self, pages):
        self._pages = pages
        self.requested_urls = []

    def get(self, url):
        self.requested_urls.append(url)
        page_num = int(url.split("page=")[-1])
        if page_num <= len(self._pages):
            return FakeResponse(self._pages[page_num - 1])
        return FakeResponse("<html><body>no results</body></html>")


class TestHKTDCScraper:

    def test_scrape_parses_supplier_cards(self):
        page1 = f"""
        <html><body>
        {_hktdc_card_html("LED Masters HK")}
        {_hktdc_card_html("Fastener Traders Ltd")}
        </body></html>
        """
        client = FakeHKTDCClient(pages=[page1])
        scraper = HKTDCScraper(http_client=client, enable_delays=False)

        results = scraper.scrape("LED lighting", max_pages=3)

        assert len(results) == 2
        assert results[0].source == "hktdc"
        assert results[0].raw_data["company_name"] == "LED Masters HK"
        assert "LED marker lights" in results[0].raw_data["products"]

    def test_scrape_stops_at_empty_page(self):
        page1 = f"<html><body>{_hktdc_card_html('Shipper A')}</body></html>"
        client = FakeHKTDCClient(pages=[page1])
        scraper = HKTDCScraper(http_client=client, enable_delays=False)

        results = scraper.scrape("widgets", max_pages=5)
        assert len(results) == 1
        assert len(client.requested_urls) == 2

    def test_scrape_returns_error_result_on_first_page_failure(self):
        class FailingClient:
            def get(self, url):
                raise httpx.ConnectError("connection refused")

        scraper = HKTDCScraper(http_client=FailingClient(), enable_delays=False)
        results = scraper.scrape("widgets")
        assert len(results) == 1
        assert results[0].success is False

    def test_selectors_config_used(self):
        assert "supplier_card" in HKTDC_SELECTORS


# ═════════════════════════════════════════════════════════════
# HKTDCNormalizer
# ═════════════════════════════════════════════════════════════

class TestHKTDCNormalizer:

    def test_normalise_maps_fields(self):
        normalizer = HKTDCNormalizer()
        raw = {
            "company_name": "LED Masters HK",
            "country": "Hong Kong",
            "products": ["LED marker lights", "trailer lighting"],
            "website": "https://www.ledmastershk.com/en",
            "description": "Manufacturer of trailer lighting components",
            "hktdc_profile_url": "/profile/led-masters-hk",
        }
        result = normalizer.normalise(raw)

        assert result["canonical_name"] == "LED Masters HK"
        assert result["domain"] == "ledmastershk.com"
        assert result["country"] == "Hong Kong"
        assert result["product_keywords"] == ["LED marker lights", "trailer lighting"]
        assert result["hktdc_url"] == "/profile/led-masters-hk"
        assert result["moq_notes"] == "Manufacturer of trailer lighting components"

    def test_normalise_missing_name(self):
        normalizer = HKTDCNormalizer()
        result = normalizer.normalise({"country": "China"})
        assert result["canonical_name"] == ""

    def test_normalise_drops_empty_fields(self):
        normalizer = HKTDCNormalizer()
        result = normalizer.normalise({"company_name": "Bare Co"})
        assert "domain" not in result
        assert "hktdc_url" not in result

    def test_normalise_output_compatible_with_repository(self, tmp_path):
        db_path = tmp_path / "test.db"
        initialise_schema(db_path)
        repo = SupplierRepository(db_path=db_path)
        normalizer = HKTDCNormalizer()

        supplier_data = normalizer.normalise({
            "company_name": "LED Masters HK",
            "country": "Hong Kong",
            "products": ["LED marker lights"],
            "website": "ledmastershk.com",
        })
        supplier_id = repo.create_golden_record(supplier_data)
        supplier = repo.get_supplier(supplier_id)

        assert supplier["canonical_name"] == "LED Masters HK"
        assert supplier["product_keywords"] == ["LED marker lights"]
