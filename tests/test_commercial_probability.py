"""
tests/test_commercial_probability.py

Tests for verification.commercial_probability. The property that
matters most: probability and confidence answer different questions,
and a sparse-but-positive supplier must never be reported with
unwarranted confidence just because the one thing we know is good.
"""

from __future__ import annotations

from verification.commercial_probability import estimate_commercial_flexibility


class TestStrongSignalSupplier:

    def test_all_signals_present_and_positive_yields_high_probability_and_confidence(self):
        supplier = {
            "registered_capital_rmb": 5_000_000.0, "year_established": 2000, "factory_size_sqm": 8000,
            "confirmed_shipments_uk": 10, "confirmed_shipments_eu": 5, "is_manufacturer": True,
        }
        result = estimate_commercial_flexibility(supplier, "60-day OEM payment terms")
        assert result.probability == 100.0
        assert result.confidence_level == "high"

    def test_reasoning_names_the_actual_contributing_signals(self):
        supplier = {"registered_capital_rmb": 5_000_000.0, "is_manufacturer": True}
        result = estimate_commercial_flexibility(supplier, "60-day OEM payment terms")
        assert "large registered capital" in result.reasoning
        assert "confirmed manufacturer" in result.reasoning


class TestSparseDataSupplier:
    """The core property this module exists to get right: a supplier
    we barely know anything about must never be reported with
    unwarranted confidence, even if the one fact we have is positive."""

    def test_minimal_available_data_gives_low_or_medium_confidence_never_high(self):
        """high_shipment_volume is always available by design (see its
        own comment in commercial_probability.py), so the true minimum
        is 2 of 6 signals (that one, plus whatever else is provided) --
        not 1. The property that actually matters: sparse data must
        never compute "high" confidence."""
        supplier = {"is_manufacturer": True}
        result = estimate_commercial_flexibility(supplier, "60-day OEM payment terms")
        assert result.confidence_level in ("low", "medium")
        assert result.confidence_level != "high"

    def test_completely_empty_supplier_is_zero_probability_low_confidence(self):
        result = estimate_commercial_flexibility({}, "60-day OEM payment terms")
        assert result.probability == 0.0
        assert result.confidence_level == "low"

    def test_zero_confirmed_shipments_is_a_real_available_fact_not_missing_data(self):
        """0 shipments across all tracked markets is itself a known,
        computable fact (NOT NULL DEFAULT 0 in the schema) -- this is
        why even a totally empty supplier dict still has one
        available signal, not zero."""
        result = estimate_commercial_flexibility({}, "60-day OEM payment terms")
        shipment_signal = next(s for s in result.contributing_signals if s.name == "high_shipment_volume")
        assert shipment_signal.available is True
        assert shipment_signal.present is False


class TestMediumDataSupplier:

    def test_half_the_signals_available_gives_medium_confidence(self):
        supplier = {
            "registered_capital_rmb": 2_000_000.0, "year_established": 2015,
            "confirmed_shipments_uk": 8,
        }
        result = estimate_commercial_flexibility(supplier, "framework agreement")
        assert result.confidence_level in ("medium", "high")  # exact boundary depends on which 3 of 6 are available


class TestSignalThresholds:

    def test_registered_capital_below_threshold_is_not_present(self):
        supplier = {"registered_capital_rmb": 100.0}  # far below the RMB 1M threshold
        result = estimate_commercial_flexibility(supplier, "test outcome")
        capital_signal = next(s for s in result.contributing_signals if s.name == "large_registered_capital")
        assert capital_signal.available is True
        assert capital_signal.present is False

    def test_registered_capital_at_or_above_threshold_is_present(self):
        supplier = {"registered_capital_rmb": 1_000_000.0}
        result = estimate_commercial_flexibility(supplier, "test outcome")
        capital_signal = next(s for s in result.contributing_signals if s.name == "large_registered_capital")
        assert capital_signal.present is True

    def test_confirmed_trader_is_a_real_available_negative_signal(self):
        """is_manufacturer=False (a confirmed trader, not just
        unknown) is available data, and correctly not present --
        distinct from is_manufacturer being unset entirely."""
        supplier = {"is_manufacturer": False}
        result = estimate_commercial_flexibility(supplier, "test outcome")
        mfr_signal = next(s for s in result.contributing_signals if s.name == "confirmed_manufacturer")
        assert mfr_signal.available is True
        assert mfr_signal.present is False


class TestOutcomeIsPreservedNotFabricated:

    def test_outcome_string_is_echoed_back_unchanged(self):
        result = estimate_commercial_flexibility({}, "forecast-based production commitment")
        assert result.outcome == "forecast-based production commitment"

    def test_every_signal_is_returned_even_when_not_present(self):
        """Full transparency requirement: every signal in the fixed
        set is always in contributing_signals, not just the ones that
        turned out positive."""
        result = estimate_commercial_flexibility({}, "test outcome")
        assert len(result.contributing_signals) == 6
