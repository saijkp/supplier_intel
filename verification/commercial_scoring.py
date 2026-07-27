"""
verification/commercial_scoring.py

Assesses commercial/logistics compatibility factors for a supplier --
does it ship DDP, does it serve a given market, does it hold a
required certification, what's known about its scale -- each as a
`FactorResult` carrying value, confidence, evidence, source, and
reasoning, per the commercial-intelligence spec's own requirement.

Why this reuses existing data rather than collecting new signals
--------------------------------------------------------------------
Most of what "Financial & Commercial Strength" and "Market Presence"
ask for already exists on the supplier record: `registered_capital_rmb`
and `year_established` (from Qichacha), `exports_to_uk/eu/us`
(self-reported "main markets" text, via
`BaseNormalizer.infer_export_flags_from_markets`), and
`confirmed_shipments_uk/eu/us` (actual customs/trade data, via the
Volza normalizer). This module's job for those factors is scoring and
attaching evidence to what's already collected, not collecting
anything new.

Logistics, market-presence-from-website-text, and OEM-readiness
factors are genuinely new signals, but they reuse the *mechanism*
already built for manufacturing capabilities: `capability_extractor`
reads the supplier's own website, `capability_vocabulary` maps free
text to a canonical term, and results land in `supplier_capabilities`
with the same evidence/confidence/relationship shape. This module
queries that table; it does not re-implement extraction.

Tiered confidence by source, not a single number
--------------------------------------------------
The same real-world fact (e.g. "serves the EU market") can be known
with very different reliability depending on where it came from:
confirmed customs shipment data is strong, third-party-verified
evidence; a supplier's own website assertion is real but self-
reported; a directory listing's "main markets" checkbox is the
weakest of the three. Each `assess_*` function below picks the
strongest available source for a factor and reports which one it used
-- never silently blends sources into one number that hides which
evidence it actually rests on.

Unknown is a real value, not an absence
-------------------------------------------
Every factor with no evidence returns `value=None, confidence=0.0`
rather than `False` -- "no evidence supplier ships DDP" and "evidence
supplier does not ship DDP" are different facts, and this module has
no mechanism to observe the second one (a positive-observation-only
extractor, by design, never produces a "does not do X" finding). Never
conflate the two when consuming this module's output.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from verification.capability_vocabulary import (
    CATEGORY_LOGISTICS,
    CATEGORY_OEM_READINESS,
    CATEGORY_STANDARD,
    map_to_canonical,
)

SOURCE_TRADE_DATA = "trade_data"
SOURCE_SUPPLIER_LOCATION = "supplier_location"
SOURCE_OWN_WEBSITE = "own_website"
SOURCE_DIRECTORY_LISTING = "directory_listing"
SOURCE_REGISTRY = "registry"
SOURCE_UNAVAILABLE = "unavailable"

_MARKET_CANONICAL_TERMS = {
    "uk": "serves uk market",
    "eu": "serves eu market",
    "us": "serves north america market",
    "australia": "serves australia market",
}

# Deliberately small and explicit, not an attempt at a comprehensive
# country-to-region mapping — a destination_country outside this list
# simply skips the location-based tier below rather than guessing.
# Shared with pipeline.buyer_profile_search, which imports this
# directly rather than keeping its own copy.
COUNTRY_TO_MARKET_KEY = {
    "united kingdom": "uk", "uk": "uk", "great britain": "uk", "england": "uk",
    "scotland": "uk", "wales": "uk", "northern ireland": "uk",
    "united states": "us", "usa": "us", "us": "us", "united states of america": "us",
    "australia": "australia",
    **{country: "eu" for country in (
        "germany", "france", "italy", "spain", "netherlands", "belgium", "poland",
        "ireland", "austria", "sweden", "denmark", "finland", "portugal", "greece",
        "czech republic", "romania", "hungary", "bulgaria", "slovakia", "croatia",
        "slovenia", "lithuania", "latvia", "estonia", "luxembourg", "malta", "cyprus",
    )},
}


def country_to_market_key(country: Optional[str]) -> Optional[str]:
    if not country:
        return None
    return COUNTRY_TO_MARKET_KEY.get(country.strip().lower())


@dataclasses.dataclass(frozen=True)
class FactorResult:
    factor_name: str
    value: Optional[Any]  # True / a string / a number / None ("unknown" -- see module docstring)
    confidence: float  # 0.0 when value is None
    evidence: Optional[str]
    source: str
    reasoning: str


def _best_capability_match(capabilities: List[Dict[str, Any]], canonical_term: str) -> Optional[Dict[str, Any]]:
    matches = [c for c in capabilities if c.get("canonical_term") == canonical_term]
    if not matches:
        return None
    return max(matches, key=lambda c: c.get("confidence") or 0.0)


def assess_incoterm_capability(capabilities: List[Dict[str, Any]], incoterm: str) -> FactorResult:
    """Does the supplier's own website assert a given logistics
    capability (DDP, DAP, FOB, CIF, door-to-door, a UK/EU warehouse,
    customs expertise)? `incoterm` is free text, mapped through the
    same controlled vocabulary `capability_extractor` uses -- an
    unrecognised term is reported as unavailable, never silently
    matched against something unrelated.
    """
    term = map_to_canonical(incoterm)
    if term is None or term.category != CATEGORY_LOGISTICS:
        return FactorResult(
            factor_name=f"logistics:{incoterm}", value=None, confidence=0.0,
            evidence=None, source=SOURCE_UNAVAILABLE,
            reasoning=f"{incoterm!r} is not a recognised logistics term in the controlled vocabulary",
        )

    match = _best_capability_match(capabilities, term.canonical)
    if match is None:
        return FactorResult(
            factor_name=f"logistics:{term.canonical}", value=None, confidence=0.0,
            evidence=None, source=SOURCE_UNAVAILABLE,
            reasoning="no evidence found on the supplier's own website",
        )

    relationship_note = "operated in-house" if match["relationship"] == "in_house" else (
        "via a named logistics partner" if match["relationship"] == "subcontracted" else "asserted"
    )
    return FactorResult(
        factor_name=f"logistics:{term.canonical}", value=True, confidence=match.get("confidence") or 0.0,
        evidence=match.get("evidence"), source=SOURCE_OWN_WEBSITE,
        reasoning=f"supplier's own website states this capability ({relationship_note})",
    )


def assess_market_presence(
    supplier: Dict[str, Any], capabilities: List[Dict[str, Any]], market: str
) -> FactorResult:
    """Does the supplier serve `market` ('uk' | 'eu' | 'us' |
    'australia')? Checks three sources in order of evidentiary
    strength and returns the strongest one found -- see module
    docstring for why these are never blended into one number.
    """
    market_key = market.lower()
    factor_name = f"market_presence:{market_key}"

    if market_key in ("uk", "eu", "us"):
        shipment_count = supplier.get(f"confirmed_shipments_{market_key}") or 0
        if shipment_count > 0:
            return FactorResult(
                factor_name=factor_name, value=True, confidence=0.95,
                evidence=f"{shipment_count} confirmed shipment(s) on customs/trade record",
                source=SOURCE_TRADE_DATA,
                reasoning="verified via third-party customs/trade data — the strongest evidence tier available",
            )

    if country_to_market_key(supplier.get("country")) == market_key:
        return FactorResult(
            factor_name=factor_name, value=True, confidence=0.85,
            evidence=f"supplier is based in {supplier.get('country')}",
            source=SOURCE_SUPPLIER_LOCATION,
            reasoning="the supplier is physically located within this market — strong evidence of at "
                      "least domestic presence, though not proof of active export/commercial activity "
                      "there specifically",
        )

    canonical_term = _MARKET_CANONICAL_TERMS.get(market_key)
    if canonical_term:
        match = _best_capability_match(capabilities, canonical_term)
        if match is not None:
            return FactorResult(
                factor_name=factor_name, value=True, confidence=(match.get("confidence") or 0.0) * 0.9,
                evidence=match.get("evidence"), source=SOURCE_OWN_WEBSITE,
                reasoning="supplier's own website asserts this — self-reported but in their own words, "
                          "confidence discounted 10% relative to third-party trade data",
            )

    if market_key in ("uk", "eu", "us") and supplier.get(f"exports_to_{market_key}"):
        return FactorResult(
            factor_name=factor_name, value=True, confidence=0.5,
            evidence="self-reported 'main markets' field on a B2B directory listing",
            source=SOURCE_DIRECTORY_LISTING,
            reasoning="weakest evidence tier: a directory-listing checkbox, not verified against any "
                      "independent source",
        )

    return FactorResult(
        factor_name=factor_name, value=None, confidence=0.0, evidence=None, source=SOURCE_UNAVAILABLE,
        reasoning="no evidence found from trade data, the supplier's own website, or directory listings",
    )


def assess_certification(capabilities: List[Dict[str, Any]], certification: str) -> FactorResult:
    """Does the supplier hold a given certification (ISO 9001, IATF
    16949, CE marking, ...)? Reuses the exact same controlled
    vocabulary and evidence every other standard-holding check in this
    codebase already relies on -- this is a thin wrapper, not a new
    signal.
    """
    term = map_to_canonical(certification)
    if term is None or term.category != CATEGORY_STANDARD:
        return FactorResult(
            factor_name=f"certification:{certification}", value=None, confidence=0.0,
            evidence=None, source=SOURCE_UNAVAILABLE,
            reasoning=f"{certification!r} is not a recognised standard in the controlled vocabulary",
        )

    match = _best_capability_match(capabilities, term.canonical)
    if match is None:
        return FactorResult(
            factor_name=f"certification:{term.canonical}", value=None, confidence=0.0,
            evidence=None, source=SOURCE_UNAVAILABLE,
            reasoning="no evidence found on the supplier's own website",
        )
    return FactorResult(
        factor_name=f"certification:{term.canonical}", value=True, confidence=match.get("confidence") or 0.0,
        evidence=match.get("evidence"), source=SOURCE_OWN_WEBSITE,
        reasoning="supplier's own website states this certification",
    )


def assess_oem_readiness(capabilities: List[Dict[str, Any]], readiness_term: str) -> FactorResult:
    """Does the supplier assert an OEM-readiness capability (PPAP,
    CAD/engineering support, traceability system)? Identical shape to
    `assess_incoterm_capability`, scoped to CATEGORY_OEM_READINESS.
    """
    term = map_to_canonical(readiness_term)
    if term is None or term.category != CATEGORY_OEM_READINESS:
        return FactorResult(
            factor_name=f"oem_readiness:{readiness_term}", value=None, confidence=0.0,
            evidence=None, source=SOURCE_UNAVAILABLE,
            reasoning=f"{readiness_term!r} is not a recognised OEM-readiness term in the controlled vocabulary",
        )

    match = _best_capability_match(capabilities, term.canonical)
    if match is None:
        return FactorResult(
            factor_name=f"oem_readiness:{term.canonical}", value=None, confidence=0.0,
            evidence=None, source=SOURCE_UNAVAILABLE,
            reasoning="no evidence found on the supplier's own website",
        )
    return FactorResult(
        factor_name=f"oem_readiness:{term.canonical}", value=True, confidence=match.get("confidence") or 0.0,
        evidence=match.get("evidence"), source=SOURCE_OWN_WEBSITE,
        reasoning="supplier's own website states this capability",
    )


def assess_oem_supplier_status(capabilities: List[Dict[str, Any]]) -> FactorResult:
    """Does the supplier describe itself as an OEM/Tier-1 supplier?
    A market-presence-category claim (relationship is always
    "asserted" for this term -- see capability_vocabulary), not a
    process/capability one.
    """
    match = _best_capability_match(capabilities, "oem supplier")
    if match is None:
        return FactorResult(
            factor_name="oem_supplier_status", value=None, confidence=0.0, evidence=None,
            source=SOURCE_UNAVAILABLE, reasoning="no evidence found on the supplier's own website",
        )
    return FactorResult(
        factor_name="oem_supplier_status", value=True, confidence=match.get("confidence") or 0.0,
        evidence=match.get("evidence"), source=SOURCE_OWN_WEBSITE,
        reasoning="supplier's own website describes itself as an OEM/Tier-1 supplier",
    )


def assess_company_scale(supplier: Dict[str, Any]) -> List[FactorResult]:
    """Surfaces the financial/scale fields already on the supplier
    record (registered_capital_rmb, year_established, employee_count,
    factory_size_sqm, annual_revenue_usd) as individual factors, each
    honest about its own precision.

    Two of these (`employee_count`, `annual_revenue_usd`) are stored as
    free text (e.g. "50-100 employees", "$1M-$5M") because that's how
    they're actually reported by directories and registries -- this
    function deliberately does NOT attempt to parse them into a single
    numeric size tier. A parsed, false-precision number is a worse
    output than the honest range text it came from; a caller wanting a
    numeric comparison should parse it themselves with the specific
    tolerance their use case needs, not trust an invented one here.
    """
    results: List[FactorResult] = []

    registered_capital = supplier.get("registered_capital_rmb")
    if registered_capital:
        results.append(FactorResult(
            factor_name="registered_capital_rmb", value=registered_capital, confidence=0.85,
            evidence=f"RMB {registered_capital:,.0f} registered capital", source=SOURCE_REGISTRY,
            reasoning="from Qichacha (Chinese business registry) — a red-flag/scale signal, not a revenue figure",
        ))
    else:
        results.append(FactorResult(
            factor_name="registered_capital_rmb", value=None, confidence=0.0, evidence=None,
            source=SOURCE_UNAVAILABLE, reasoning="no Qichacha verification on file for this supplier",
        ))

    year_established = supplier.get("year_established")
    if year_established:
        years = datetime.now(timezone.utc).year - int(year_established)
        results.append(FactorResult(
            factor_name="company_age_years", value=years, confidence=0.8,
            evidence=f"established {year_established}", source=SOURCE_REGISTRY,
            reasoning="company age is a proxy for maturity, not directly for export experience specifically "
                      "— see assess_export_maturity for that distinction",
        ))
    else:
        results.append(FactorResult(
            factor_name="company_age_years", value=None, confidence=0.0, evidence=None,
            source=SOURCE_UNAVAILABLE, reasoning="year established not on file",
        ))

    for field, factor_name, source in (
        ("employee_count", "employee_count", SOURCE_DIRECTORY_LISTING),
        ("factory_size_sqm", "factory_size_sqm", SOURCE_DIRECTORY_LISTING),
        ("annual_revenue_usd", "annual_revenue_usd", SOURCE_DIRECTORY_LISTING),
    ):
        value = supplier.get(field)
        if value:
            results.append(FactorResult(
                factor_name=factor_name, value=value, confidence=0.4, evidence=str(value), source=source,
                reasoning="self-reported figure/range, not independently verified — reported as-is, "
                          "deliberately not converted to a single number (see this function's own docstring)",
            ))
        else:
            results.append(FactorResult(
                factor_name=factor_name, value=None, confidence=0.0, evidence=None,
                source=SOURCE_UNAVAILABLE, reasoning=f"{field} not on file",
            ))

    return results


def assess_export_maturity(supplier: Dict[str, Any]) -> FactorResult:
    """How many distinct markets does the supplier have *some*
    evidence of exporting to, combining confirmed shipments and
    self-reported export flags. A count, not a single confidence
    number, since "3 markets, weakly evidenced" and "3 markets,
    strongly evidenced" are genuinely different facts this single
    factor can't distinguish -- see assess_market_presence for the
    per-market breakdown with its own confidence.
    """
    markets_with_any_evidence = []
    for market in ("uk", "eu", "us"):
        if (supplier.get(f"confirmed_shipments_{market}") or 0) > 0 or supplier.get(f"exports_to_{market}"):
            markets_with_any_evidence.append(market)

    if not markets_with_any_evidence:
        return FactorResult(
            factor_name="export_market_count", value=None, confidence=0.0, evidence=None,
            source=SOURCE_UNAVAILABLE, reasoning="no evidence of exporting to any tracked market",
        )
    return FactorResult(
        factor_name="export_market_count", value=len(markets_with_any_evidence), confidence=0.6,
        evidence=f"evidence of activity in: {', '.join(m.upper() for m in markets_with_any_evidence)}",
        source=SOURCE_TRADE_DATA,
        reasoning="count of tracked markets (UK/EU/US) with at least directory-level or trade-data evidence "
                  "— see assess_market_presence for the confidence of each market individually",
    )
