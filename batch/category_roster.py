"""
batch/category_roster.py

Shared "resolve an audited category to its checked-in roster, then to
live supplier records" logic -- factored out of main.py's `status
--category` (the first caller) so the new Audit API endpoints
(api/app.py) can reuse the exact same resolution instead of a second,
independently-drifting implementation.

CATEGORY_ROSTERS is the single source of truth for which categories
have a roster checked in (data/source_files/<dir>/) -- both main.py
and api/app.py import it from here now, rather than main.py keeping
its own private copy.

Confirmed suppliers are always re-checked against the live DB
(supplier still exists, still unflagged) rather than trusting the
roster snapshot blindly -- same reasoning as main.py's own
`_status_for_category`: the roster records a point-in-time audit
result, but `suppliers.flagged` is the one place exclusion is
actually tracked going forward (see standing rule 8 in CLAUDE.md).
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List, Optional

from deduplication.domain_utils import extract_domain
from storage.repository import SupplierRepository

# Maps a category name (lowercased) to its roster directory under
# data/source_files/. One entry per audited category -- a category
# with no roster checked in yet should fail with a clear message, not
# silently list itself as available with zero candidates.
CATEGORY_ROSTERS: Dict[str, str] = {
    "injection moulding": "injection_moulding_100",
}

_ROSTER_ROOT = Path(__file__).parent.parent / "data" / "source_files"


def roster_dir_for_category(category: str) -> Optional[Path]:
    dir_name = CATEGORY_ROSTERS.get(category.strip().lower())
    if dir_name is None:
        return None
    roster_dir = _ROSTER_ROOT / dir_name
    return roster_dir if roster_dir.exists() else None


def read_roster_csv(roster_dir: Path, filename: str) -> List[Dict[str, Any]]:
    path = roster_dir / filename
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def resolve_confirmed_suppliers(
    category: str, repo: Optional[SupplierRepository] = None,
) -> Dict[str, Any]:
    """Resolves `category`'s confirmed.csv roster against the live DB.
    Returns a dict with:
      - "suppliers": full supplier dicts, live and unflagged (the ones
        actually still "confirmed" right now)
      - "drifted_to_excluded": roster rows now flagged in the DB
      - "missing": roster rows with no matching supplier at all

    Returns None for "suppliers"/other keys (empty results) if the
    category has no checked-in roster -- callers should check
    `roster_dir_for_category(category)` first if they need to
    distinguish "no roster" from "roster with zero confirmed rows"."""
    roster_dir = roster_dir_for_category(category)
    if roster_dir is None:
        return {"suppliers": [], "drifted_to_excluded": [], "missing": []}

    repo = repo or SupplierRepository()
    confirmed_roster = read_roster_csv(roster_dir, "confirmed.csv")

    live_suppliers: List[Dict[str, Any]] = []
    drifted_to_excluded: List[str] = []
    missing: List[str] = []

    for row in confirmed_roster:
        domain = extract_domain(row.get("Website") or "")
        supplier = repo.find_by_domain(domain) if domain else None
        if supplier is None:
            missing.append(row.get("Company Name") or "")
        elif supplier.get("flagged"):
            drifted_to_excluded.append(row.get("Company Name") or "")
        else:
            live_suppliers.append(supplier)

    return {"suppliers": live_suppliers, "drifted_to_excluded": drifted_to_excluded, "missing": missing}
