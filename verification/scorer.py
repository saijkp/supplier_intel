"""
verification/scorer.py

Composite scoring engine. Scores each supplier 0-100 across five
weighted dimensions (product_fit, provenance, verification, export,
contact), weighted per config.settings.SCORING_WEIGHTS, into a single
composite_score plus a recommendation bucket ('recommended' | 'review'
| 'unverified' | 'unscored' | 'avoid').

Rewritten (v16) because the original verification/export/platform/
contact split read almost exclusively China/Alibaba-shaped columns
(uscc_verified, confirmed_shipments_*, alibaba_*) that are populated
for well under 3% of this database, while ignoring product_keywords/
domain/address/country, which are populated for 82-99% of it. The
practical effect was that non-Chinese, non-marketplace-listed
suppliers (the majority of the DB) were structurally floored near
zero regardless of how real or well-matched they were -- absence of
Chinese-platform evidence, not actual negative evidence, was driving
the score. USCC and Alibaba platform strength are now small capped
bonuses instead of weighted dimensions, so lacking them no longer
costs a supplier reachable points.

evidence_coverage (how much we know) is tracked and returned
separately from composite_score (how good what we know looks) -- see
_evidence_coverage's own docstring for why it's read from the *_at
attempt-timestamps rather than the boolean/count columns directly.
Low coverage routes the recommendation to 'unscored' rather than
'avoid': 'avoid' is reserved for supplier records with actual
negative evidence (flagged, or a confirmed trader), never for gaps in
what this pipeline happens to have collected yet.

Note on SQLite booleans: columns declared BOOLEAN in suppliers come back
from the repository as Python ints (0/1), not True/False, since SQLite
has no native boolean type. Every check below uses truthiness (`if
s.get("x"):`) rather than `is True`/`is False` comparisons for exactly
this reason — the one explicit `in (0, False)` check in _recommend is
the deliberate exception, needed because 0 there is a meaningful
"confirmed trader" signal rather than just "missing/unknown".
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Dict, Optional, Set

from config.settings import SCORING_WEIGHTS
from reports.coverage import BOM_CATEGORIES, _haystack, _matches

# How much independent-verification weight a given source type deserves
# for the provenance dimension below -- a self-service marketplace
# listing (anyone can post one) isn't the same evidence as a curated
# trade-show exhibitor registration or an actual customs shipment
# record. Fixed and curated, same "unmapped rather than discarded"
# philosophy as verification.capability_vocabulary's vocabulary -- an
# unlisted/future source gets _DEFAULT_SOURCE_QUALITY rather than being
# silently over- or under-trusted. Extend this as new sources are
# onboarded (scrapers/global_directory_scraper.py's `directory` config
# in particular is open-ended, so not every specific site name it might
# produce is pre-listed here).
SOURCE_QUALITY_WEIGHTS: Dict[str, int] = {
    "trade": 90,                      # importyeti/panjiva/volza -- real customs shipment records
    "automechanika_2026": 65,         # curated trade-show exhibitor registration
    "shanghai_expo": 60,              # curated exhibition directory
    "hktdc": 55,                      # HKTDC's own vetted trade directory
    "europages_eastern_europe": 50,   # curated regional B2B directory
    "global_directory": 45,           # generic curated regional directory (default source name)
    "alibaba": 35,                    # self-service marketplace listing
    "china_1688": 35,
    "indiamart": 35,
    "google": 25,                     # generic web search hit -- weakest identity signal
}
_DEFAULT_SOURCE_QUALITY = 30  # unclassified/not-yet-curated source

# Below this evidence_coverage, there simply isn't enough known about a
# supplier to render *any* verdict -- see _recommend.
_UNSCORED_COVERAGE_FLOOR = 30

_JSON_HAYSTACK_FIELDS = ("product_keywords", "primary_categories", "trailer_components")


class SupplierScorer:
    """
    Product Fit (25% weight by default):
    - product_keywords/primary_categories/trailer_components/notes
      matched against reports.coverage.BOM_CATEGORIES (reused directly,
      not duplicated, so the taxonomy only lives in one place).
      0 categories matched: 0, 1: 55, 2: 75, 3: 90, 4+: 100.

    Provenance (25%):
    - Source-quality-weighted, not a flat "came from a curated list"
      bonus -- see _provenance_score's own docstring for why. Best
      matching source's quality (up to 40) + corroboration by 2+ or 3+
      independent sources (+18/+30) + live domain (+20) + real address
      (+10).

    Verification (25%):
    - Confirmed manufacturer:    +40
    - ISO 9001:                  +25
    - E-mark certified:          +20
    - ISO/TS 16949 (automotive): +15
    (USCC verification moved to a bonus -- see score().)

    Export (15%):
    - UK shipments confirmed:    +40
    - >10 UK shipments:          +20 (established UK exporter)
    - EU shipments confirmed:    +20
    - US shipments confirmed:    +10
    - Shipment within last 180 days: +10

    Contact (10%):
    - Named contact:             +30
    - Direct email:              +25
    - WhatsApp or WeChat:        +25
    - Verified phone:            +20

    Bonuses (added to the weighted composite, not part of the 100%):
    - uscc_verified:              +5
    - Alibaba platform strength:  +0-5, proportional to platform_score
      (still computed by _platform_score, unchanged formula -- just no
      longer a weighted dimension).
    """

    def score(self, supplier: Dict[str, Any], sources: Optional[Set[str]] = None) -> Dict[str, Any]:
        fit = self._product_fit_score(supplier)
        prov = self._provenance_score(supplier, sources)
        v = self._verification_score(supplier)
        e = self._export_score(supplier)
        c = self._contact_score(supplier)
        platform = self._platform_score(supplier)

        weighted = (
            fit * SCORING_WEIGHTS["product_fit"]
            + prov * SCORING_WEIGHTS["provenance"]
            + v * SCORING_WEIGHTS["verification"]
            + e * SCORING_WEIGHTS["export"]
            + c * SCORING_WEIGHTS["contact"]
        )

        bonus = 0
        if supplier.get("uscc_verified"):
            bonus += 5
        bonus += round(platform * 0.05)  # 0-5, proportional to platform_score

        composite = int(round(weighted)) + bonus
        composite = max(0, min(100, composite))

        coverage = self._evidence_coverage(supplier)

        return {
            "product_fit_score": fit,
            "provenance_score": prov,
            "verification_score": v,
            "export_score": e,
            "platform_score": platform,
            "contact_score": c,
            "composite_score": composite,
            "evidence_coverage": coverage,
            "recommendation": self._recommend(composite, coverage, supplier),
        }

    # ═════════════════════════════════════════════════════
    # Dimension scores
    # ═════════════════════════════════════════════════════

    def _product_fit_score(self, s: Dict[str, Any]) -> int:
        haystack = _haystack(self._as_row_like(s))
        matched = sum(1 for category in BOM_CATEGORIES if _matches(haystack, category))
        return {0: 0, 1: 55, 2: 75, 3: 90}.get(matched, 100)

    @staticmethod
    def _as_row_like(s: Dict[str, Any]) -> Dict[str, Any]:
        """reports.coverage._haystack expects JSON-array columns as raw
        JSON strings, matching a fresh sqlite3.Row straight off the DB
        -- but storage.repository already decodes those columns into
        Python lists before a supplier dict reaches here. Re-serialise
        just the fields _haystack reads so product-fit matching sees
        exactly the same input reports.coverage's own `coverage` CLI
        command does, rather than relying on _haystack's str()-fallback
        path for what would otherwise look like malformed JSON."""
        shim = dict(s)
        for field in _JSON_HAYSTACK_FIELDS:
            value = shim.get(field)
            if isinstance(value, list):
                shim[field] = json.dumps(value)
        return shim

    def _provenance_score(self, s: Dict[str, Any], sources: Optional[Set[str]]) -> int:
        """
        Provenance answers "is this a real company, corroborated by real
        records" -- independent of what it claims about its own
        capability (that's product fit) or hard certification evidence
        (that's verification).

        Deliberately source-aware rather than a flat "came from a
        curated exhibitor list" bonus: 676/688 suppliers in this
        database currently share the exact same single source (the
        Automechanika exhibitor import), so a flat per-source bonus
        would shift every one of them by the same constant and
        discriminate between nothing. Scoring the *quality* of the best
        single source plus how many *independent* sources corroborate
        the record means a supplier confirmed by two independent
        sources outscores one appearing in a single bulk import, and as
        new sources land (1688, customs data, type-approval registers)
        each scores according to its own SOURCE_QUALITY_WEIGHTS entry
        rather than all being treated alike.
        """
        sources = sources or set()
        best_quality = max(
            (SOURCE_QUALITY_WEIGHTS.get(src, _DEFAULT_SOURCE_QUALITY) for src in sources),
            default=0,
        )
        score = round(best_quality * 0.4)  # up to 40

        distinct = len(sources)
        if distinct >= 3:
            score += 30
        elif distinct == 2:
            score += 18

        if s.get("domain"):
            score += 20
        if s.get("address"):
            score += 10

        return min(score, 100)

    def _verification_score(self, s: Dict[str, Any]) -> int:
        score = 0
        if s.get("is_manufacturer"):
            score += 40
        if s.get("iso_9001"):
            score += 25
        if s.get("e_mark_certified"):
            score += 20
        if s.get("iso_ts_16949"):
            score += 15
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
        """No longer a weighted composite dimension (see score()) --
        Alibaba/marketplace presence is China/Alibaba-specific and most
        suppliers in this database have none, so it now only feeds a
        small capped bonus. Formula is unchanged from before the v16
        rewrite, and the column is still persisted, so "how strong is
        this supplier's Alibaba presence" stays independently queryable."""
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
    # Evidence coverage
    # ═════════════════════════════════════════════════════

    def _evidence_coverage(self, s: Dict[str, Any]) -> int:
        """
        How much we actually know about this supplier, independent of
        whether what we know looks good (that's composite_score --
        "evidence quality"). Drives the 'unscored' recommendation below.

        Checked via the *_at attempt-timestamps rather than the
        boolean/count columns themselves where one exists: uscc_verified,
        iso_9001, e_mark_certified, iso_ts_16949, exports_to_*, and
        confirmed_shipments_* are all NOT NULL DEFAULT 0 in the schema
        (no NULL/"unknown" state), so a 0 there is indistinguishable
        between "checked, and it's false" and "never checked" --
        trusting those columns directly for coverage would silently
        double-count "unknown" as "known and negative."
        """
        checks = [
            bool(s.get("product_keywords") or s.get("primary_categories") or s.get("trailer_components")),
            bool(s.get("domain")),
            bool(s.get("address")),
            s.get("capability_extracted_at") is not None,
            s.get("manufacturer_verified_at") is not None,
            bool(
                (s.get("confirmed_shipments_uk") or 0) or (s.get("confirmed_shipments_eu") or 0)
                or (s.get("confirmed_shipments_us") or 0)
                or s.get("exports_to_uk") or s.get("exports_to_eu") or s.get("exports_to_us")
            ),
            bool(s.get("contacts_found_at") is not None or s.get("primary_email") or s.get("primary_phone")),
        ]
        return round(100 * sum(checks) / len(checks))

    # ═════════════════════════════════════════════════════
    # Recommendation
    # ═════════════════════════════════════════════════════

    def _recommend(self, composite: int, coverage: int, s: Dict[str, Any]) -> str:
        # Automatic disqualifiers require actual negative evidence --
        # absence of evidence routes to 'unscored' below, never 'avoid'.
        if s.get("flagged"):
            return "avoid"
        if s.get("is_manufacturer") in (0, False) and (s.get("manufacturer_confidence") or 0) > 80:
            return "avoid"  # confirmed trader, not a factory — disqualifying for this use case

        if coverage < _UNSCORED_COVERAGE_FLOOR:
            return "unscored"  # not enough known either way to render a verdict

        if composite >= 70:
            return "recommended"
        elif composite >= 40:
            return "review"
        else:
            return "unverified"
