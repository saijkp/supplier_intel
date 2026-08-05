"""
reports/generator.py

Generates human-readable reports (Markdown) and machine-readable
exports (CSV) from the supplier database — the "report generation +
export" milestone from the original build sequence.

Both entry points work directly off SupplierRepository.list_suppliers(),
so anything filterable there (recommendation, country, min composite
score) is filterable here too.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from jinja2 import Template

from storage.repository import SupplierRepository

MARKDOWN_TEMPLATE = Template(
    """\
# Supplier Report

Generated: {{ generated_at }}
{% if query %}Search: "{{ query }}"
{% endif %}Filters: recommendation={{ recommendation or 'any' }}, min_composite_score={{ min_score or 0 }}
Suppliers: {{ suppliers|length }}
{% if suppliers|length == 0 %}
No suppliers matched these filters.
{% endif %}
{% for s in suppliers %}
## {{ loop.index }}. {{ s.canonical_name }}

- **Recommendation:** {{ s.recommendation }} (composite score: {{ s.composite_score }}/100)
- **Country:** {{ s.country or 'Unknown' }}{% if s.city %}, {{ s.city }}{% endif %}
- **Manufacturer:** {{ 'Yes' if s.is_manufacturer else ('No' if s.is_manufacturer == 0 else 'Unknown') }} (confidence: {{ s.manufacturer_confidence or 0 }}/100)
{% if s.manufacturer_signals %}- **Manufacturer evidence:**
{% for signal in s.manufacturer_signals %}    - {{ signal }}
{% endfor %}{% endif %}
- **USCC verified:** {{ 'Yes' if s.uscc_verified else 'No' }}
- **Certifications:** {{ [
    'ISO 9001' if s.iso_9001 else None,
    'E-mark' if s.e_mark_certified else None,
    'UKCA' if s.ukca_certified else None,
    'CE' if s.ce_certified else None,
  ]|select('string')|join(', ') or 'None on file' }}
