"""
tests/test_csv_exporter.py

Tests for batch/csv_exporter.py -- flattening batch_upload_rows (+ the
suppliers they resolved to) into one CSV, original columns preserved on
the left. Fakes the repository entirely (no DB) -- get_supplier() is
the only method this module calls.
"""

from __future__ import annotations

import csv
import io

from batch.csv_exporter import flatten_batch_results


class FakeRepo:
    def __init__(self, suppliers=None, field_provenance=None, phone_numbers=None):
        self._suppliers = suppliers or {}
        # supplier_id -> list of {"field_name": ..., "value": ...} dicts,
        # oldest first -- matches the real repository's ORDER BY created_at.
        self._field_provenance = field_provenance or {}
        # supplier_id -> list of {"phone_number": ..., "phone_type": ...} dicts.
        self._phone_numbers = phone_numbers or {}
        self.get_supplier_calls = []

    def get_supplier(self, supplier_id):
        self.get_supplier_calls.append(supplier_id)
        return self._suppliers.get(supplier_id)

    def get_field_provenance(self, supplier_id, field_name=None):
        entries = self._field_provenance.get(supplier_id, [])
        if field_name is None:
            return entries
        return [e for e in entries if e.get("field_name") == field_name]

    def get_phone_numbers(self, supplier_id):
        return self._phone_numbers.get(supplier_id, [])


def _read_csv(text):
    return list(csv.DictReader(io.StringIO(text)))


class TestFlatteningBasics:

    def test_original_columns_come_before_result_columns(self):
        rows = [{
            "row_index": 0, "original_columns": {"Company Name": "Acme Co", "Region": "EU"},
            "status": "success", "company_name": "Acme Co", "name_source": "csv",
            "supplier_id": None, "error_message": None,
        }]
        csv_text = flatten_batch_results(rows, repo=FakeRepo())
        header = csv_text.splitlines()[0].split(",")
        assert header[:2] == ["Company Name", "Region"]
        assert "status" in header

    def test_one_row_in_one_row_out(self):
        rows = [{
            "row_index": 0, "original_columns": {"Company Name": "Acme Co"},
            "status": "success", "company_name": "Acme Co", "name_source": "csv",
            "supplier_id": None, "error_message": None,
        }]
        csv_text = flatten_batch_results(rows, repo=FakeRepo())
        parsed = _read_csv(csv_text)
        assert len(parsed) == 1

    def test_empty_rows_produce_header_only(self):
        csv_text = flatten_batch_results([], repo=FakeRepo())
        parsed = _read_csv(csv_text)
        assert parsed == []
        assert "status" in csv_text.splitlines()[0]


class TestOriginalColumnUnion:

    def test_columns_from_all_rows_are_unioned_in_first_seen_order(self):
        """A human-edited spreadsheet isn't guaranteed every row has
        identical columns."""
        rows = [
            {"row_index": 0, "original_columns": {"Company Name": "Acme", "Notes": "x"},
             "status": "success", "company_name": "Acme", "name_source": "csv", "supplier_id": None, "error_message": None},
            {"row_index": 1, "original_columns": {"Company Name": "Beta", "Region": "EU"},
             "status": "success", "company_name": "Beta", "name_source": "csv", "supplier_id": None, "error_message": None},
        ]
        csv_text = flatten_batch_results(rows, repo=FakeRepo())
        header = csv_text.splitlines()[0].split(",")
        assert header[:3] == ["Company Name", "Notes", "Region"]

    def test_row_missing_a_column_gets_a_blank_cell_not_a_shifted_row(self):
        rows = [
            {"row_index": 0, "original_columns": {"Company Name": "Acme", "Notes": "x"},
             "status": "success", "company_name": "Acme", "name_source": "csv", "supplier_id": None, "error_message": None},
            {"row_index": 1, "original_columns": {"Company Name": "Beta"},
             "status": "success", "company_name": "Beta", "name_source": "csv", "supplier_id": None, "error_message": None},
        ]
        csv_text = flatten_batch_results(rows, repo=FakeRepo())
        parsed = _read_csv(csv_text)
        assert parsed[1]["Notes"] == ""


class TestNeedsUrlRows:

    def test_needs_url_row_has_blank_enrichment_columns(self):
        rows = [{
            "row_index": 0, "original_columns": {"Company Name": "Acme Co"},
            "status": "needs_url", "company_name": "Acme Co", "name_source": "csv",
            "supplier_id": None, "error_message": None,
        }]
        csv_text = flatten_batch_results(rows, repo=FakeRepo())
        parsed = _read_csv(csv_text)
        assert parsed[0]["status"] == "needs_url"
        assert parsed[0]["resolved_domain"] == ""
        assert parsed[0]["primary_email"] == ""

    def test_needs_url_row_never_calls_get_supplier(self):
        repo = FakeRepo()
        rows = [{
            "row_index": 0, "original_columns": {"Company Name": "Acme Co"},
            "status": "needs_url", "company_name": "Acme Co", "name_source": "csv",
            "supplier_id": None, "error_message": None,
        }]
        flatten_batch_results(rows, repo=repo)
        assert repo.get_supplier_calls == []


