"""
verification/qichacha.py

Client for verifying a Chinese supplier's USCC against Qichacha (企查查),
a commercial Chinese business-registry data provider.

IMPORTANT — CONFIRM BEFORE LIVE USE:
Qichacha offers several API products (basic company search, "enterprise
verify" / 工商信息验证, risk reports, etc.) with different endpoint paths,
signing requirements, and response shapes depending on your contract
tier. The endpoint path, auth header names, and response field mappings
below are written against Qichacha's commonly-documented signing scheme
(AppKey + AppSecret -> MD5-based Token header) but have NOT been
verified against a live account. Before running this against real
traffic:
  1. Confirm QICHACHA_ENDPOINT_PATH against your actual API contract.
  2. Confirm the response field names in `_parse_response` — adjust
     RESPONSE_FIELD_ALIASES rather than the parsing logic itself.
  3. Confirm the signing scheme in `_build_auth_headers` matches what
     your contract's docs specify (some Qichacha products sign
     differently from others).

The HTTP client is injectable via the constructor specifically so this
class is fully unit-testable against canned responses without an API
key or network access — see tests/test_phase4.py.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any, Dict, List, Optional

import httpx

from config.settings import QICHACHA_API_KEY, QICHACHA_SECRET_KEY
from verification.uscc_validator import is_valid_uscc

logger = logging.getLogger(__name__)

QICHACHA_BASE_URL = "https://api.qichacha.com"
# CONFIRM: exact path depends on your Qichacha API product/contract.
QICHACHA_ENDPOINT_PATH = "/verify/getverifybaseinfo"

# our_field -> possible response key(s), checked in order. Adjust this
# table (not the parsing code) once you've confirmed real response shapes.
RESPONSE_FIELD_ALIASES: Dict[str, List[str]] = {
    "company_reg_number": ["CreditCode", "CreditNo", "RegNumber", "regno"],
    "canonical_name": ["Name", "EntName", "name"],
    "year_established": ["StartDate", "EstablishDate", "founded"],
    "employee_count": ["StaffSize", "EmployeeCount", "staffnum"],
    "is_manufacturer": ["EconKind", "EconKindCode", "OperScope", "econkind"],
    "company_status": ["ShortStatus", "Status", "status"],
    "address": ["Address", "address"],
    # The registered business scope (经营范围) is the single most
    # authoritative manufacturing-vs-trading signal available — it's
    # what the company legally declared to the registry, not a
    # self-reported platform claim. verification.manufacturer_verifier
    # parses this text directly rather than relying on EconKind alone.
    "business_scope": ["BusinessScope", "Scope", "OperScope", "scope"],
    # Registered capital (注册资本) — a red-flag signal for shell/trading
    # companies claiming to be manufacturers, NOT a revenue figure.
    # Previously this project mistakenly mapped this to
    # 'annual_revenue_usd' and then discarded it entirely; it's now
    # captured properly under its own field.
    "registered_capital_rmb": ["RegistCapi", "RegisteredCapital", "RegCap"],
}


class QichachaError(Exception):
    """Raised for configuration problems or unrecoverable API errors —
    not for 'company not found', which is a normal, valid response."""


class QichachaVerifier:

    def __init__(
        self,
        app_key: Optional[str] = None,
        app_secret: Optional[str] = None,
        http_client: Optional[httpx.Client] = None,
        enable_delays: bool = True,
    ):
        self.app_key = app_key or QICHACHA_API_KEY
        self.app_secret = app_secret or QICHACHA_SECRET_KEY
        self.enable_delays = enable_delays
        self._client = http_client or httpx.Client(timeout=15)

    def _build_auth_headers(self, timestamp: str) -> Dict[str, str]:
        """Qichacha's commonly-documented scheme: Token =
        MD5(MD5(AppKey + AppSecret + Timestamp)).upper(), sent alongside
        a Timespan header. CONFIRM against your contract's docs — some
        Qichacha API products use a different signing scheme entirely."""
        raw = f"{self.app_key}{self.app_secret}{timestamp}"
        inner_digest = hashlib.md5(raw.encode("utf-8")).hexdigest()
        token = hashlib.md5(inner_digest.encode("utf-8")).hexdigest().upper()
        return {"Token": token, "Timespan": timestamp}

    def verify(self, uscc: str) -> Dict[str, Any]:
        """
        Verify a USCC against Qichacha. Returns a dict shaped for
        SupplierRepository.update_verification() — always includes
        'uscc_verified' (bool); includes additional confirmed fields
        (company_reg_number, year_established, etc.) only when the API
        returned them.

        Never raises for "not found" or "no match" — only for
        configuration problems (missing keys) or transport failures,
        both surfaced as an 'error' key in the returned dict so a batch
        verification job can log and continue rather than crash.
        """
        if not is_valid_uscc(uscc):
            return {"uscc_verified": False, "error": "invalid_uscc_format"}

        if not self.app_key or not self.app_secret:
            raise QichachaError(
                "QICHACHA_API_KEY / QICHACHA_SECRET_KEY are not configured. "
                "Set them in your .env file before calling QichachaVerifier.verify(), "
                "or inject app_key=/app_secret= directly for testing."
            )

        timestamp = str(int(time.time()))
        headers = self._build_auth_headers(timestamp)
        params = {"key": self.app_key, "keyword": uscc}

        try:
            response = self._client.get(
                f"{QICHACHA_BASE_URL}{QICHACHA_ENDPOINT_PATH}",
                params=params,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error("Qichacha lookup failed for %s: %s", uscc, e)
            return {"uscc_verified": False, "error": str(e)}

        return self._parse_response(data, uscc)

    def _parse_response(self, data: Dict[str, Any], uscc: str) -> Dict[str, Any]:
        # Qichacha's typical envelope is {"Status": "200", "Result": {...}, "Message": "..."}
        # A non-"200" status or missing Result means no match was found.
        status = str(data.get("Status", ""))
        result = data.get("Result")

        if status != "200" or not result:
            logger.info("Qichacha: no verified match for USCC %s (status=%s)", uscc, status)
            return {"uscc_verified": False}

        verification: Dict[str, Any] = {"uscc_verified": True}

        for our_field, aliases in RESPONSE_FIELD_ALIASES.items():
            value = self._first_present(result, aliases)
            if value is None:
                continue
            if our_field == "is_manufacturer":
                inferred = self._infer_is_manufacturer(value)
                if inferred is not None:
                    verification["is_manufacturer"] = inferred
            elif our_field == "year_established":
                verification["year_established"] = self._extract_year(value)
            elif our_field == "registered_capital_rmb":
                verification["registered_capital_rmb"] = self._extract_number(value)
            else:
                verification[our_field] = value

        return verification

    @staticmethod
    def _first_present(data: Dict[str, Any], keys: List[str]) -> Any:
        for key in keys:
            if key in data and data[key] not in (None, "", []):
                return data[key]
        return None

    @staticmethod
    def _infer_is_manufacturer(econ_kind_value: Any) -> Optional[bool]:
        """Heuristic only: Qichacha's business-scope / economic-kind
        text sometimes distinguishes manufacturing from trading, but
        this needs tuning against real response samples — treat the
        result as a signal to combine with other sources, not ground truth."""
        text = str(econ_kind_value).lower()
        manufacturing_markers = ("manufactur", "生产", "制造", "factory", "工厂")
        trading_markers = ("trading", "贸易", "商贸", "trade co")
        if any(m in text for m in manufacturing_markers):
            return True
        if any(m in text for m in trading_markers):
            return False
        return None

    @staticmethod
    def _extract_year(value: Any) -> Optional[int]:
        text = str(value)
        digits = "".join(c for c in text[:4] if c.isdigit())
        return int(digits) if len(digits) == 4 else None

    @staticmethod
    def _extract_number(value: Any) -> Optional[float]:
        """Parses a registered-capital figure that may be a plain number
        or Chinese financial notation like '1000万元人民币' (10,000,000
        RMB). '万' (wàn) is a factor of 10,000 and ubiquitous in Chinese
        company registry data — stripping it out along with other
        non-digit characters would silently turn 10,000,000 into 1,000,
        a 10,000x error, so it's handled explicitly here rather than by
        the generic digit-stripping pattern used elsewhere in this codebase."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value)
        multiplier = 10_000 if "万" in text else 1
        digits = re.sub(r"[^\d.]", "", text)
        try:
            return float(digits) * multiplier if digits else None
        except ValueError:
            return None
