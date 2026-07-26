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
    "id", "canonical_name", "country", "city", "domain",
    "is_manufacturer", "manufacturer_confidence",
    "uscc", "uscc_verified", "business_scope", "registered_capital_rmb",
    "iso_9001", "e_mark_certified", "ukca_certified", "ce_certified",
    "confirmed_shipments_uk", "confirmed_shipments_eu", "confirmed_shipments_us",
    "contact_name", "primary_email", "primary_phone",
    "alibaba_url", "indiamart_url", "hktdc_url",
    "composite_score", "recommendation", "source_count",
    "manufacturer_signals",
]


def _prepare_supplier_for_csv(supplier: Dict[str, Any]) -> Dict[str, Any]:
    """CSV cells can't hold a Python list — flatten manufacturer_signals
    into a single semicolon-joined string so the evidence is still
    visible in a spreadsheet, not silently dropped."""
    row = dict(supplier)
    signals = row.get("manufacturer_signals")
    if isinstance(signals, list):
        row["manufacturer_signals"] = "; ".join(signals)
    return row


def suppliers_to_csv_string(suppliers: List[Dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for supplier in suppliers:
        row = _prepare_supplier_for_csv(supplier)
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
