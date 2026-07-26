"""
tests/test_trust_signals.py

Tests for verification.trust_signals. The E-mark tests deliberately
include a case proving the honest limitation stated in the module
docstring: a well-formed but fabricated number passes format checking,
because format checking is all this module can actually do.
"""

from __future__ import annotations

from verification.trust_signals import (
    check_emark_format,
    check_emark_numbers,
    check_phone_numbers,
    check_phone_validity,
)


class TestPhoneValidity:

    def test_valid_chinese_number_with_country_code_is_plausible(self):
        result = check_phone_validity("+8657487654321")
        assert result.plausible is True

    def test_garbage_number_is_not_plausible(self):
        result = check_phone_validity("+861234")
        assert result.plausible is False

    def test_empty_string_is_not_plausible(self):
        result = check_phone_validity("")
        assert result.plausible is False
        assert result.reason == "empty"

    def test_unparseable_text_does_not_raise(self):
        result = check_phone_validity("not a phone number at all")
        assert result.plausible is False

    def test_region_hint_allows_a_locally_formatted_number(self):
        result = check_phone_validity("0574 8765 4321", default_region="CN")
        assert result.plausible is True

    def test_bulk_check_preserves_order(self):
        results = check_phone_numbers(["+8657487654321", "", "+861234"])
        assert [r.plausible for r in results] == [True, False, False]


class TestEmarkFormat:

    def test_well_formed_number_with_regulation_is_format_plausible(self):
        result = check_emark_format("e1 10R-05 12345")
        assert result.format_plausible is True

    def test_bare_country_code_only_is_format_plausible(self):
        # Some suppliers report just the country-code form ("e4",
        # "E11") without the full regulation/approval suffix -- still
        # a plausible shape, not a red flag on its own.
        result = check_emark_format("e4")
        assert result.format_plausible is True

    def test_marketing_text_with_no_number_is_not_plausible(self):
        result = check_emark_format("E-MARK CERTIFIED!!!")
        assert result.format_plausible is False

    def test_empty_value_is_not_plausible(self):
        result = check_emark_format("")
        assert result.format_plausible is False

    def test_random_text_is_not_plausible(self):
        result = check_emark_format("yes we have certification")
        assert result.format_plausible is False


class TestEmarkFormatHonestLimitation:
    """The specific thing the module docstring promises: format
    checking cannot and does not catch a well-formed but fabricated
    number. This test exists to keep that honesty claim true rather
    than aspirational."""

    def test_a_plausible_but_entirely_made_up_number_still_passes(self):
        # e99 is not a real UNECE contracting-party country code as of
        # writing, but this module makes no claim to know the current
        # valid code list -- only the shape. A fabricated-but-
        # well-shaped number passing is the expected, disclosed
        # behaviour, not a bug.
        result = check_emark_format("e99 10R-05 99999")
        assert result.format_plausible is True
        assert "not verified against any registry" in result.reason


class TestBulkEmarkCheck:

    def test_bulk_check_preserves_order_and_mixed_results(self):
        results = check_emark_numbers(["e1 10R-05 12345", "not a real one", "e4"])
        assert [r.format_plausible for r in results] == [True, False, True]
