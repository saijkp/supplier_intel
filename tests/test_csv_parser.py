"""
tests/test_csv_parser.py

Tests for batch/csv_parser.py -- fuzzy header matching against messy
real-world spreadsheet headers, and per-row extraction. No network, no
DB -- this module's whole job is "given raw CSV bytes, what are the
rows and which columns are company_name/website."
"""

from __future__ import annotations

import io

from openpyxl import Workbook

from batch.csv_parser import parse_batch_upload_file, parse_csv, parse_xlsx


def _xlsx_bytes(rows: list) -> bytes:
    """Builds a real in-memory .xlsx workbook from a list of row tuples
    (first row is the header row) -- same real openpyxl read/write path
    the app's own XLSX export already uses, not a hand-rolled fixture."""
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class TestHeaderDetection:

    def test_exact_canonical_headers_are_detected(self):
        result = parse_csv(b"Company Name,Website\nAcme Co,https://acme.com\n")
        assert result.company_name_column == "Company Name"
        assert result.website_column == "Website"

    def test_short_messy_headers_are_still_detected(self):
        result = parse_csv(b"Company,URL\nAcme Co,acme.com\n")
        assert result.company_name_column == "Company"
        assert result.website_column == "URL"

    def test_underscored_headers_are_detected(self):
        result = parse_csv(b"company_name,web_site\nAcme Co,acme.com\n")
        assert result.company_name_column == "company_name"
        assert result.website_column == "web_site"

    def test_headers_with_surrounding_whitespace_are_detected(self):
        result = parse_csv(b"  Supplier , Website URL \nAcme Co,acme.com\n")
        assert result.company_name_column == "  Supplier "
        assert result.website_column == " Website URL "

    def test_case_insensitive_detection(self):
        result = parse_csv(b"COMPANY,WEBSITE\nAcme Co,acme.com\n")
        assert result.company_name_column == "COMPANY"
        assert result.website_column == "WEBSITE"

    def test_variant_aliases_detected(self):
        result = parse_csv(b"Business Name,Domain\nAcme Co,acme.com\n")
        assert result.company_name_column == "Business Name"
        assert result.website_column == "Domain"

    def test_unrelated_headers_are_not_falsely_matched(self):
        result = parse_csv(b"random1,random2\nfoo,bar\n")
        assert result.company_name_column is None
        assert result.website_column is None

    def test_country_column_is_detected(self):
        result = parse_csv(b"Company Name,Website,Country\nAcme Co,https://acme.com,United Kingdom\n")
        assert result.country_column == "Country"
        assert result.rows[0].country == "United Kingdom"

    def test_country_column_variant_alias_detected(self):
        result = parse_csv(b"Company,URL,Nation\nAcme Co,acme.com,France\n")
        assert result.country_column == "Nation"

    def test_no_country_column_leaves_it_none_not_a_false_match(self):
        result = parse_csv(b"Company Name,Website,Notes\nAcme Co,https://acme.com,a note\n")
        assert result.country_column is None
        assert result.rows[0].country is None

    def test_product_keywords_column_is_detected(self):
        result = parse_csv(b"Company Name,Website,Category\nAcme Co,https://acme.com,gas cylinder manufacturer\n")
        assert result.product_keywords_column == "Category"
        assert result.rows[0].product_keywords == "gas cylinder manufacturer"

    def test_product_keywords_variant_alias_detected(self):
        result = parse_csv(b"Company,URL,Product Keywords\nAcme Co,acme.com,metal pressing\n")
        assert result.product_keywords_column == "Product Keywords"

    def test_no_product_keywords_column_leaves_it_none_not_a_false_match(self):
        result = parse_csv(b"Company Name,Website,Notes\nAcme Co,https://acme.com,a note\n")
        assert result.product_keywords_column is None
        assert result.rows[0].product_keywords is None

    def test_bare_product_header_is_not_falsely_matched(self):
        # Deliberately NOT an alias -- too generic, would false-match an
        # ordinary "Product"/"Product Name" column that has nothing to
        # do with a category tag (see csv_parser.py's own comment).
        result = parse_csv(b"Company Name,Website,Product\nAcme Co,https://acme.com,Widget X\n")
        assert result.product_keywords_column is None