class TestResolvedSupplierFields:

    def test_resolved_supplier_fields_are_pulled_in(self):
        repo = FakeRepo(suppliers={
            5: {"id": 5, "canonical_name": "Acme Trailer Co", "domain": "acmetrailer.com",
                "primary_email": "sales@acmetrailer.com", "primary_phone": "+44 123", "country": "United Kingdom"},
        })
        rows = [{
            "row_index": 0, "original_columns": {"Company Name": "Acme Co"},
            "status": "success", "company_name": "Acme Co", "name_source": "csv",
            "supplier_id": 5, "error_message": None,
        }]
        csv_text = flatten_batch_results(rows, repo=repo, excel_safe_phone=False)
        parsed = _read_csv(csv_text)
        assert parsed[0]["resolved_domain"] == "acmetrailer.com"
        assert parsed[0]["primary_email"] == "sales@acmetrailer.com"
        assert parsed[0]["primary_phone"] == "+44 123"
        assert parsed[0]["country"] == "United Kingdom"

    def test_live_supplier_canonical_name_wins_over_stale_row_snapshot(self):
        """The supplier record is the source of truth -- a row's own
        company_name can be a stale snapshot (e.g. a domain-derived
        placeholder later replaced with a real extracted name)."""
        repo = FakeRepo(suppliers={
            5: {"id": 5, "canonical_name": "Acme Trailer Manufacturing Ltd", "domain": "acmetrailer.com"},
        })
        rows = [{
            "row_index": 0, "original_columns": {"Website": "acmetrailer.com"},
            "status": "success", "company_name": "Acmetrailer", "name_source": "inferred_from_domain",
            "supplier_id": 5, "error_message": None,
        }]
        csv_text = flatten_batch_results(rows, repo=repo)
        parsed = _read_csv(csv_text)
        assert parsed[0]["company_name"] == "Acme Trailer Manufacturing Ltd"

    def test_same_supplier_id_across_rows_only_queries_repo_once(self):
        repo = FakeRepo(suppliers={5: {"id": 5, "canonical_name": "Acme Co", "domain": "acme.com"}})
        rows = [
            {"row_index": 0, "original_columns": {"Website": "acme.com"}, "status": "success",
             "company_name": "Acme Co", "name_source": "csv", "supplier_id": 5, "error_message": None},
            {"row_index": 1, "original_columns": {"Website": "acme.com"}, "status": "success",
             "company_name": "Acme Co", "name_source": "csv", "supplier_id": 5, "error_message": None},
        ]
        flatten_batch_results(rows, repo=repo)
        assert repo.get_supplier_calls == [5]

    def test_supplier_id_with_no_matching_row_does_not_raise(self):
        repo = FakeRepo(suppliers={})  # supplier_id 5 not found -- e.g. deleted since
        rows = [{
            "row_index": 0, "original_columns": {"Website": "acme.com"},
            "status": "success", "company_name": "Acme Co", "name_source": "csv",
            "supplier_id": 5, "error_message": None,
        }]
        csv_text = flatten_batch_results(rows, repo=repo)  # must not raise
        parsed = _read_csv(csv_text)
        assert parsed[0]["resolved_domain"] == ""


