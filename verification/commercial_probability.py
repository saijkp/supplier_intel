"""
verification/commercial_probability.py

Estimates the *probability* a supplier could support a given
commercial arrangement (60-day payment terms, a framework agreement,
forecast-based production) — never a fact, never stored or reported
as one. Every estimate exposes exactly which signals fed it and how
many of them were actually available, per the commercial-intelligence
spec's own explicit requirement.

Why this is a transparent rule-based score, not an LLM call
------------------------------------------------------------
Nothing on a supplier's website states "we accept 60-day payment
terms" in the way it states "we operate injection moulding" — this is
inherently an inference from *other* signals (company age, export
volume, confirmed markets, manufacturing confirmation), not something
`capability_extractor` could ever read off a page. An LLM asked to
estimate this number directly would be free to invent a plausible-
sounding percentage with no way for a caller to audit *why* — exactly
the fabrication risk the whole codebase has been built to avoid
everywhere else. A small, fixed set of weighted signals, each
individually checkable and each contributing an explicit fraction to
the total, is auditable in a way a model's raw output number never is.

Probability vs. confidence — two different questions
-----------------------------------------------------
`probability` answers "of the signals we could check, how many point
toward yes" — computed only over *available* signals, never treating
missing data as a negative signal (a supplier with only one data point
available, and that point positive, is not "definitely flexible," it's
"we only have one weak reason to think so").

`confidence_level` answers a completely different question: how much
of the total signal set was actually available to check at all. A
supplier with 5 of 6 signals available computes a more trustworthy
probability than one with 1 of 6 available, even if both happen to
land on the same percentage — this is exactly why a probability
number alone, without a paired confidence level, would be misleading,
and why this module refuses to return one without the other.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List

from verification.commercial_scoring import assess_company_scale, assess_export_maturity

# Disclosed, deliberately simple thresholds -- not derived from any
# calibrated model, since none exists yet (see this codebase's
# standing recommendation, repeated throughout, to calibrate against
# known-good suppliers before trusting a scoring system at volume).
# Treat these as a reasonable starting point to correct once real
# outcomes are recorded (see storage.repository's procurement_outcomes
# methods) -- not as tuned, authoritative cutoffs.
_LARGE_REGISTERED_CAPITAL_RMB = 1_000_000.0
_ESTABLISHED_COMPANY_AGE_YEARS = 10
_LARGE_FACTORY_SIZE_SQM = 2_000
_ESTABLISHED_EXPORTER_MARKET_COUNT = 2


@dataclasses.dataclass(frozen=True)
class ProbabilitySignal:
    name: str
    available: bool
    present: bool  # meaningless when available=False
    weight: float
    detail: str


@dataclasses.dataclass(frozen=True)
class CommercialFlexibilityEstimate:
    outcome: str
    probability: float  # 0-100. Reflects only signals that were available -- see module docstring.
    confidence_level: str  # 'low' | 'medium' | 'high'
    contributing_signals: List[ProbabilitySignal]
    reasoning: str


def _confidence_level(available_count: int, total_count: int) -> str:
    if total_count == 0:
        return "low"
    ratio = available_count / total_count
    if ratio >= 0.66:
        return "high"
    if ratio >= 0.33:
        return "medium"
    return "low"


def _build_signals(supplier: Dict[str, Any]) -> List[ProbabilitySignal]:
    """The fixed signal set every commercial-flexibility estimate in
    this module is built from. Adding a signal here changes every
    estimate's weighting -- deliberately centralised in one place
    rather than duplicated per-outcome.
    """
    signals: List[ProbabilitySignal] = []

    scale_factors = {f.factor_name: f for f in assess_company_scale(supplier)}

    capital = scale_factors["registered_capital_rmb"]
    signals.append(ProbabilitySignal(
        name="large_registered_capital", available=capital.value is not None,
        present=bool(capital.value and capital.value >= _LARGE_REGISTERED_CAPITAL_RMB),
        weight=20.0,
        detail=f"registered capital {'>=' if capital.value else 'unknown, threshold'} "
               f"RMB {_LARGE_REGISTERED_CAPITAL_RMB:,.0f}",
    ))

    age = scale_factors["company_age_years"]
    signals.append(ProbabilitySignal(
        name="established_company_age", available=age.value is not None,
        present=bool(age.value is not None and age.value >= _ESTABLISHED_COMPANY_AGE_YEARS),
        weight=15.0,
        detail=f"company age {'unknown' if age.value is None else f'{age.value} years'} "
               f"(threshold: {_ESTABLISHED_COMPANY_AGE_YEARS}+ years)",
    ))

    factory_size = supplier.get("factory_size_sqm")
    signals.append(ProbabilitySignal(
        name="large_factory_footprint", available=factory_size is not None,
        present=bool(factory_size and factory_size >= _LARGE_FACTORY_SIZE_SQM),
        weight=15.0,
        detail=f"factory size {'unknown' if factory_size is None else f'{factory_size} sqm'} "
               f"(threshold: {_LARGE_FACTORY_SIZE_SQM:,}+ sqm)",
    ))

    export_maturity = assess_export_maturity(supplier)
    signals.append(ProbabilitySignal(
        name="established_exporter", available=export_maturity.value is not None,
        present=bool(export_maturity.value is not None and export_maturity.value >= _ESTABLISHED_EXPORTER_MARKET_COUNT),
        weight=25.0,
        detail=f"evidenced export markets: {export_maturity.value if export_maturity.value is not None else 'unknown'} "
               f"(threshold: {_ESTABLISHED_EXPORTER_MARKET_COUNT}+)",
    ))

    is_manufacturer = supplier.get("is_manufacturer")
    signals.append(ProbabilitySignal(
        name="confirmed_manufacturer", available=is_manufacturer is not None,
        present=bool(is_manufacturer),
        weight=15.0,
        detail=f"manufacturer status: {'confirmed' if is_manufacturer else ('confirmed trader' if is_manufacturer == 0 else 'unknown')}",
    ))

    high_shipment_volume = (supplier.get("confirmed_shipments_uk") or 0) + \
        (supplier.get("confirmed_shipments_eu") or 0) + (supplier.get("confirmed_shipments_us") or 0)
    signals.append(ProbabilitySignal(
        name="high_shipment_volume", available=True,  # always computable -- absence of data reads as 0, a real fact
        present=high_shipment_volume >= 5,
        weight=10.0,
        detail=f"{high_shipment_volume} total confirmed shipment(s) across tracked markets (threshold: 5+)",
    ))

    return signals


def estimate_commercial_flexibility(supplier: Dict[str, Any], outcome: str) -> CommercialFlexibilityEstimate:
    """Estimates the probability of `outcome` (a short description,
    e.g. "60-day OEM payment terms", "framework agreement",
    "forecast-based production") using the same fixed signal set
    regardless of which outcome is asked about -- this module does not
    (yet) have outcome-specific signal weighting, since no real
    procurement-outcome data exists yet to calibrate one against (see
    `storage.repository`'s `procurement_outcomes` methods, built for
    exactly that future calibration).
    """
    signals = _build_signals(supplier)
    available = [s for s in signals if s.available]

    if not available:
        # Defensive only: with the current fixed signal set, at least
        # one signal (high_shipment_volume) is always available -- 0
        # confirmed shipments is itself a real, known fact, not
        # missing data (see that signal's own comment above), so this
        # branch cannot currently trigger. Kept as a guard in case a
        # future signal set ever makes every signal conditionally
        # unavailable.
        return CommercialFlexibilityEstimate(
            outcome=outcome, probability=0.0, confidence_level="low",
            contributing_signals=signals,
            reasoning="no contributing signals available for this supplier — probability is not "
                      "meaningful, treat as entirely unknown, not as evidence against",
        )

    available_weight = sum(s.weight for s in available)
    present_weight = sum(s.weight for s in available if s.present)
    probability = round((present_weight / available_weight) * 100, 1) if available_weight else 0.0

    confidence = _confidence_level(len(available), len(signals))
    present_names = [s.name.replace("_", " ") for s in available if s.present]
    reasoning = (
        f"Based on {len(available)} of {len(signals)} possible signals: "
        + (", ".join(present_names) if present_names else "none of the available signals were positive")
    )

    return CommercialFlexibilityEstimate(
        outcome=outcome, probability=probability, confidence_level=confidence,
        contributing_signals=signals, reasoning=reasoning,
    )