class TestMissingColumns:

    def test_missing_website_column_entirely(self):
        result = parse_csv(b"Company Name,Notes\nAcme Co,a note\n")
        assert result.company_name_column == "Company Name"
        assert result.website_column is None
        assert result.rows[0].company_name == "Acme Co"
        assert result.rows[0].website is None

    def test_missing_company_name_column_entirely(self):
        result = parse_csv(b"Website,Notes\nacme.com,a note\n")
        assert result.company_name_column is None
        assert result.website_column == "Website"
        assert result.rows[0].company_name is None
        assert result.rows[0].website == "acme.com"

    def test_empty_cell_in_a_detected_column_is_none_not_empty_string(self):
        result = parse_csv(b"Company Name,Website\nAcme Co,\nBeta Co,https://beta.com\n")
        assert result.rows[0].website is None
        assert result.rows[1].website == "https://beta.com"

    def test_empty_file_returns_no_rows(self):
        result = parse_csv(b"")
        assert result.rows == []
        assert result.company_name_column is None

    def test_header_only_no_data_rows(self):
        result = parse_csv(b"Company Name,Website\n")
        assert result.rows == []
        assert result.company_name_column == "Company Name"

    def test_malformed_bytes_do_not_raise(self):
        parse_csv(b"\xff\xfe\x00\x01not valid utf8 or much of anything")  # must not raise


class TestOriginalColumnsPreserved:

    def test_every_original_column_survives_verbatim(self):
        result = parse_csv(b"Company Name,Website,Region,Notes\nAcme Co,acme.com,EU,cold lead\n")
        assert result.rows[0].original_columns == {
            "Company Name": "Acme Co", "Website": "acme.com", "Region": "EU", "Notes": "cold lead",
        }

    def test_bom_prefixed_file_parses_and_strips_the_bom_from_the_header(self):
        result = parse_csv("Company Name,Website\nAcme Co,acme.com\n".encode("utf-8-sig"))
        assert result.company_name_column == "Company Name"
        assert "﻿Company Name" not in (result.rows[0].original_columns or {})


class TestDuplicateRows:

    def test_exact_repeat_is_flagged(self):
        result = parse_csv(
            b"Company Name,Website\n"
            b"Acme Co,https://acme.com\n"
            b"Beta Co,https://beta.com\n"
            b"Acme Co,https://acme.com\n"
        )
        assert result.duplicate_row_indices == [2]

    def test_duplicate_rows_are_still_included_in_output_not_dropped(self):
        result = parse_csv(
            b"Company Name,Website\nAcme Co,https://acme.com\nAcme Co,https://acme.com\n"
        )
        assert len(result.rows) == 2

    def test_two_rows_both_missing_name_and_website_are_not_flagged_as_duplicates(self):
        """Two equally-uninformative rows aren't meaningfully "the same
        duplicate company" -- only a real, repeated (name, website)
        pair should be flagged."""
        result = parse_csv(b"Company Name,Website,Notes\n,,note one\n,,note two\n")
        assert result.duplicate_row_indices == []

    def test_different_website_same_name_is_not_a_duplicate(self):
        result = parse_csv(
            b"Company Name,Website\nAcme Co,https://acme.com\nAcme Co,https://acme-europe.com\n"
        )
        assert result.duplicate_row_indices == []

    def test_row_index_matches_actual_position(self):
        result = parse_csv(
            b"Company Name,Website\n"
            b"A,https://a.com\n"
            b"B,https://b.com\n"
            b"C,https://c.com\n"
            b"B,https://b.com\n"
        )
        assert result.duplicate_row_indices == [3]