class TestPhoneAndEmailColumns:

    def test_secondary_emails_joined_with_semicolons(self):
        repo = FakeRepo(suppliers={5: {
            "id": 5, "canonical_name": "Acme Co",
            "secondary_emails": ["sales@acme.com", "support@acme.com"],
        }})
        rows = [{
            "row_index": 0, "original_columns": {"Company Name": "Acme Co"},
            "status": "success", "company_name": "Acme Co", "name_source": "csv",
            "supplier_id": 5, "error_message": None,
        }]
        parsed = _read_csv(flatten_batch_results(rows, repo=repo))
        assert parsed[0]["secondary_emails"] == "sales@acme.com; support@acme.com"

    def test_secondary_emails_blank_when_empty(self):
        repo = FakeRepo(suppliers={5: {"id": 5, "canonical_name": "Acme Co"}})
        rows = [{
            "row_index": 0, "original_columns": {"Company Name": "Acme Co"},
            "status": "success", "company_name": "Acme Co", "name_source": "csv",
            "supplier_id": 5, "error_message": None,
        }]
        parsed = _read_csv(flatten_batch_results(rows, repo=repo))
        assert parsed[0]["secondary_emails"] == ""

    def test_all_phones_shows_number_and_type_pairs(self):
        repo = FakeRepo(
            suppliers={5: {"id": 5, "canonical_name": "Acme Co"}},
            phone_numbers={5: [
                {"phone_number": "+862112345678", "phone_type": "landline"},
                {"phone_number": "+8613800001111", "phone_type": "mobile"},
            ]},
        )
        rows = [{
            "row_index": 0, "original_columns": {"Company Name": "Acme Co"},
            "status": "success", "company_name": "Acme Co", "name_source": "csv",
            "supplier_id": 5, "error_message": None,
        }]
        parsed = _read_csv(flatten_batch_results(rows, repo=repo))
        assert parsed[0]["all_phones"] == "+862112345678 (landline); +8613800001111 (mobile)"

    def test_source_pages_joined_with_semicolons(self):
        repo = FakeRepo(suppliers={5: {
            "id": 5, "canonical_name": "Acme Co",
            "contact_source_pages": ["https://acme.com/", "https://acme.com/contact"],
        }})
        rows = [{
            "row_index": 0, "original_columns": {"Company Name": "Acme Co"},
            "status": "success", "company_name": "Acme Co", "name_source": "csv",
            "supplier_id": 5, "error_message": None,
        }]
        parsed = _read_csv(flatten_batch_results(rows, repo=repo))
        assert parsed[0]["source_pages"] == "https://acme.com/; https://acme.com/contact"


class TestExcelSafePhone:

    def test_default_wraps_primary_phone_as_a_formula_string(self):
        repo = FakeRepo(suppliers={5: {"id": 5, "canonical_name": "Acme Co", "primary_phone": "+865462883156"}})
        rows = [{
            "row_index": 0, "original_columns": {"Company Name": "Acme Co"},
            "status": "success", "company_name": "Acme Co", "name_source": "csv",
            "supplier_id": 5, "error_message": None,
        }]
        parsed = _read_csv(flatten_batch_results(rows, repo=repo))
        assert parsed[0]["primary_phone"] == '="+865462883156"'

    def test_excel_safe_phone_false_produces_a_plain_value(self):
        repo = FakeRepo(suppliers={5: {"id": 5, "canonical_name": "Acme Co", "primary_phone": "+865462883156"}})
        rows = [{
            "row_index": 0, "original_columns": {"Company Name": "Acme Co"},
            "status": "success", "company_name": "Acme Co", "name_source": "csv",
            "supplier_id": 5, "error_message": None,
        }]
        parsed = _read_csv(flatten_batch_results(rows, repo=repo, excel_safe_phone=False))
        assert parsed[0]["primary_phone"] == "+865462883156"

    def test_empty_primary_phone_is_not_wrapped(self):
        repo = FakeRepo(suppliers={5: {"id": 5, "canonical_name": "Acme Co"}})
        rows = [{
            "row_index": 0, "original_columns": {"Company Name": "Acme Co"},
            "status": "success", "company_name": "Acme Co", "name_source": "csv",
            "supplier_id": 5, "error_message": None,
        }]
        parsed = _read_csv(flatten_batch_results(rows, repo=repo))
        assert parsed[0]["primary_phone"] == ""

    def test_all_phones_column_is_never_wrapped(self):
        """all_phones mixes numbers with text labels/semicolons -- not
        pure-numeric, so Excel isn't misreading it as a number today;
        wrapping it would be pointless and would obscure the labels."""
        repo = FakeRepo(
            suppliers={5: {"id": 5, "canonical_name": "Acme Co"}},
            phone_numbers={5: [{"phone_number": "+8613800001111", "phone_type": "mobile"}]},
        )
        rows = [{
            "row_index": 0, "original_columns": {"Company Name": "Acme Co"},
            "status": "success", "company_name": "Acme Co", "name_source": "csv",
            "supplier_id": 5, "error_message": None,
        }]
        parsed = _read_csv(flatten_batch_results(rows, repo=repo))
        assert parsed[0]["all_phones"] == "+8613800001111 (mobile)"


class TestFailedRows:

    def test_error_message_is_included(self):
        rows = [{
            "row_index": 0, "original_columns": {"Website": "dead-domain.example"},
            "status": "failed", "company_name": None, "name_source": "inferred_from_domain",
            "supplier_id": 3, "error_message": "could not fetch homepage",
        }]
        csv_text = flatten_batch_results(rows, repo=FakeRepo())
        parsed = _read_csv(csv_text)
        assert parsed[0]["status"] == "failed"
        assert parsed[0]["error_message"] == "could not fetch homepage"


