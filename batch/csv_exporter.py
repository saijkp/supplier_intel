"""
batch/csv_exporter.py

Flattens a completed batch upload's results (batch_upload_rows + the
resolved suppliers they point at) into one CSV, one row per input
company -- original spreadsheet columns preserved on the left, exactly
as batch/csv_parser.py read them, followed by what enrichment found.

Confidence-aware suffixing ("(unverified)" on low-confidence values) is
step 2's job, once field_provenance covers more than just
canonical_name -- deliberately not attempted here yet rather than
half-built against a provenance system that doesn't exist for most
fields.
"""

from __future__ import annotations

import csv
import io
from typing import Any, Dict, List, Optional

from storage.repository import SupplierRepository

# Columns appended after the original spreadsheet columns, in this
# order. A fixed, small set for step 1 -- the per-field confidence/
# provenance columns (step 2) extend this, they don't replace it.
_RESULT_COLUMNS = (
    "status", "company_name", "name_source", "resolved_domain",
    "primary_email", "secondary_emails", "primary_phone", "all_phones",
    "contact_form_url", "source_pages", "country",
    "address", "address_candidate",
    "error_message", "name_extraction_note",
)


def _excel_safe_number(value: str) -> str:
    """Wraps a numeric-looking string as an Excel formula-string
    ("="value"") so a long digit sequence (a phone number) doesn't get
    auto-converted to scientific notation on a plain double-click CSV
    open -- confirmed necessary even with a leading "+" already present
    (Excel's own numeric auto-detection still applies to "+"-prefixed
    long digit strings in at least some locales/versions). A non-Excel
    reader (plain text editor, a script re-parsing the CSV) sees the
    literal formula text rather than just the number -- see
    flatten_batch_results' own `excel_safe_phone` parameter to opt out."""
    return f'="{value}"'


def flatten_batch_results(
    rows: List[Dict[str, Any]], repo: Optional[SupplierRepository] = None,
    excel_safe_phone: bool = True,
) -> str:
    """`rows` is storage.repository.SupplierRepository.get_batch_upload_rows()'s
    own return shape. Returns CSV text (not bytes) -- caller decides
    encoding/response headers. Never raises for a row with no resolved
    supplier (needs_url/needs_name/failed rows) -- those columns are
    just left blank.

    `excel_safe_phone`: wraps `primary_phone` as an Excel formula-
    string (see `_excel_safe_number`) so it opens as text, not
    scientific notation, in Excel -- the default, since that's where
    this file normally lands. Pass False for a clean, plain
    machine-readable CSV (e.g. for scripted re-processing)."""
    repo = repo or SupplierRepository()

    # Union of every original column across all rows, in first-seen
    # order -- a batch upload isn't guaranteed every row has identical
    # columns (a human-edited spreadsheet rarely is), so this can't
    # just take the first row's keys.
    original_columns: List[str] = []
    seen: set = set()
    for row in rows:
        for col in (row.get("original_columns") or {}).keys():
            if col not in seen:
                seen.add(col)
                original_columns.append(col)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([*original_columns, *_RESULT_COLUMNS])

    supplier_cache: Dict[int, Optional[Dict[str, Any]]] = {}
    # Latest address_candidate value per supplier -- populated only when
    # the trusted-address guard blocked an extraction from being applied
    # (see batch_service.py's _attempt_address_extraction). Without this,
    # a supplier that already had an address (e.g. from a bulk import)
    # would show no evidence at all of whether extraction worked, since
    # the applied `address` column just echoes the pre-existing value.
    address_candidate_cache: Dict[int, str] = {}
    # All typed phone numbers per supplier (supplier_phone_numbers) --
    # see storage.repository.save_phone_numbers/get_phone_numbers.
    phone_numbers_cache: Dict[int, List[Dict[str, Any]]] = {}

    for row in rows:
        original = row.get("original_columns") or {}
        original_values = [original.get(col, "") for col in original_columns]

        supplier_id = row.get("supplier_id")
        supplier: Optional[Dict[str, Any]] = None
        address_candidate = ""
        all_phones = ""
        if supplier_id is not None:
            if supplier_id not in supplier_cache:
                supplier_cache[supplier_id] = repo.get_supplier(supplier_id)
            supplier = supplier_cache[supplier_id]

            if supplier_id not in address_candidate_cache:
                entries = repo.get_field_provenance(supplier_id, field_name="address_candidate")
                address_candidate_cache[supplier_id] = (entries[-1].get("value") or "") if entries else ""
            address_candidate = address_candidate_cache[supplier_id]

            if supplier_id not in phone_numbers_cache:
                phone_numbers_cache[supplier_id] = repo.get_phone_numbers(supplier_id)
            all_phones = "; ".join(
                f'{p["phone_number"]} ({p["phone_type"]})' for p in phone_numbers_cache[supplier_id]
            )

        # Prefer the live supplier record's canonical_name over the
        # batch row's own snapshot -- it's the authoritative, current
        # value (could have changed since this row was written, e.g.
        # merged into an existing record with a different name), the
        # row-level copy is a consistency nicety, not the source of truth.
        company_name = (supplier or {}).get("canonical_name") or row.get("company_name") or ""

        primary_phone = (supplier or {}).get("primary_phone") or ""
        if primary_phone and excel_safe_phone:
            primary_phone = _excel_safe_number(primary_phone)

        result_values = [
            row.get("status") or "",
            company_name,
            row.get("name_source") or "",
            (supplier or {}).get("domain") or "",
            (supplier or {}).get("primary_email") or "",
            "; ".join((supplier or {}).get("secondary_emails") or []),
            primary_phone,
            all_phones,
            (supplier or {}).get("contact_form_url") or "",
            "; ".join((supplier or {}).get("contact_source_pages") or []),
            (supplier or {}).get("country") or "",
            (supplier or {}).get("address") or "",
            address_candidate,
            row.get("error_message") or "",
            row.get("name_extraction_note") or "",
        ]

        writer.writerow([*original_values, *result_values])

    return output.getvalue()