- **UK shipments confirmed:** {{ s.confirmed_shipments_uk or 0 }}
- **Contact:** {{ s.contact_name or 'Unknown' }}{% if s.primary_email %} — {{ s.primary_email }}{% endif %}
- **Sources confirming this record:** {{ s.source_count }}
{% if s.notes %}- **Notes:** {{ s.notes }}
{% endif %}
{% endfor %}"""
)


def generate_markdown_report(
    repo: SupplierRepository,
    recommendation: Optional[str] = None,
    min_score: Optional[int] = None,
    query: Optional[str] = None,
    limit: int = 50,
) -> str:
    """Render a Markdown report of suppliers matching the given filters,
    ordered by composite score (via repo.list_suppliers's default order)."""
    suppliers = repo.list_suppliers(
        recommendation=recommendation, min_composite_score=min_score, limit=limit,
    )
    return MARKDOWN_TEMPLATE.render(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        query=query,
        recommendation=recommendation,
        min_score=min_score,
        suppliers=suppliers,
    )


def save_markdown_report(
    repo: SupplierRepository,
    output_path: Union[Path, str],
    recommendation: Optional[str] = None,
    min_score: Optional[int] = None,
    query: Optional[str] = None,
    limit: int = 50,
) -> Path:
    content = generate_markdown_report(
        repo, recommendation=recommendation, min_score=min_score, query=query, limit=limit,
    )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return output_path


# Columns included in CSV exports — deliberately a curated subset of
# storage.repository.SUPPLIER_WRITABLE_FIELDS rather than every column,
# since a spreadsheet with 60+ columns is unusable in practice.
CSV_COLUMNS: List[str] = [
    "id", "canonical_name", "country", "city", "address", "domain",
    "is_manufacturer", "manufacturer_confidence",
    "uscc", "uscc_verified", "business_scope", "registered_capital_rmb",
    "iso_9001", "e_mark_certified", "ukca_certified", "ce_certified",
    "confirmed_shipments_uk", "confirmed_shipments_eu", "confirmed_shipments_us",
    "contact_name", "primary_email", "primary_phone",
    "alibaba_url", "indiamart_url", "hktdc_url",
    "composite_score", "recommendation", "source_count",
    "manufacturer_signals",
]

# Excel export is the fuller sibling of CSV_COLUMNS — same curated base
# plus the contact/address-enrichment fields (secondary_emails,
# contact_form_url, facility address verification, LinkedIn) that
# CSV_COLUMNS deliberately leaves out to stay a lean, quick-glance
# subset. These are exactly the fields
# verification/website_contact_extractor.py and
# verification/facility_address_verifier.py populate, so a spreadsheet
# pulled after an enrichment run actually shows what was found.
EXCEL_COLUMNS: List[str] = CSV_COLUMNS + [
    "secondary_emails", "contact_form_url",
    "facility_address_verified", "facility_address_verification_source",
    "linkedin_url",
]


def _prepare_supplier_for_export(supplier: Dict[str, Any]) -> Dict[str, Any]:
    """Neither CSV cells nor Excel cells can hold a Python list —
    flatten any list-valued field (manufacturer_signals,
    secondary_emails) into a single semicolon-joined string so the
    evidence is still visible in a spreadsheet, not silently dropped."""
    row = dict(supplier)
    for key, value in row.items():
        if isinstance(value, list):
            row[key] = "; ".join(str(v) for v in value)
    return row


# Columns for GET /sourcing/runs/{id}/export.csv -- a deliberately
# different, purpose-built column set from CSV_COLUMNS/EXCEL_COLUMNS
# above (which are a general-purpose curated subset for the whole
# database). This one exists to match a sourcing brief's own requested
# output shape (company name, country, city, website, export market,
# address, contact details, payment terms, verification status) plus
# the sourcing_* procurement-dossier fields sourcing/dossier_generator.py
# writes -- fields that are empty/irrelevant for the ~1,400 suppliers
# never processed by a sourcing run, so they don't belong in the
# general export.
SOURCING_CSV_COLUMNS: List[str] = [
    "id", "canonical_name", "country", "city", "domain",
    "active_export_countries", "address",
    "primary_email", "primary_phone", "whatsapp", "contact_form_url",
    "payment_terms_offered", "incoterms_supported",
    "sourcing_oem_odm_notes", "sourcing_factory_notes",
    "sourcing_engineering_notes", "sourcing_export_notes",
    "sourcing_volume_suitability", "sourcing_payment_terms_notes",
    "sourcing_verification_status",
    "is_manufacturer", "composite_score", "ai_confidence_score",
]


def suppliers_to_sourcing_csv_string(suppliers: List[Dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=SOURCING_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for supplier in suppliers:
        row = _prepare_supplier_for_export(supplier)
        writer.writerow({col: row.get(col, "") for col in SOURCING_CSV_COLUMNS})
    return buffer.getvalue()


def suppliers_to_csv_string(suppliers: List[Dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for supplier in suppliers:
        row = _prepare_supplier_for_export(supplier)
        writer.writerow({col: row.get(col, "") for col in CSV_COLUMNS})
    return buffer.getvalue()


def export_suppliers_csv(
    repo: SupplierRepository,
    output_path: Union[Path, str],
    recommendation: Optional[str] = None,
    min_score: Optional[int] = None,
    limit: int = 1000,
) -> Path:
    suppliers = repo.list_suppliers(
        recommendation=recommendation, min_composite_score=min_score, limit=limit,
    )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(suppliers_to_csv_string(suppliers), encoding="utf-8", newline="")
    return output_path


def suppliers_to_excel_bytes(suppliers: List[Dict[str, Any]]) -> bytes:
    """Same data/columns philosophy as suppliers_to_csv_string, as an
    actual .xlsx workbook instead of a text format -- a frozen header
    row and auto-sized-ish column widths so the file is usable the
    moment it's opened, not just technically correct."""
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Suppliers"
    sheet.append(EXCEL_COLUMNS)
    sheet.freeze_panes = "A2"

    for supplier in suppliers:
        row = _prepare_supplier_for_export(supplier)
        sheet.append([row.get(col, "") for col in EXCEL_COLUMNS])

    for i, col in enumerate(EXCEL_COLUMNS, start=1):
        width = max(len(col), 12)
        sheet.column_dimensions[sheet.cell(row=1, column=i).column_letter].width = min(width + 4, 40)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def export_suppliers_excel(
    repo: SupplierRepository,
    output_path: Union[Path, str],
    recommendation: Optional[str] = None,
    min_score: Optional[int] = None,
    limit: int = 1000,
) -> Path:
    suppliers = repo.list_suppliers(
        recommendation=recommendation, min_composite_score=min_score, limit=limit,
    )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(suppliers_to_excel_bytes(suppliers))
    return output_path
