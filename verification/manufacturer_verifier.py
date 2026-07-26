"""
verification/manufacturer_verifier.py

The core check for this platform's actual purpose: distinguishing real
manufacturing operations from trading companies that present themselves
as manufacturers on B2B platforms.

Before this module, "is_manufacturer" was set by weak keyword-matching
against self-reported platform text (Alibaba certification blurbs,
IndiaMART's free-text "nature of business" field) and
`manufacturer_confidence` was a schema column nothing ever populated.
This module aggregates several independent, mostly-authoritative
signals into one score and verdict:

  - Registered business scope (from Qichacha) — what the company
    legally declared to the government registry, not what it claims on
    a sales page. The single strongest signal available.
  - Registered capital — unusually low capital for a claimed
    manufacturing operation is a real red flag.
  - Platform tenure vs. registered incorporation date — a mismatch
    suggests a transferred/rebranded storefront, not a stable factory.
  - Production-relevant certifications (ISO 9001, IATF 16949, E-mark) —
    a pure trading company has no reason to hold these.

Every signal that fires is recorded as a plain-English string, not just
folded into an opaque number — a procurement professional reviewing a
supplier should be able to see *why* it was judged that way.

NOT YET WIRED IN: a factory-photo visual check (see
verification.factory_photo_verifier.FactoryPhotoVerifier) is a
genuinely valuable fifth signal but requires downloading photos from
each platform, which no scraper in this codebase does yet — that's the
natural next increment, not something folded into this module's
`assess()` today.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Business-scope text markers. Chinese company registrations state their
# permitted business scope in filed text (经营范围) — this is the single
# most authoritative signal available, since it's what the company
# legally declared to the registry, not what it claims on a sales page.
MANUFACTURING_SCOPE_MARKERS: Tuple[str, ...] = (
    "生产", "制造", "加工", "研发", "制作",
    "manufactur", "production of", "processing of", "fabrication",
)
TRADING_SCOPE_MARKERS: Tuple[str, ...] = (
    "销售", "批发", "零售", "贸易", "经销", "代理",
    "wholesale", "retail", "trading of", "distribution of", "import and export of",
)

# Registered capital below this (in RMB) is a red flag for a company
# claiming to be a serious manufacturing operation — real production
# lines, tooling, and factory floor space cost real money to register
# against, even allowing for China's post-2014 authorised-capital reform
# under which companies self-declare capital rather than depositing it.
LOW_CAPITAL_THRESHOLD_RMB = 500_000

# A textbook shell/trading-company pattern: claims 5+ years as an
# Alibaba Gold Supplier, but the registry shows the company was formed
# far more recently — the storefront may have been transferred or
# rebranded, or the "years" figure may not reflect this legal entity.
TENURE_MISMATCH_YEARS = 2


def assess_business_scope(business_scope_text: Optional[str]) -> Tuple[Optional[bool], str]:
    """Returns (verdict, explanation). verdict is True/False/None —
    None means the scope text didn't contain a recognisable marker
    either way (including when there's no text at all)."""
    if not business_scope_text:
        return None, "No registered business scope text available"

    text = business_scope_text.lower()
    has_mfg = any(marker in text for marker in MANUFACTURING_SCOPE_MARKERS)
    has_trade = any(marker in text for marker in TRADING_SCOPE_MARKERS)

    if has_mfg and not has_trade:
        return True, "Registered business scope explicitly includes production/manufacturing"
    if has_mfg and has_trade:
        return True, "Registered business scope includes both manufacturing and trading activity"
    if has_trade and not has_mfg:
        return False, "Registered business scope is limited to sales/wholesale/trading — no manufacturing activity declared"
    return None, "Registered business scope present but didn't match known manufacturing/trading markers"


def assess_registered_capital(registered_capital_rmb: Optional[float]) -> Tuple[Optional[bool], str]:
    if registered_capital_rmb is None:
        return None, "No registered capital on file"
    if registered_capital_rmb < LOW_CAPITAL_THRESHOLD_RMB:
        return False, f"Registered capital (¥{registered_capital_rmb:,.0f}) is unusually low for a manufacturing operation"
    return True, f"Registered capital (¥{registered_capital_rmb:,.0f}) is consistent with a real manufacturing operation"


def assess_tenure_consistency(
    alibaba_years: Optional[int], year_established: Optional[int], today: Optional[date] = None
) -> Tuple[Optional[bool], str]:
    """Cross-checks Alibaba's self-reported platform tenure against the
    registry's actual incorporation year, when both are known."""
    if not alibaba_years or not year_established:
        return None, "Not enough data to cross-check platform tenure against registered incorporation year"

    today = today or date.today()
    implied_founding = today.year - alibaba_years
    gap = abs(implied_founding - year_established)

    if gap > TENURE_MISMATCH_YEARS:
        return False, (
            f"Alibaba tenure implies founding around {implied_founding}, but the registry shows "
            f"incorporation in {year_established} — a {gap}-year mismatch worth investigating "
            f"(possible storefront transfer or rebrand)"
        )
    return True, "Platform tenure is consistent with the registered incorporation year"


def assess_certifications(supplier: Dict[str, Any]) -> Tuple[Optional[bool], str]:
    """Certifications that require an actual production facility (ISO
    9001, IATF 16949, E-mark) are themselves evidence of manufacturing
    capability — a pure trading company has no reason to hold them."""
    held: List[str] = []
    if supplier.get("iso_9001"):
        held.append("ISO 9001")
    if supplier.get("iatf_16949"):
        held.append("IATF 16949 (automotive)")
    if supplier.get("e_mark_certified"):
        held.append("E-mark")

    if held:
        return True, f"Holds production-relevant certification(s): {', '.join(held)}"
    return None, "No production-relevant certifications on file"


class ManufacturerVerifier:
    """
    Combines every available signal for a supplier into:
      - manufacturer_confidence: int 0-100
      - is_manufacturer: True / False / None ("genuinely inconclusive" —
        distinct from False, and should NOT be treated as "probably a trader")
      - manufacturer_signals: list[str], supporting evidence first, then
        any red flags prefixed "RED FLAG:" — meant to be read by a
        person, not just consumed as a number.

    This does NOT call any external API — it works entirely off fields
    already present on the supplier record (populated by Qichacha
    verification, scrapers/normalizers, etc.). Run Qichacha verification
    first if business_scope / registered_capital_rmb aren't populated
    yet, or this will have little more than the certification signal to
    work with.

    Scoring model: starts at a neutral 50. Each signal that fires moves
    the score by its configured weight (the four weights below sum to
    100, so unanimous agreement in one direction reaches the 0/100
    clamp). This is a deliberately simple, auditable model — not a
    trained classifier — chosen so every point of the final score can
    be traced back to a specific, explainable reason in `signals`.
    """

    ASSESSOR_WEIGHTS: Tuple[Tuple[str, int], ...] = (
        ("business_scope", 40),
        ("registered_capital", 20),
        ("tenure_consistency", 15),
        ("certifications", 25),
    )

    def assess(self, supplier: Dict[str, Any]) -> Dict[str, Any]:
        checks = {
            "business_scope": lambda: assess_business_scope(supplier.get("business_scope")),
            "registered_capital": lambda: assess_registered_capital(supplier.get("registered_capital_rmb")),
            "tenure_consistency": lambda: assess_tenure_consistency(
                supplier.get("alibaba_years"), supplier.get("year_established"),
            ),
            "certifications": lambda: assess_certifications(supplier),
        }

        score = 50
        supporting: List[str] = []
        red_flags: List[str] = []
        any_signal_found = False

        for name, weight in self.ASSESSOR_WEIGHTS:
            verdict, explanation = checks[name]()
            if verdict is None:
                continue
            any_signal_found = True
            if verdict:
                score += weight
                supporting.append(explanation)
            else:
                score -= weight
                red_flags.append(explanation)

        score = max(0, min(100, score))
        signals = supporting + [f"RED FLAG: {flag}" for flag in red_flags]

        if not any_signal_found:
            is_manufacturer: Optional[bool] = None
            summary = "Insufficient data to assess manufacturing status — treat as unverified, not confirmed."
        elif score >= 70:
            is_manufacturer = True
            summary = "Multiple independent signals support a genuine manufacturing operation."
        elif score <= 30:
            is_manufacturer = False
            summary = "Multiple independent signals suggest this is a trading company, not a manufacturer."
        else:
            is_manufacturer = None
            summary = "Mixed or weak signals — worth a direct factory audit before relying on this supplier as a manufacturer."

        return {
            "manufacturer_confidence": score,
            "is_manufacturer": is_manufacturer,
            "manufacturer_signals": signals,
            "summary": summary,
        }
