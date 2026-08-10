"""
reports/coverage.py

BOM category coverage analysis.

The point of this module is to answer one question: "for each thing Ifor
Williams actually buys, how many candidate suppliers do we hold, and how
far short of a usable shortlist are we?"

That is deliberately different from the global row count. A database of
10,000 suppliers that holds 900 brake makers and zero jockey wheel makers
is worse for procurement than a database of 1,200 with 40 of each, because
sourcing happens per category, not in aggregate. Expansion should therefore
be driven by the gaps this report surfaces rather than by whichever source
happens to be easiest to scrape next.

Tiers set the shortlist floor per category:
    A -> 60 candidates. High spend, safety-critical, or type-approval bound
         (axles, brakes, couplings, lighting). Wide funnel needed because
         attrition through certification and terms screening is brutal.
    B -> 40 candidates. Significant spend, ordinary sourcing risk.
    C -> 25 candidates. Commodity or low spend; a short list is fine.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from config.settings import DB_PATH

TIER_TARGETS: Dict[str, int] = {"A": 60, "B": 40, "C": 25}


@dataclass(frozen=True)
class BomCategory:
    """One purchasable category on the trailer BOM."""

    key: str
    label: str
    group: str
    tier: str
    include: tuple[str, ...]
    exclude: tuple[str, ...] = field(default=tuple())

    @property
    def target(self) -> int:
        return TIER_TARGETS[self.tier]


# ═══════════════════════════════════════════════════════════════
# Taxonomy
# ═══════════════════════════════════════════════════════════════
# `include` terms are matched case-insensitively as substrings against a
# supplier's product_keywords, primary_categories, trailer_components and
# notes. `exclude` terms veto a match on the same haystack — they exist
# because Automechanika-sourced keywords skew heavy commercial vehicle,
# so "air spring" and "fifth wheel" would otherwise inflate categories
# that mean something quite different on a 3.5t braked trailer.

BOM_CATEGORIES: tuple[BomCategory, ...] = (
    # ── Running gear ────────────────────────────────────────────
    BomCategory("axles", "Axles (braked/unbraked)", "Running gear", "A",
                ("axle", "stub axle", "beam axle", "torsion axle")),
    BomCategory("suspension", "Suspension & springs", "Running gear", "A",
                ("leaf spring", "suspension", "rubber torsion", "spring seat",
                 "u-bolt", "shackle", "spring bolt", "spring bush"),
                ("air spring", "air suspension")),
    BomCategory("brakes", "Brakes & drums", "Running gear", "A",
                ("brake", "drum", "brake shoe", "brake lining", "autoreverse",
                 "brake cable"),
                ("air brake", "disc brake caliper", "ebs", "abs module")),
    BomCategory("hubs_bearings", "Hubs & bearings", "Running gear", "B",
                ("hub", "bearing", "wheel bearing", "hub unit", "grease cap")),
    BomCategory("wheels_tyres", "Wheels & tyres", "Running gear", "B",
                ("wheel rim", "steel wheel", "tyre", "tire", "wheel nut",
                 "road wheel")),

    # ── Drawgear & coupling ─────────────────────────────────────
    BomCategory("couplings", "Couplings & hitches", "Drawgear", "A",
                ("coupling", "hitch", "towing eye", "drawbar eye", "ball hitch",
                 "overrun", "tow ball", "r55"),
                ("fifth-wheel", "fifth wheel", "kingpin")),
    BomCategory("jockey_props", "Jockey wheels & props", "Drawgear", "B",
                ("jockey", "prop stand", "corner steady", "support leg",
                 "landing leg")),
    BomCategory("breakaway", "Breakaway cables & chains", "Drawgear", "C",
                ("breakaway", "safety chain", "break-away")),
    BomCategory("drawbars", "Drawbars & A-frames", "Drawgear", "B",
                ("drawbar", "a-frame", "towing frame", "chassis beam")),

    # ── Body & structure ────────────────────────────────────────
    BomCategory("chassis_steel", "Chassis steel & sections", "Body", "B",
                ("chassis", "steel section", "rhs", "channel section",
                 "hot rolled", "galvanised steel")),
    BomCategory("flooring", "Flooring & decking", "Body", "B",
                ("floor", "phenolic", "plywood", "decking", "aluminium plank",
                 "chequer plate", "checker plate")),
    BomCategory("panels_mesh", "Side panels, mesh & gates", "Body", "B",
                ("mesh", "side panel", "gate", "sheeting", "louvre",
                 "aluminium panel", "sideboard")),
    BomCategory("ramps", "Ramps & tailgates", "Body", "A",
                ("ramp", "tailgate", "tail lift", "loading ramp", "ramp spring",
                 "gas strut")),
    BomCategory("mudguards", "Mudguards & wings", "Body", "C",
                ("mudguard", "mudflap", "wing", "fender", "splash guard")),
    BomCategory("canopy", "Canopies, hoops & covers", "Body", "B",
                ("canopy", "tarpaulin", "cover", "hoop", "curtain", "roof bow",
                 "pvc fabric")),

    # ── Hardware & fit-out ──────────────────────────────────────
    BomCategory("hinges", "Hinges", "Hardware", "B",
                ("hinge", "butt hinge", "weld-on hinge", "pivot")),
    BomCategory("locks_catches", "Locks, catches & latches", "Hardware", "B",
                ("lock", "latch", "catch", "over-centre", "overcentre",
                 "fastener clamp", "toggle clamp", "hasp", "padlock")),
    BomCategory("lashing", "Lashing rings & load restraint", "Hardware", "B",
                ("lashing", "load restraint", "tie down", "tie-down",
                 "ratchet strap", "d-ring", "anchor point", "rope hook")),
    BomCategory("fasteners", "Fasteners & fixings", "Hardware", "C",
                ("bolt", "nut", "rivet", "screw", "washer", "fastener")),
    BomCategory("winches", "Winches & recovery", "Hardware", "C",
                ("winch", "hand winch", "recovery strap", "snatch block")),
    BomCategory("handles", "Handles & grab rails", "Hardware", "C",
                ("handle", "grab rail", "grab handle", "step", "footplate")),

    # ── Electrical ──────────────────────────────────────────────
    BomCategory("lighting", "Lighting (rear, marker, LED)", "Electrical", "A",
                ("lamp", "light", "led", "indicator", "reflector",
                 "turn signal", "tail light")),
    BomCategory("wiring", "Wiring looms & connectors", "Electrical", "B",
                ("wiring", "loom", "harness", "connector", "7-pin", "13-pin",
                 "junction box", "socket", "plug")),

    # ── Hydraulics (tippers) ────────────────────────────────────
    BomCategory("hydraulics", "Hydraulic rams & power packs", "Hydraulics", "B",
                ("hydraulic", "cylinder", "ram", "power pack", "tipping gear",
                 "hydraulic pump")),
    BomCategory("hoses", "Hoses & fittings", "Hydraulics", "C",
                ("hose", "hydraulic fitting", "coupler", "quick release")),

    # ── Materials & finishing ───────────────────────────────────
    BomCategory("coatings", "Paint, coatings & galvanising", "Finishing", "C",
                ("paint", "coating", "galvanis", "galvaniz", "powder coat",
                 "zinc plating", "e-coat")),
    BomCategory("rubber_seals", "Rubber & seals", "Finishing", "C",
                ("rubber", "seal", "gasket", "grommet", "buffer", "bump stop")),
    BomCategory("plastics", "Plastic mouldings", "Finishing", "C",
                ("moulding", "molding", "injection", "rotational mould",
                 "plastic component")),
)


# ═══════════════════════════════════════════════════════════════
# Analysis
# ═══════════════════════════════════════════════════════════════

_HAYSTACK_COLUMNS = (
    "product_keywords",
    "primary_categories",
    "trailer_components",
    "notes",
    "canonical_name",
)


def _haystack(row: sqlite3.Row) -> str:
    """Flatten every text field that might carry product signal into one blob."""
    parts: List[str] = []
    for col in _HAYSTACK_COLUMNS:
        raw = row[col] if col in row.keys() else None
        if not raw:
            continue
        text = str(raw)
        # product_keywords etc. are stored as JSON arrays; unpack so that
        # "Brake drums" matches on "brake" without the quoting noise.
        if text.strip().startswith("["):
            try:
                parts.extend(str(x) for x in json.loads(text))
                continue
            except (ValueError, TypeError):
                pass
        parts.append(text)
    return " || ".join(parts).lower()


def _matches(hay: str, category: BomCategory) -> bool:
    if any(term in hay for term in category.exclude):
        # An exclusion only vetoes when nothing else in the row independently
        # qualifies — otherwise a supplier listing both "air springs" and
        # "leaf springs" would be dropped from suspension entirely.
        positives = [t for t in category.include if t in hay]
        strong = [t for t in positives if not any(e in t for e in category.exclude)]
        return bool(strong)
    return any(term in hay for term in category.include)


def analyse_coverage(
    db_path: Optional[str] = None,
    countries: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """
    Return per-category counts against tier targets.

    `countries`, when given, restricts the analysis to suppliers in those
    countries — useful for asking "how much of this category could plausibly
    quote DDP in EUR?" by passing the European and Turkish set.
    """
    conn = sqlite3.connect(str(db_path or DB_PATH))
    conn.row_factory = sqlite3.Row

    sql = "SELECT * FROM suppliers"
    params: List[Any] = []
    if countries:
        wanted = list(countries)
        sql += f" WHERE country IN ({','.join('?' * len(wanted))})"
        params = wanted

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    buckets: Dict[str, Dict[str, Any]] = {
        c.key: {
            "category": c,
            "total": 0,
            "with_domain": 0,
            "with_email": 0,
            "countries": {},
        }
        for c in BOM_CATEGORIES
    }

    unmatched = 0
    for row in rows:
        hay = _haystack(row)
        hit = False
        for cat in BOM_CATEGORIES:
            if not _matches(hay, cat):
                continue
            hit = True
            b = buckets[cat.key]
            b["total"] += 1
            if row["domain"]:
                b["with_domain"] += 1
            if row["primary_email"]:
                b["with_email"] += 1
            country = row["country"] or "Unknown"
            b["countries"][country] = b["countries"].get(country, 0) + 1
        if not hit:
            unmatched += 1

    results: List[Dict[str, Any]] = []
    for cat in BOM_CATEGORIES:
        b = buckets[cat.key]
        gap = max(0, cat.target - b["total"])
        pct = (b["total"] / cat.target * 100) if cat.target else 0.0
        results.append(
            {
                "key": cat.key,
                "label": cat.label,
                "group": cat.group,
                "tier": cat.tier,
                "total": b["total"],
                "with_domain": b["with_domain"],
                "with_email": b["with_email"],
                "target": cat.target,
                "gap": gap,
                "pct_of_target": round(pct, 1),
                "status": _status(b["total"], cat.target),
                "top_countries": sorted(
                    b["countries"].items(), key=lambda kv: kv[1], reverse=True
                )[:3],
            }
        )

    total_gap = sum(r["gap"] for r in results)
    return {
        "suppliers_analysed": len(rows),
        "unmatched": unmatched,
        "categories": results,
        "total_gap": total_gap,
        "target_total": sum(c.target for c in BOM_CATEGORIES),
        "covered_categories": sum(1 for r in results if r["gap"] == 0),
    }


def _status(total: int, target: int) -> str:
    if total == 0:
        return "EMPTY"
    if total >= target:
        return "OK"
    if total >= target * 0.5:
        return "THIN"
    return "CRITICAL"
