"""
batch/tracker_exporter.py

Group 3 of the procurement-tracker feature set: exports a list of
already-confirmed candidate suppliers into the buyer's own tracker CSV
format -- exact column order/names matching their real "Injection
Moulding" tab (No. through Date Reviewed) so rows paste in without
reformatting or shifting any of the tracker's own columns.

Columns A-D and Qualified are always the literal text "Pending" --
100% manual, filled in by the buyer. "Pending" is an explicit
placeholder, not a verdict: it exists so a blank cell is never
ambiguous between "not yet reviewed" and "forgot to check." Nothing in
this module (or anywhere upstream of it) ever writes Pass/Fail/Clean/
Flagged/Y into them; see PASTE_RANGE_COLUMNS's own comment.

Evidence/helper columns (per-field source URLs, candidate facility
photos, a Street View link, a LinkedIn search link, certification
evidence, D-search snippets, Checks Remaining, Sort Key) are appended
AFTER Date Reviewed -- outside the paste range, so the main block
still pastes in clean while the evidence needed to audit each value
stays in the same file, one click away. Street View/LinkedIn links are
both free Google search-links, never an automated match judgment --
deliberately no proxy infrastructure and no reverse image search here
(out of scope given the deadline: most dead fetches are DNS failures a
proxy wouldn't fix, and neither of us has a working reverse-image-
search tool right now).

A second export, the removed-candidates list, mirrors the tracker's
own "Removed Candidates" tab (Company Name, Website, Reason) -- every
supplier a human has flagged=1 excluded (see storage/repository.py's
flagged/flag_reason fields, wired into search_suppliers/
search_suppliers_full/find_discovered_suppliers so an excluded record
can't silently resurface as a candidate later) among a given set of
supplier ids.
"""

from __future__ import annotations

import csv
import io
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from storage.repository import SupplierRepository

# Exact column order/names of the buyer's real tracker "Injection
# Moulding" tab, confirmed against their own exported CSV. A-D/
# Qualified are ALWAYS the literal text "Pending" here (see
# _PENDING_PLACEHOLDER); Notes/Date Reviewed are ALWAYS blank -- see
# module docstring.
PASTE_RANGE_COLUMNS: Tuple[str, ...] = (
    "No.", "Supplier Name", "Website", "Country", "Address", "Phone", "Email",
    "Factory Location",
    "A - Website Deep-Dive", "B - Certifications", "C - Factory Authenticity",
    "D - Reviews & Ratings", "Qualified", "Notes / Difficulties", "Date Reviewed",
)

# The literal placeholder written into A-D/Qualified on every export --
# an explicit "not yet reviewed" marker, never a verdict (see module
# docstring for why a blank cell alone is ambiguous here).
_PENDING_PLACEHOLDER = "Pending"

# Appended after the paste range -- never part of what gets pasted
# into the tracker itself. Phone/Email only have row-level
# (contact_source_pages), not per-value, source tracking -- see
# storage/database.py's contact_source_pages comment -- so those two
# columns show the page(s) checked for contact info, not a single
# precise per-value link, unlike Address/Factory Location which do
# have exact per-value provenance (field_provenance).
EVIDENCE_COLUMNS: Tuple[str, ...] = (
    "Address Source URL", "Phone Source Page(s)", "Email Source Page(s)",
    "Factory Location Source URL",
    "Candidate Facility Photo URLs", "Street View Link", "LinkedIn Search Link",
    "Certifications Claimed (source + evidence)",
    "D-Search: Scam", "D-Search: Review", "D-Search: Factory Tour",
    "Checks Remaining", "Sort Key (helper)",
)

REMOVED_CANDIDATES_COLUMNS: Tuple[str, ...] = ("Company Name", "Website", "Reason")

# Every field a fresh export leaves entirely to the buyer -- see
# module docstring. Always all four: this export only ever produces
# brand-new candidate rows, which never have any of A-D pre-filled.
_CHECKS_REMAINING_ON_A_FRESH_EXPORT = "A, B, C, D"

# The fields _evidence_score checks -- one point each, so Sort Key
# (helper) ranges 0-4. Used to rank rows by how much evidence is
# already on file (most review-ready first), never as a Qualified
# judgment -- it says nothing about whether the evidence is GOOD, only
# whether it exists yet.
_EVIDENCE_SCORE_FIELDS: Tuple[str, ...] = ("address", "primary_phone", "primary_email", "factory_location")


def _evidence_score(supplier: Dict[str, Any]) -> int:
    return sum(1 for field in _EVIDENCE_SCORE_FIELDS if supplier.get(field))


def _latest_provenance_source_url(repo: SupplierRepository, supplier_id: int, field_name: str) -> str:
    entries = repo.get_field_provenance(supplier_id, field_name=field_name)
    return (entries[-1].get("source_url") or "") if entries else ""


def _phone_source_pages(repo: SupplierRepository, supplier: Dict[str, Any]) -> str:
    # No per-phone source_url match attempted beyond the exact primary_phone
    # string -- supplier_phone_numbers.source_url exists, but
    # contact_source_pages is the honest fallback for what's actually
    # reliably available at export time (see EVIDENCE_COLUMNS comment).
    return "; ".join(supplier.get("contact_source_pages") or [])


def _street_view_link(address: str) -> str:
    """Free Google Maps search-link (no geocoding/Street-View-Static
    API dependency) -- one click to the address's approximate map
    location, not an automated match judgment. Empty when there's no
    address to link."""
    if not address:
        return ""
    return f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(address)}"


