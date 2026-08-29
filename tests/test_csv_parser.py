"""
tests/test_csv_parser.py

Tests for batch/csv_parser.py -- fuzzy header matching against messy
real-world spreadsheet headers, and per-row extraction. No network, no
DB -- this module's whole job is "given raw CSV bytes, what are the
rows and which columns are company_name/website."
"""

from __future__ import annotations

from batch.csv_parser import parse_csv


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
        assert [r.row_index for r in result.rows] == [0, 1, 2, 3]
