"""
normalizers/base_normalizer.py

Abstract base class every source-specific normalizer implements. A
normalizer's only job is: raw scraped dict in -> supplier_data dict out,
using only field names storage.repository.SUPPLIER_WRITABLE_FIELDS
recognises. Normalizers never touch the database directly — that's the
pipeline orchestrator's job (Phase 3), via SupplierRepository.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class BaseNormalizer(ABC):
    source_name: str = "unknown"

    @abstractmethod
    def normalise(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """Transform a single raw scraped record into a supplier_data
        dict. Should never raise on missing/malformed fields — return
        partial data instead, since a partial golden record beats
        losing the supplier entirely. `canonical_name` should always be
        present in the result (even as an empty string) so callers can
        explicitly check for it rather than hitting a KeyError."""
        ...

    # ── Shared helpers available to all normalizers ──────────

    @staticmethod
    def clean_str(value: Any) -> str:
        """Coerce to a trimmed string; None/missing becomes ''."""
        if value is None:
            return ""
        return str(value).strip()

    @staticmethod
    def to_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        if isinstance(value, str):
            return value.strip().lower() in ("true", "yes", "1", "y")
        return bool(value)

    @staticmethod
    def to_int(value: Any, default: int = 0) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    # ── Shared export-market inference ───────────────────────
    # UK-shipment data is hard to come by (see
    # scrapers.global_trade_scraper / normalizers.trade_normalizer for
    # the confirmed-shipment path) — this gives partial, lower-confidence
    # credit for a platform's *self-reported* main-markets claim, which
    # every B2B directory normalizer can reuse identically.

    _UK_MARKET_MARKERS = ("united kingdom", "uk", "britain", "england")
    _EU_MARKET_MARKERS = ("europe", "eu ", "european union")
    _US_MARKET_MARKERS = ("north america", "united states", "usa", "u.s.")

    @classmethod
    def infer_export_flags_from_markets(cls, markets: List[Any]) -> Dict[str, Any]:
        """Given a list of self-reported 'main markets' / 'export
        markets' strings from a B2B platform (e.g. Alibaba's
        'mainMarkets' field), infer coarse export-destination flags.

        This is deliberately a WEAKER signal than a confirmed shipment
        record (normalizers.trade_normalizer) — it's a self-reported
        marketing claim, not evidence of a completed export.
        verification.scorer.SupplierScorer._export_score gives it
        correspondingly less credit than confirmed shipment counts."""
        if not markets:
            return {}
        text = " ".join(str(m) for m in markets).lower()
        flags: Dict[str, Any] = {"active_export_countries": list(markets)}
        if any(marker in text for marker in cls._UK_MARKET_MARKERS):
            flags["exports_to_uk"] = True
        if any(marker in text for marker in cls._EU_MARKET_MARKERS):
            flags["exports_to_eu"] = True
        if any(marker in text for marker in cls._US_MARKET_MARKERS):
            flags["exports_to_us"] = True
        return flags
