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
    "primary_email", "primary_phone", "contact_form_url", "country",
    "error_message",
)


def flatten_batch_results(rows: List[Dict[str, Any]], repo: Optional[SupplierRepository] = None) -> str:
    """`rows` is storage.repository.SupplierRepository.get_batch_upload_rows()'s
    own return shape. Returns CSV text (not bytes) -- caller decides
    encoding/response headers. Never raises for a row with no resolved
    supplier (needs_url/needs_name/failed rows) -- those columns are
    just left blank."""
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

    for row in rows:
        original = row.get("original_columns") or {}
        original_values = [original.get(col, "") for col in original_columns]

        supplier_id = row.get("supplier_id")
        supplier: Optional[Dict[str, Any]] = None
        if supplier_id is not None:
            if supplier_id not in supplier_cache:
                supplier_cache[supplier_id] = repo.get_supplier(supplier_id)
            supplier = supplier_cache[supplier_id]

        # Prefer the live supplier record's canonical_name over the
        # batch row's own snapshot -- it's the authoritative, current
        # value (could have changed since this row was written, e.g.
        # merged into an existing record with a different name), the
        # row-level copy is a consistency nicety, not the source of truth.
        company_name = (supplier or {}).get("canonical_name") or row.get("company_name") or ""

        result_values = [
            row.get("status") or "",
            company_name,
            row.get("name_source") or "",
            (supplier or {}).get("domain") or "",
            (supplier or {}).get("primary_email") or "",
            (supplier or {}).get("primary_phone") or "",
            (supplier or {}).get("contact_form_url") or "",
            (supplier or {}).get("country") or "",
            row.get("error_message") or "",
        ]

        writer.writerow([*original_values, *result_values])

    return output.getvalue()
