"""
verification/scorer.py

Composite scoring engine. Scores each supplier 0-100 across four
dimensions (verification, export, platform, contact), weighted per
config.settings.SCORING_WEIGHTS, into a single composite_score plus a
recommendation bucket ('recommended' | 'review' | 'unverified' | 'avoid').

Note on SQLite booleans: columns declared BOOLEAN in suppliers come back
from the repository as Python ints (0/1), not True/False, since SQLite
has no native boolean type. Every check below uses truthiness (`if
s.get("x"):`) rather than `is True`/`is False` comparisons for exactly
this reason — the one explicit `in (0, False)` check in _recommend is
the deliberate exception, needed because 0 there is a meaningful
"confirmed trader" signal rather than just "missing/unknown".
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict

from config.settings import SCORING_WEIGHTS


class SupplierScorer:
    """
    Verification Score (35% weight by default):
    - USCC verified:             +30
    - Confirmed manufacturer:    +25
    - ISO 9001:                  +15
    - E-mark certified:          +20
    - ISO/TS 16949 (automotive): +10

    Export Score (30%):
    - UK shipments confirmed:    +40
    - >10 UK shipments:          +20 (established UK exporter)
    - EU shipments confirmed:    +20
    - US shipments confirmed:    +10
    - Shipment within last 180 days: +10

    Platform Score (20%):
    - Alibaba Gold 5+ years:     +40 (3-4 yrs: +25, 1-2 yrs: +10)
    - Trade Assurance enabled:   +20
    - Alibaba rating >= 4.5:     +20 (>= 4.0: +10)
    - Presence on 2+ platforms:  +20

    Contact Score (15%):
    - Named contact:             +30
    - Direct email:              +25
    - WhatsApp or WeChat:        +25
    - Verified phone:            +20
    """

    def score(self, supplier: Dict[str, Any]) -> Dict[str, Any]:
        v = self._verification_score(supplier)
        e = self._export_score(supplier)
        p = self._platform_score(supplier)
        c = self._contact_score(supplier)

        composite = int(round(
            v * SCORING_WEIGHTS["verification"]
            + e * SCORING_WEIGHTS["export"]
            + p * SCORING_WEIGHTS["platform"]
            + c * SCORING_WEIGHTS["contact"]
        ))
        composite = max(0, min(100, composite))

        return {
            "verification_score": v,
            "export_score": e,
            "platform_score": p,
            "contact_score": c,
            "composite_score": composite,
            "recommendation": self._recommend(composite, supplier),
        }

    # ═════════════════════════════════════════════════════
    # Dimension scores
    # ═════════════════════════════════════════════════════

    def _verification_score(self, s: Dict[str, Any]) -> int:
        score = 0
        if s.get("uscc_verified"):
            score += 30
        if s.get("is_manufacturer"):
            score += 25
        if s.get("iso_9001"):
            score += 15
        if s.get("e_mark_certified"):
            score += 20
        if s.get("iso_ts_16949"):
            score += 10
        return min(score, 100)

    def _export_score(self, s: Dict[str, Any]) -> int:
        """
        UK-bound confirmed shipment data is genuinely hard to come by —
        of the sources in this pipeline, only scrapers.global_trade_scraper
        (Volza-backed) can populate confirmed_shipments_uk directly;
        scrapers.importyeti_scraper only covers US customs data. Without
        this fallback, a supplier that genuinely does export to the UK
        but hasn't been matched to a confirmed shipment record yet would
        score a flat 0 here regardless of how strong every other signal
        is — which isn't "unverified", it's "structurally unreachable".

        So: confirmed shipment counts still drive the bulk of the score
        and are always worth more than a self-reported claim. But when
        there's no confirmed count for a destination, a self-reported
        exports_to_uk/eu/us flag (see
        normalizers.base_normalizer.BaseNormalizer.infer_export_flags_from_markets)
        gets partial credit instead of nothing — clearly worth less
        (15 vs 40 for UK) since a platform marketing claim isn't
        verified evidence, but nonzero rather than crushing the score
        for a source-coverage gap that isn't the supplier's fault.
        """
        score = 0
        uk = s.get("confirmed_shipments_uk", 0) or 0
        eu = s.get("confirmed_shipments_eu", 0) or 0
        us = s.get("confirmed_shipments_us", 0) or 0

        if uk > 0:
            score += 40
            if uk > 10:
                score += 20  # established UK exporter
        elif s.get("exports_to_uk"):
            score += 15  # self-reported only — no confirmed shipment yet

        if eu > 0:
            score += 20
        elif s.get("exports_to_eu"):
            score += 8

        if us > 0:
            score += 10
        elif s.get("exports_to_us"):
            score += 5

        last_shipment = s.get("last_shipment_date")
        if last_shipment:
            try:
                last_date = (
                    last_shipment if isinstance(last_shipment, date)
                    else date.fromisoformat(str(last_shipment)[:10])
                )
                if last_date > date.today() - timedelta(days=180):
                    score += 10  # active in the last 6 months
            except (ValueError, TypeError):
                pass

        return min(score, 100)

    def _platform_score(self, s: Dict[str, Any]) -> int:
        score = 0
        years = s.get("alibaba_years", 0) or 0
        if years >= 5:
            score += 40
        elif years >= 3:
            score += 25
        elif years >= 1:
            score += 10

        if s.get("alibaba_trade_assurance"):
            score += 20

        rating = s.get("alibaba_rating", 0) or 0
        if rating >= 4.5:
            score += 20
        elif rating >= 4.0:
            score += 10

        platforms = sum([
            bool(s.get("alibaba_url")),
            bool(s.get("indiamart_url")),
            bool(s.get("hktdc_url")),
            bool(s.get("made_in_china_url")),
        ])
        if platforms >= 2:
            score += 20  # bonus for multi-platform presence

        return min(score, 100)

    def _contact_score(self, s: Dict[str, Any]) -> int:
        score = 0
        if s.get("contact_name"):
            score += 30
        if s.get("primary_email"):
            score += 25
        if s.get("whatsapp") or s.get("wechat_id"):
            score += 25
        if s.get("primary_phone"):
            score += 20
        return min(score, 100)

    # ═════════════════════════════════════════════════════
    # Recommendation
    # ═════════════════════════════════════════════════════

    def _recommend(self, composite: int, s: Dict[str, Any]) -> str:
        # Automatic disqualifiers override the composite score entirely.
        if s.get("flagged"):
            return "avoid"
        if s.get("is_manufacturer") in (0, False) and (s.get("manufacturer_confidence") or 0) > 80:
            return "avoid"  # confirmed trader, not a factory — disqualifying for this use case

        if composite >= 75:
            return "recommended"
        elif composite >= 50:
            return "review"
        elif composite >= 25:
            return "unverified"
        else:
            return "avoid"
