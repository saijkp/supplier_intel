"""
batch/csv_parser.py

Parses an uploaded CSV into per-row candidates for batch/batch_service.py,
fuzzy-matching messy real-world column headers ("Company", "Website URL",
"URL", "Company Name") onto the two fields batch enrichment actually
needs -- company_name and website. Every original column is preserved
verbatim per row (for export pass-through, see batch/csv_exporter.py) --
this module only detects which columns to READ FROM, it never drops or
renames anything in the source data.

Deliberately does no row classification (needs_url/needs_name/ready) or
enrichment decisions -- that's batch/batch_service.py's job. This module's
only job is "what are the rows, and which columns are company_name/
website," so it can be tested and reasoned about in isolation.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from rapidfuzz import fuzz

# Canonical column aliases, checked via fuzzy match (not exact) against
# each real header -- real spreadsheets vary: "Company", "Company Name",
# "Business Name", "Supplier"; "Website", "URL", "Website URL", "Site".
_COMPANY_NAME_ALIASES: tuple = (
    "company name", "company", "business name", "supplier", "supplier name",
    "organisation", "organization", "org name", "vendor", "vendor name",
)
_WEBSITE_ALIASES: tuple = (
    "website", "website url", "url", "site", "web site", "domain",
    "company website", "web address", "homepage", "link",
)
# Trusted directly into suppliers.country when present -- this is the
# uploader's OWN data, not a scraped extraction, same status as
# _COMPANY_NAME_ALIASES already has for canonical_name (see
# batch_service.py's _resolve_named_row/_resolve_placeholder_row).
_COUNTRY_ALIASES: tuple = ("country", "nation")

# rapidfuzz.fuzz.ratio, 0-100 -- checked against real messy header
# samples ("Company Name", "company", "Business Name ", "COMPANY") in
# tests/test_csv_parser.py before settling on this threshold.
_FUZZY_MATCH_THRESHOLD = 70.0


@dataclass
class ParsedRow:
    row_index: int
    original_columns: Dict[str, str]      # every column from this CSV row, verbatim (whitespace-stripped)
    company_name: Optional[str]           # detected company name, stripped, or None if no column matched / cell empty
    website: Optional[str]                # detected website, stripped, or None if no column matched / cell empty
    country: Optional[str] = None         # detected country, stripped, or None if no column matched / cell empty


@dataclass
class ParseResult:
    rows: List[ParsedRow] = field(default_factory=list)
    company_name_column: Optional[str] = None    # which real header was detected as company_name, or None if none matched
    website_column: Optional[str] = None          # which real header was detected as website, or None if none matched
    country_column: Optional[str] = None          # which real header was detected as country, or None if none matched
    # row_index of any row whose (company_name, website) pair repeats an
    # earlier row -- informational only; batch_service.py decides what
    # to do with it (e.g. skip a redundant real enrichment call and
    # reuse the first occurrence's resolved supplier), this module never
    # drops a row on its own account.
    duplicate_row_indices: List[int] = field(default_factory=list)


def _normalise_header(header: str) -> str:
    return header.strip().lower().replace("_", " ").replace("-", " ")


def _best_column_match(headers: List[str], aliases: tuple) -> Optional[str]:
    """The real header whose normalised text best fuzzy-matches any
    alias, or None if nothing clears _FUZZY_MATCH_THRESHOLD. Ties keep
    the first (leftmost) header, matching how a human skimming column
    order would naturally pick."""
    best_header: Optional[str] = None
    best_score = 0.0
    for header in headers:
        if not header:
            continue
        normalised = _normalise_header(header)
        for alias in aliases:
            score = fuzz.ratio(normalised, alias)
            if score > best_score:
                best_score = score
                best_header = header
    return best_header if best_score >= _FUZZY_MATCH_THRESHOLD else None


def parse_csv(file_content: bytes) -> ParseResult:
    """Never raises for ordinary messy input (empty file, no recognisable
    header, malformed rows) -- returns an empty/partial ParseResult
    instead, matching every other source's "return partial data, don't
    lose the batch to a parsing bug" discipline already established in
    this codebase (see normalizers/base_normalizer.py)."""
    result = ParseResult()
    if not file_content:
        return result

    try:
        text = file_content.decode("utf-8-sig")  # -sig strips a BOM, common in Excel-exported CSVs
    except UnicodeDecodeError:
        text = file_content.decode("utf-8", errors="replace")

    try:
        reader = csv.DictReader(io.StringIO(text))
        headers = reader.fieldnames or []
    except csv.Error:
        return result
    if not headers:
        return result

    company_col = _best_column_match(headers, _COMPANY_NAME_ALIASES)
    website_col = _best_column_match(headers, _WEBSITE_ALIASES)
    country_col = _best_column_match(headers, _COUNTRY_ALIASES)
    result.company_name_column = company_col
    result.website_column = website_col
    result.country_column = country_col

    seen_keys: set = set()
    for i, raw_row in enumerate(reader):
        original = {k: (v or "").strip() for k, v in raw_row.items() if k is not None}

        company_name = (original.get(company_col) or "").strip() or None if company_col else None
        website = (original.get(website_col) or "").strip() or None if website_col else None
        country = (original.get(country_col) or "").strip() or None if country_col else None

        key = (company_name or "", website or "")
        if key != ("", "") and key in seen_keys:
            result.duplicate_row_indices.append(i)
        else:
            seen_keys.add(key)

        result.rows.append(ParsedRow(
            row_index=i, original_columns=original,
            company_name=company_name, website=website, country=country,
        ))

    return result