class TestXlsxParsing:
    """parse_xlsx must match parse_csv's own fuzzy header detection,
    per-row extraction, and dedup discipline exactly -- same
    _build_parse_result underneath, just fed from a real workbook
    instead of decoded CSV text."""

    def test_exact_canonical_headers_are_detected(self):
        result = parse_xlsx(_xlsx_bytes([
            ["Company Name", "Website"],
            ["Acme Co", "https://acme.com"],
        ]))
        assert result.company_name_column == "Company Name"
        assert result.website_column == "Website"
        assert len(result.rows) == 1
        assert result.rows[0].company_name == "Acme Co"
        assert result.rows[0].website == "https://acme.com"

    def test_messy_alias_headers_are_still_detected(self):
        result = parse_xlsx(_xlsx_bytes([
            ["Business Name", "Domain", "Nation"],
            ["Acme Co", "acme.com", "France"],
        ]))
        assert result.company_name_column == "Business Name"
        assert result.website_column == "Domain"
        assert result.country_column == "Nation"
        assert result.rows[0].country == "France"

    def test_numeric_cells_are_coerced_to_stripped_strings(self):
        """openpyxl hands back native types (int/float/None), not
        strings -- a company name or website that Excel auto-formatted
        as a number must still come through as text, not crash."""
        result = parse_xlsx(_xlsx_bytes([
            ["Company Name", "Website"],
            [12345, "https://acme.com"],
        ]))
        assert result.rows[0].company_name == "12345"

    def test_duplicate_rows_detected_same_as_csv(self):
        result = parse_xlsx(_xlsx_bytes([
            ["Company Name", "Website"],
            ["Acme Co", "https://acme.com"],
            ["Beta Co", "https://beta.com"],
            ["Acme Co", "https://acme.com"],
        ]))
        assert result.duplicate_row_indices == [2]

    def test_blank_trailing_row_is_skipped_not_counted(self):
        result = parse_xlsx(_xlsx_bytes([
            ["Company Name", "Website"],
            ["Acme Co", "https://acme.com"],
            [None, None],
        ]))
        assert len(result.rows) == 1

    def test_empty_file_returns_empty_result_not_raise(self):
        result = parse_xlsx(b"")
        assert result.rows == []

    def test_corrupt_workbook_bytes_return_empty_result_not_raise(self):
        result = parse_xlsx(b"this is not a real xlsx file")
        assert result.rows == []
        assert result.company_name_column is None


class TestParseBatchUploadFileDispatch:

    def test_xlsx_filename_routes_to_xlsx_parser(self):
        result = parse_batch_upload_file(
            _xlsx_bytes([["Company Name", "Website"], ["Acme Co", "https://acme.com"]]),
            filename="companies.xlsx",
        )
        assert result.rows[0].company_name == "Acme Co"

    def test_csv_filename_routes_to_csv_parser(self):
        result = parse_batch_upload_file(
            b"Company Name,Website\nAcme Co,https://acme.com\n", filename="companies.csv",
        )
        assert result.rows[0].company_name == "Acme Co"

    def test_missing_filename_defaults_to_csv(self):
        result = parse_batch_upload_file(
            b"Company Name,Website\nAcme Co,https://acme.com\n", filename=None,
        )
        assert result.rows[0].company_name == "Acme Co"

    def test_unrecognised_extension_defaults_to_csv(self):
        result = parse_batch_upload_file(
            b"Company Name,Website\nAcme Co,https://acme.com\n", filename="companies.txt",
        )
        assert result.rows[0].company_name == "Acme Co"

    def test_legacy_xls_extension_is_not_routed_to_xlsx_parser(self):
        """openpyxl can't read legacy .xls -- routing it to parse_xlsx
        would silently return an empty result. Falls through to
        parse_csv instead (also empty for real binary .xls bytes, but
        an explicit, documented non-support rather than a surprise)."""
        result = parse_batch_upload_file(b"not real content", filename="companies.xls")
        assert result.rows == []