class TestNameExtractionNote:

    def test_rejection_reason_is_included(self):
        rows = [{
            "row_index": 0, "original_columns": {"Website": "cgpsealing.com"},
            "status": "success", "company_name": None, "name_source": "inferred_from_domain",
            "supplier_id": 402, "error_message": None,
            "name_extraction_note": "rejected: extracted name 'nginx' matches a known "
                                     "server-default/placeholder page name",
        }]
        csv_text = flatten_batch_results(rows, repo=FakeRepo(suppliers={402: {"id": 402, "canonical_name": "Cgpsealing"}}))
        parsed = _read_csv(csv_text)
        assert "rejected" in parsed[0]["name_extraction_note"]
        assert "nginx" in parsed[0]["name_extraction_note"]

    def test_absent_when_extraction_was_not_attempted_or_applied_cleanly(self):
        rows = [{
            "row_index": 0, "original_columns": {"Company Name": "Acme Co"},
            "status": "success", "company_name": "Acme Co", "name_source": "csv",
            "supplier_id": None, "error_message": None,
        }]
        csv_text = flatten_batch_results(rows, repo=FakeRepo())
        parsed = _read_csv(csv_text)
        assert parsed[0]["name_extraction_note"] == ""


class TestAddressColumns:

    def test_address_pulled_from_live_supplier_record(self):
        repo = FakeRepo(suppliers={
            5: {"id": 5, "canonical_name": "Acme Co", "address": "1 Main St, Springfield, IL"},
        })
        rows = [{
            "row_index": 0, "original_columns": {"Company Name": "Acme Co"},
            "status": "success", "company_name": "Acme Co", "name_source": "csv",
            "supplier_id": 5, "error_message": None,
        }]
        csv_text = flatten_batch_results(rows, repo=repo)
        parsed = _read_csv(csv_text)
        assert parsed[0]["address"] == "1 Main St, Springfield, IL"

    def test_address_candidate_shown_when_guard_blocked_the_write(self):
        """The whole point of this column: a supplier that already had
        a trusted address (e.g. from a bulk import) still needs to show
        what extraction found, even though `address` itself is
        unchanged -- otherwise there's no way to tell whether
        extraction worked at all."""
        repo = FakeRepo(
            suppliers={402: {"id": 402, "canonical_name": "CGP (Wuhu) Sealing Co., Ltd.",
                              "address": "Trusted bulk-import address, China"}},
            field_provenance={402: [
                {"field_name": "address_candidate", "value": "Extracted candidate address, China"},
            ]},
        )
        rows = [{
            "row_index": 0, "original_columns": {"Website": "cgpsealing.com"},
            "status": "success", "company_name": "CGP (Wuhu) Sealing Co., Ltd.", "name_source": "inferred_from_domain",
            "supplier_id": 402, "error_message": None,
        }]
        csv_text = flatten_batch_results(rows, repo=repo)
        parsed = _read_csv(csv_text)
        assert parsed[0]["address"] == "Trusted bulk-import address, China"
        assert parsed[0]["address_candidate"] == "Extracted candidate address, China"

    def test_address_candidate_blank_when_no_conflict_occurred(self):
        repo = FakeRepo(suppliers={5: {"id": 5, "canonical_name": "Acme Co", "address": "1 Main St"}})
        rows = [{
            "row_index": 0, "original_columns": {"Company Name": "Acme Co"},
            "status": "success", "company_name": "Acme Co", "name_source": "csv",
            "supplier_id": 5, "error_message": None,
        }]
        csv_text = flatten_batch_results(rows, repo=repo)
        parsed = _read_csv(csv_text)
        assert parsed[0]["address_candidate"] == ""

    def test_address_candidate_takes_the_most_recent_entry(self):
        repo = FakeRepo(
            suppliers={5: {"id": 5, "canonical_name": "Acme Co", "address": "Trusted address"}},
            field_provenance={5: [
                {"field_name": "address_candidate", "value": "older candidate"},
                {"field_name": "address_candidate", "value": "newer candidate"},
            ]},
        )
        rows = [{
            "row_index": 0, "original_columns": {"Company Name": "Acme Co"},
            "status": "success", "company_name": "Acme Co", "name_source": "csv",
            "supplier_id": 5, "error_message": None,
        }]
        csv_text = flatten_batch_results(rows, repo=repo)
        parsed = _read_csv(csv_text)
        assert parsed[0]["address_candidate"] == "newer candidate"


class TestSpecialCharactersEscaped:

    def test_commas_and_quotes_in_original_data_round_trip_correctly(self):
        rows = [{
            "row_index": 0, "original_columns": {"Company Name": 'Acme, "The Best" Co'},
            "status": "success", "company_name": 'Acme, "The Best" Co', "name_source": "csv",
            "supplier_id": None, "error_message": None,
        }]
        csv_text = flatten_batch_results(rows, repo=FakeRepo())
        parsed = _read_csv(csv_text)
        assert parsed[0]["Company Name"] == 'Acme, "The Best" Co'