def _linkedin_search_link(company_name: str) -> str:
    """Same free-search-link pattern as _street_view_link, no LinkedIn
    API/scraping involved -- a site:linkedin.com Google search for the
    company name, one click for the buyer to manually check for a real
    company page. Deliberately not an automated match/verdict, and
    deliberately not proxy infrastructure or reverse image search (out
    of scope for now -- most dead fetches are DNS failures a proxy
    wouldn't fix, and neither of us has a working reverse-image-search
    tool). Empty when there's no name to search for."""
    if not company_name:
        return ""
    query = f'site:linkedin.com "{company_name}"'
    return f"https://www.google.com/search?q={urllib.parse.quote(query)}"


def _certifications_claimed(repo: SupplierRepository, supplier_id: int) -> str:
    """Export-only surfacing of certification claims the older,
    separate capability_extractor.py stage already found (category=
    'standard' in supplier_capabilities) -- no new extraction here.
    Blank for a supplier that stage has never run against."""
    parts = []
    for cap in repo.get_capabilities(supplier_id):
        if cap.get("category") != "standard":
            continue
        term = cap.get("canonical_term") or cap.get("reported_term") or ""
        source = cap.get("source_url") or ""
        evidence = (cap.get("evidence") or "").strip()
        parts.append(f'{term} [{source}] "{evidence}"')
    return " | ".join(parts)


def _reputation_snippets(repo: SupplierRepository, supplier_id: int, query_type: str) -> str:
    """Export-only surfacing of Group 2's opt-in D-search results.
    Blank unless --search-reputation was actually run for this
    supplier -- never a Clean/Flagged judgment, just the raw
    title/snippet/link each query returned."""
    parts = []
    for s in repo.get_reputation_snippets(supplier_id, query_type=query_type):
        title = s.get("title") or ""
        snippet = s.get("snippet") or ""
        link = s.get("link") or ""
        parts.append(f"{title}: {snippet} ({link})")
    return " | ".join(parts)


def build_tracker_export(supplier_ids: List[int], repo: Optional[SupplierRepository] = None) -> str:
    """Returns CSV text (not bytes) -- caller decides encoding/response
    headers. A supplier_id with no matching row is silently skipped
    (never raises) -- one stale/deleted id must never break the whole
    export.

    Row order: Sort Key (helper) descending -- most evidence-complete
    candidates first -- ties broken alphabetically by name for a
    stable, predictable order across repeated exports of the same set."""
    repo = repo or SupplierRepository()

    scored: List[Tuple[int, str, int, Dict[str, Any]]] = []
    for supplier_id in supplier_ids:
        supplier = repo.get_supplier(supplier_id)
        if supplier is None:
            continue
        scored.append((
            _evidence_score(supplier), (supplier.get("canonical_name") or "").lower(), supplier_id, supplier,
        ))
    scored.sort(key=lambda t: (-t[0], t[1]))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([*PASTE_RANGE_COLUMNS, *EVIDENCE_COLUMNS])

    for i, (score, _, supplier_id, supplier) in enumerate(scored, start=1):
        address = supplier.get("address") or ""
        phone = supplier.get("primary_phone") or ""
        email = supplier.get("primary_email") or ""
        factory_location = supplier.get("factory_location") or ""

        paste_row = [
            i, supplier.get("canonical_name") or "", supplier.get("domain") or "",
            supplier.get("country") or "", address, phone, email, factory_location,
            # A, B, C, D, Qualified -- always "Pending", never a verdict, see module docstring
            _PENDING_PLACEHOLDER, _PENDING_PLACEHOLDER, _PENDING_PLACEHOLDER,
            _PENDING_PLACEHOLDER, _PENDING_PLACEHOLDER,
            "", "",  # Notes / Difficulties, Date Reviewed -- always blank
        ]

        evidence_row = [
            _latest_provenance_source_url(repo, supplier_id, "address"),
            _phone_source_pages(repo, supplier),
            "; ".join(supplier.get("contact_source_pages") or []),
            _latest_provenance_source_url(repo, supplier_id, "factory_location"),
            "; ".join(supplier.get("candidate_facility_photo_urls") or []),
            _street_view_link(address),
            _linkedin_search_link(supplier.get("canonical_name") or ""),
            _certifications_claimed(repo, supplier_id),
            _reputation_snippets(repo, supplier_id, "scam"),
            _reputation_snippets(repo, supplier_id, "review"),
            _reputation_snippets(repo, supplier_id, "factory_tour"),
            _CHECKS_REMAINING_ON_A_FRESH_EXPORT,
            score,
        ]
        writer.writerow([*paste_row, *evidence_row])

    return output.getvalue()


def build_removed_candidates_export(
    supplier_ids: List[int], repo: Optional[SupplierRepository] = None,
) -> str:
    """Every supplier among `supplier_ids` that's flagged=1 excluded --
    mirrors the tracker's own "Removed Candidates" tab. Scoped to the
    ids passed in (the current export's own candidate universe), not
    every flagged supplier system-wide -- a company excluded from a
    DIFFERENT product category's sourcing run has no place on THIS
    tracker's removed-candidates list."""
    repo = repo or SupplierRepository()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(REMOVED_CANDIDATES_COLUMNS)

    for supplier_id in supplier_ids:
        supplier = repo.get_supplier(supplier_id)
        if supplier is None or not supplier.get("flagged"):
            continue
        domain = supplier.get("domain") or ""
        website = f"https://{domain}" if domain else ""
        writer.writerow([supplier.get("canonical_name") or "", website, supplier.get("flag_reason") or ""])

    return output.getvalue()
