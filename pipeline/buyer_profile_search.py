"""
pipeline/buyer_profile_search.py

Ties `commercial_scoring`, `commercial_probability`, and
`storage.repository.search_suppliers_full` together into the single
query a buyer profile actually needs — extending the existing search
and ranking system, not a parallel scoring engine, per the
commercial-intelligence spec's own explicit instruction.

Required vs. preferred — the actual design decision
------------------------------------------------------
A `BuyerProfile`'s fields split into two genuinely different roles,
and conflating them would either wrongly exclude good suppliers or
wrongly include bad ones:

- `destination_country`, `required_capabilities`, `manufacturers_only`
  are REQUIRED — passed straight to `search_suppliers_full`'s existing
  filters, exactly as a direct CLI/API caller would. A supplier
  missing one of these is excluded, the same AND semantics
  `search_suppliers_full` already documents for `required_capabilities`.
- `preferred_incoterm`, `target_market`, `min_export_experience_years`,
  `preferred_payment_terms_days`, `min_company_size` are PREFERENCES —
  computed as commercial-compatibility factors and attached to every
  result for ranking and display, but never used to exclude a
  supplier. A supplier with no evidence of DDP shipping might still be
  exactly the right factory; it just ranks lower, it doesn't
  disappear.

Two scores, kept separate, never silently blended
-------------------------------------------------------
Per the spec's own worked example (Technical 92, Commercial 84,
Overall 89), this returns the supplier's existing `composite_score`
(technical/verification-based, entirely unchanged) alongside a new
`commercial_compatibility_score`, both visible — never collapsed into
one number that would hide which dimension actually drove a
supplier's ranking. Combining them into a single "Overall Procurement
Suitability" figure, if ever wanted, is left to the caller
(frontend/API consumer), since the right weighting between technical
fit and commercial fit is a buyer judgement call this module has no
basis to make silently.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Optional

from verification.commercial_probability import estimate_commercial_flexibility
from verification.commercial_scoring import (
    assess_company_scale,
    assess_export_maturity,
    assess_incoterm_capability,
    assess_market_presence,
    assess_oem_supplier_status,
    country_to_market_key,
)


def _compute_commercial_compatibility_score(factors: List[Any]) -> Optional[float]:
    """Weighted average of confidence over available (non-None-value)
    factors, 0-100. Returns None, not 0, when nothing was available to
    score -- 0 would misleadingly imply confirmed poor compatibility,
    exactly the unknown-vs-negative conflation this whole codebase has
    been built to avoid.
    """
    scored = [f for f in factors if f is not None and f.value is not None]
    if not scored:
        return None
    return round(sum(f.confidence for f in scored) / len(scored) * 100, 1)


def search_suppliers_for_buyer_profile(
    repo: Any,
    buyer_profile: Dict[str, Any],
    *,
    product_query: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """The single query a buyer profile is actually for: applies its
    required fields as filters (see module docstring), then computes
    and attaches commercial-compatibility factors for its preferred
    fields against every result, without excluding anyone on them.

    Each result gains a `commercial_compatibility` key: a dict of
    every computed `FactorResult`/`CommercialFlexibilityEstimate`
    (as plain dicts, evidence and confidence intact), plus a top-level
    `commercial_compatibility_score` (0-100, or `None` if nothing was
    available to score — never fabricated as 0). Results are sorted by
    that score first, `composite_score` (the existing technical score)
    second, so a genuinely better-evidenced commercial fit surfaces
    first among otherwise-similar technical scores, without ever
    overriding a hard technical disqualification (missing a required
    certification already excluded the supplier at the filter step,
    long before this ranking happens).
    """
    results = repo.search_suppliers_full(
        product_query=product_query,
        required_capabilities=buyer_profile.get("required_capabilities") or [],
        manufacturers_only=buyer_profile.get("manufacturers_only", True),
        country=buyer_profile.get("destination_country"),
        limit=limit,
    )

    for supplier in results:
        capabilities = repo.get_capabilities(supplier["id"])
        factors: List[Any] = []

        if buyer_profile.get("preferred_incoterm"):
            factors.append(assess_incoterm_capability(capabilities, buyer_profile["preferred_incoterm"]))

        market_key = country_to_market_key(buyer_profile.get("destination_country"))
        if market_key:
            factors.append(assess_market_presence(supplier, capabilities, market_key))

        if (buyer_profile.get("target_market") or "").strip().lower() == "oem":
            factors.append(assess_oem_supplier_status(capabilities))

        export_maturity = assess_export_maturity(supplier)
        if buyer_profile.get("min_export_experience_years"):
            factors.append(export_maturity)

        payment_estimate = None
        if buyer_profile.get("preferred_payment_terms_days"):
            payment_estimate = estimate_commercial_flexibility(
                supplier, f"{buyer_profile['preferred_payment_terms_days']}-day payment terms"
            )

        scale_factors = assess_company_scale(supplier)

        supplier["commercial_compatibility"] = {
            "factors": [dataclasses.asdict(f) for f in factors],
            "scale": [dataclasses.asdict(f) for f in scale_factors],
            "export_maturity": dataclasses.asdict(export_maturity),
            "payment_terms_probability": dataclasses.asdict(payment_estimate) if payment_estimate else None,
        }
        supplier["commercial_compatibility_score"] = _compute_commercial_compatibility_score(factors)

    results.sort(
        key=lambda s: (
            s.get("commercial_compatibility_score") if s.get("commercial_compatibility_score") is not None else -1,
            s.get("composite_score") or 0,
        ),
        reverse=True,
    )
    return results
