"""
verification/cert_checker.py

Certificate expiry monitoring — addresses Phase 1 Gap 5: ISO 9001
certificates are reissued roughly every 3 years, and E-mark (ECE type)
approvals can be withdrawn. This module doesn't call any external
registry itself (there's no single free public API for either); it
flags suppliers whose certificates are due for a manual/Qichacha-assisted
recheck, and does a lightweight format sanity-check on E-mark numbers.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from storage.repository import SupplierRepository

# ISO 9001 certificates are typically valid for 3 years between
# surveillance/recertification audits.
ISO_9001_VALIDITY_YEARS = 3

# E-mark (ECE type-approval) numbers roughly follow: "e" + a 1-3 digit
# country/approval-authority code, optionally a revision marker
# ("*NN" or a letter suffix), then a dash and an approval number, with
# an optional extension marker. This is a loose sanity check, not a
# full grammar per UN Regulation 1958 Agreement Schedule 4 — it exists
# to catch obviously-garbled scrape output, not to certify authenticity.
_E_MARK_RE = re.compile(
    r"^[eE]\s?\d{1,3}([\s\-\*]?\d{1,3}([Rr][\s\-\*]?\d{1,6})?[\s\-\*]?\d{0,8})?$"
)


def validate_e_mark_format(number: Optional[str]) -> bool:
    if not number:
        return False
    return bool(_E_MARK_RE.match(number.strip()))


class CertChecker:

    def __init__(self, repo: SupplierRepository):
        self.repo = repo

    # ═════════════════════════════════════════════════════
    # Status classification
    # ═════════════════════════════════════════════════════

    def iso_9001_status(
        self, supplier: Dict[str, Any], days_ahead: int = 90, today: Optional[date] = None
    ) -> str:
        """Returns one of: 'not_certified', 'valid', 'expiring_soon',
        'expired', 'unknown' (certified but no expiry date on file —
        this happens for records enriched from a source, like Alibaba,
        that doesn't expose an expiry date; it should be treated as a
        prompt to verify via Qichacha or ask the supplier directly,
        not as a red flag on its own)."""
        if not supplier.get("iso_9001"):
            return "not_certified"

        expiry = supplier.get("iso_9001_expiry")
        if not expiry:
            return "unknown"

        today = today or date.today()
        expiry_date = expiry if isinstance(expiry, date) else self._parse_date(str(expiry))
        if expiry_date is None:
            return "unknown"

        if expiry_date < today:
            return "expired"
        if expiry_date <= today + timedelta(days=days_ahead):
            return "expiring_soon"
        return "valid"

    def suggest_iso_9001_expiry(self, certified_date: date) -> date:
        """Given a known certification/audit date, estimate the expiry
        date using the standard 3-year ISO 9001 cycle. Useful when a
        source confirms a supplier is ISO 9001 certified but doesn't
        give an explicit expiry — better to record an estimate than
        leave the field empty and never revisit it."""
        try:
            return certified_date.replace(year=certified_date.year + ISO_9001_VALIDITY_YEARS)
        except ValueError:
            # Feb 29 on a non-leap target year
            return certified_date.replace(month=2, day=28, year=certified_date.year + ISO_9001_VALIDITY_YEARS)

    # ═════════════════════════════════════════════════════
    # Batch queries
    # ═════════════════════════════════════════════════════

    def get_suppliers_needing_recheck(self, days_ahead: int = 90) -> List[Dict[str, Any]]:
        """All ISO 9001-certified suppliers whose status is
        'expiring_soon', 'expired', or 'unknown' — i.e. everything
        except confirmed-valid, confirmed-not-certified."""
        certified = self.repo.get_suppliers_by_cert_flag("iso_9001", value=True)
        needing_recheck = []
        for supplier in certified:
            status = self.iso_9001_status(supplier, days_ahead=days_ahead)
            if status in ("expiring_soon", "expired", "unknown"):
                needing_recheck.append({**supplier, "iso_9001_status": status})
        return needing_recheck

    def get_suppliers_with_malformed_e_mark(self) -> List[Dict[str, Any]]:
        """E-mark certified suppliers whose stored e_mark_numbers don't
        pass even a loose format sanity check — worth a manual look,
        since it usually means a scraper picked up noise rather than a
        real approval number."""
        certified = self.repo.get_suppliers_by_cert_flag("e_mark_certified", value=True)
        flagged = []
        for supplier in certified:
            numbers = supplier.get("e_mark_numbers") or []
            if not numbers:
                flagged.append(supplier)
                continue
            if not all(validate_e_mark_format(n) for n in numbers):
                flagged.append(supplier)
        return flagged

    # ═════════════════════════════════════════════════════
    # Helpers
    # ═════════════════════════════════════════════════════

    @staticmethod
    def _parse_date(text: str) -> Optional[date]:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
