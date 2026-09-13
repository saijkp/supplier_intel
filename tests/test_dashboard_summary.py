"""
tests/test_dashboard_summary.py

Tests for storage.repository.SupplierRepository.get_dashboard_summary --
the single aggregation query backing frontend/dashboard.html's Dashboard
page (GET /dashboard/summary). See that method's own docstring for the
design decisions being exercised here (the verified_high threshold,
ai_confidence_assessed_at as the "verification" signal, month/7-day/
weekly-bucket trend windows, and the space-vs-'T' date-boundary
comparison).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from storage.database import initialise_schema


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    initialise_schema(path)
    return path


@pytest.fixture()
def repo(db_path):
    from storage.repository import SupplierRepository
    return SupplierRepository(db_path=db_path)


def _make_supplier(repo, **overrides):
    name = overrides.get("canonical_name", "Acme Trailer Parts")
    data = {
        "canonical_name": name,
        "country": overrides.pop("country", "China"),
        "domain": overrides.pop("domain", None) or f"{name.lower().replace(' ', '-')}.example.com",
    }
    is_manufacturer = overrides.pop("is_manufacturer", "unset")
    composite_score = overrides.pop("composite_score", None)
    ai_confidence_score = overrides.pop("ai_confidence_score", None)
    supplier_id = repo.create_golden_record(data)
    if is_manufacturer != "unset":
        repo.update_supplier_fields(supplier_id, {"is_manufacturer": is_manufacturer})
    if composite_score is not None:
        repo.update_scores(supplier_id, {"composite_score": composite_score})
    if ai_confidence_score is not None:
        repo.update_supplier_fields(
            supplier_id,
            {"ai_confidence_score": ai_confidence_score,
             "ai_confidence_assessed_at": datetime.now(timezone.utc).isoformat()},
        )
    return supplier_id


def _backdate(repo, table, row_id, column, when):
    """Test-only helper (same shape as tests/test_ai_platform_repository.py's
    own _backdate): directly rewrites a timestamp column, bypassing the
    repository's own write paths, which always stamp 'now'."""
    conn = sqlite3.connect(str(repo.db_path))
    conn.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (when.isoformat(), row_id))
    conn.commit()
    conn.close()


def _days_ago(n):
    return datetime.now(timezone.utc) - timedelta(days=n)


def _mid_last_month():
    """A timestamp guaranteed to fall in the calendar month before the
    current one, regardless of what day of the current month the suite
    happens to run on -- a fixed `_days_ago(45)` would sometimes land
    two months back (e.g. run on the 13th: 45 days ago is in the month
    before last), which would silently zero out these tests' 'last
    month' bucket. Every month has at least 28 days, so going back 15
    days from day 1 of the current month always lands safely inside the
    previous one."""
    today = datetime.now(timezone.utc).date()
    first_of_this_month = datetime(today.year, today.month, 1, tzinfo=timezone.utc)
    return first_of_this_month - timedelta(days=15)


class TestEmptyDatabase:

    def test_returns_zeroed_shape_not_an_error(self, repo):
        summary = repo.get_dashboard_summary()

        assert summary["total_suppliers"] == 0
        assert summary["entity_mix"] == {"manufacturer": 0, "trading_company": 0, "unclear": 0}
        assert summary["avg_daily_verifications_7d"] == [0] * 7
        assert summary["avg_daily_verifications"] == 0
        assert summary["avg_daily_trend_pct"] == 0.0
        assert summary["new_suppliers_this_month"] == 0
        assert summary["new_suppliers_trend_pct"] == 0.0
        assert summary["new_suppliers_trend_30d"] == [0] * 7
        assert summary["verified_high_count"] == 0
        assert summary["verified_high_trend_pct"] == 0.0
        assert summary["pipeline_jobs_completed_this_month"] == 0
        assert summary["pipeline_jobs_trend_pct"] == 0.0
        assert summary["recent_verified_initials"] == []
        assert summary["recent_verified_extra"] == 0
        assert summary["avg_confidence_score"] == 0
        assert summary["avg_confidence_trend_pct"] == 0.0
        assert summary["confidence_trend_30d"] == [0] * 7
        assert summary["recent_suppliers"] == []

    def test_verified_high_goal_comes_from_the_config_constant(self, repo):
        from config.settings import DASHBOARD_VERIFIED_HIGH_GOAL

        assert repo.get_dashboard_summary()["verified_high_goal"] == DASHBOARD_VERIFIED_HIGH_GOAL


class TestEntityMix:

    def test_manufacturer_trader_and_unclear_are_bucketed_correctly(self, repo):
        _make_supplier(repo, canonical_name="Maker Co", is_manufacturer=True)
        _make_supplier(repo, canonical_name="Trader Co", is_manufacturer=False)
        _make_supplier(repo, canonical_name="Mystery Co")  # is_manufacturer left NULL

        summary = repo.get_dashboard_summary()

        assert summary["total_suppliers"] == 3
        assert summary["entity_mix"] == {"manufacturer": 1, "trading_company": 1, "unclear": 1}

    def test_flagged_suppliers_are_excluded_from_every_count(self, repo):
        """Same 'not a valid candidate, never resurfaces' rule
        search_suppliers/search_suppliers_full already enforce (CLAUDE.md
        standing rule 8) -- a flagged row must not inflate the dashboard
        either."""
        supplier_id = _make_supplier(repo, canonical_name="Excluded Co", is_manufacturer=True, composite_score=95)
        repo.update_supplier_fields(supplier_id, {"flagged": True})

        summary = repo.get_dashboard_summary()

        assert summary["total_suppliers"] == 0
        assert summary["entity_mix"] == {"manufacturer": 0, "trading_company": 0, "unclear": 0}
        assert summary["verified_high_count"] == 0


class TestVerifiedHigh:

    def test_counts_only_suppliers_at_or_above_the_threshold(self, repo):
        from config.settings import DASHBOARD_VERIFIED_HIGH_MIN_SCORE

        _make_supplier(repo, canonical_name="High Co", composite_score=DASHBOARD_VERIFIED_HIGH_MIN_SCORE)
        _make_supplier(repo, canonical_name="Just Under Co", composite_score=DASHBOARD_VERIFIED_HIGH_MIN_SCORE - 1)

        summary = repo.get_dashboard_summary()

        assert summary["verified_high_count"] == 1

    def test_trend_pct_is_zero_when_prior_month_had_no_verified_high_suppliers(self, repo):
        _make_supplier(repo, canonical_name="New High Co", composite_score=90)

        summary = repo.get_dashboard_summary()

        assert summary["verified_high_trend_pct"] == 0.0

    def test_trend_pct_compares_this_month_to_last_month(self, repo):
        last_month_id = _make_supplier(repo, canonical_name="Old High Co", composite_score=90)
        _backdate(repo, "suppliers", last_month_id, "last_updated", _mid_last_month())
        _make_supplier(repo, canonical_name="New High Co A", composite_score=90)
        _make_supplier(repo, canonical_name="New High Co B", composite_score=90)

        summary = repo.get_dashboard_summary()

        # 1 last month -> 2 this month == +100%
        assert summary["verified_high_trend_pct"] == 100.0


class TestAvgDailyVerifications:

    def test_verification_is_keyed_off_ai_confidence_assessed_at(self, repo):
        _make_supplier(repo, canonical_name="Verified Today", ai_confidence_score=80)
        _make_supplier(repo, canonical_name="Never Verified")

        summary = repo.get_dashboard_summary()

        assert sum(summary["avg_daily_verifications_7d"]) == 1
        assert summary["avg_daily_verifications_7d"][-1] == 1  # today is the last (most recent) bucket
        assert summary["avg_daily_verifications_7d"][0] == 0

    def test_verifications_outside_the_7_day_window_are_not_counted(self, repo):
        old_id = _make_supplier(repo, canonical_name="Verified Long Ago", ai_confidence_score=80)
        _backdate(repo, "suppliers", old_id, "ai_confidence_assessed_at", _days_ago(20))

        summary = repo.get_dashboard_summary()

        assert sum(summary["avg_daily_verifications_7d"]) == 0
        assert summary["avg_daily_verifications"] == 0

    def test_trend_pct_compares_last_7_days_to_the_7_days_before(self, repo):
        prev_id = _make_supplier(repo, canonical_name="Verified Last Week", ai_confidence_score=70)
        _backdate(repo, "suppliers", prev_id, "ai_confidence_assessed_at", _days_ago(10))
        _make_supplier(repo, canonical_name="Verified Recently A", ai_confidence_score=70)
        _make_supplier(repo, canonical_name="Verified Recently B", ai_confidence_score=70)

        summary = repo.get_dashboard_summary()

        # 1 in the prior 7-day window -> 2 in the last 7 days == +100%
        assert summary["avg_daily_trend_pct"] == 100.0


class TestNewSuppliers:

    def test_counts_suppliers_first_seen_this_calendar_month(self, repo):
        _make_supplier(repo, canonical_name="Fresh Co")
        old_id = _make_supplier(repo, canonical_name="Old Co")
        _backdate(repo, "suppliers", old_id, "first_seen", _days_ago(45))

        summary = repo.get_dashboard_summary()

        assert summary["new_suppliers_this_month"] == 1

    def test_trend_30d_has_seven_weekly_buckets_summing_to_the_real_total(self, repo):
        for offset in (2, 10, 20, 30, 40):
            supplier_id = _make_supplier(repo, canonical_name=f"Co {offset}")
            _backdate(repo, "suppliers", supplier_id, "first_seen", _days_ago(offset))

        summary = repo.get_dashboard_summary()

        assert len(summary["new_suppliers_trend_30d"]) == 7
        assert sum(summary["new_suppliers_trend_30d"]) == 5


class TestPipelineJobs:

    def test_only_completed_jobs_in_the_current_month_are_counted(self, repo):
        repo.create_pipeline_job(job_id="job-completed", query="wheel bearings", options={})
        repo.mark_pipeline_job_completed("job-completed", stats={})

        repo.create_pipeline_job(job_id="job-failed", query="wheel bearings", options={})
        repo.mark_pipeline_job_failed("job-failed", error="boom")

        repo.create_pipeline_job(job_id="job-running", query="wheel bearings", options={})
        repo.mark_pipeline_job_running("job-running")

        summary = repo.get_dashboard_summary()

        assert summary["pipeline_jobs_completed_this_month"] == 1

    def test_trend_pct_compares_this_month_to_last_month(self, repo):
        repo.create_pipeline_job(job_id="job-last-month", query="q", options={})
        repo.mark_pipeline_job_completed("job-last-month", stats={})
        _backdate(repo, "pipeline_jobs", "job-last-month", "completed_at", _mid_last_month())

        for jid in ("job-this-month-a", "job-this-month-b"):
            repo.create_pipeline_job(job_id=jid, query="q", options={})
            repo.mark_pipeline_job_completed(jid, stats={})

        summary = repo.get_dashboard_summary()

        assert summary["pipeline_jobs_completed_this_month"] == 2
        assert summary["pipeline_jobs_trend_pct"] == 100.0


class TestRecentlyVerified:

    def test_initials_and_extra_count(self, repo):
        for i in range(7):
            _make_supplier(repo, canonical_name=f"Verified Supplier {i}", ai_confidence_score=70)

        summary = repo.get_dashboard_summary()

        assert len(summary["recent_verified_initials"]) == 5
        assert summary["recent_verified_extra"] == 2  # 7 verified total, 5 shown

    def test_two_word_name_produces_two_letter_initials(self, repo):
        _make_supplier(repo, canonical_name="Meridian Precision", ai_confidence_score=70)

        summary = repo.get_dashboard_summary()

        assert summary["recent_verified_initials"] == ["MP"]

    def test_unverified_suppliers_never_appear_in_the_hero_row(self, repo):
        _make_supplier(repo, canonical_name="Never Verified Co")

        summary = repo.get_dashboard_summary()

        assert summary["recent_verified_initials"] == []
        assert summary["recent_verified_extra"] == 0


class TestAvgConfidenceScore:

    def test_averages_only_suppliers_with_a_score(self, repo):
        _make_supplier(repo, canonical_name="Scored A", ai_confidence_score=80)
        _make_supplier(repo, canonical_name="Scored B", ai_confidence_score=60)
        _make_supplier(repo, canonical_name="Unscored")

        summary = repo.get_dashboard_summary()

        assert summary["avg_confidence_score"] == 70

    def test_confidence_trend_30d_has_seven_buckets(self, repo):
        _make_supplier(repo, canonical_name="Scored", ai_confidence_score=80)

        summary = repo.get_dashboard_summary()

        assert len(summary["confidence_trend_30d"]) == 7
        assert summary["confidence_trend_30d"][-1] == 80


class TestRecentSuppliers:

    def test_returns_at_most_six_most_recently_updated_first(self, repo):
        ids = [_make_supplier(repo, canonical_name=f"Supplier {i}") for i in range(8)]
        # Space out last_updated so ordering is unambiguous (all fixtures
        # would otherwise share the same CURRENT_TIMESTAMP second).
        for i, supplier_id in enumerate(ids):
            _backdate(repo, "suppliers", supplier_id, "last_updated", _days_ago(len(ids) - i))

        summary = repo.get_dashboard_summary()

        assert len(summary["recent_suppliers"]) == 6
        assert summary["recent_suppliers"][0]["canonical_name"] == "Supplier 7"

    def test_recent_suppliers_are_raw_rows_with_supplier_fields(self, repo):
        _make_supplier(repo, canonical_name="Meridian Co", country="China", composite_score=88, is_manufacturer=True)

        summary = repo.get_dashboard_summary()

        row = summary["recent_suppliers"][0]
        assert row["canonical_name"] == "Meridian Co"
        assert row["country"] == "China"
        assert row["composite_score"] == 88
        assert row["is_manufacturer"] == 1  # raw SQLite value, same convention as list_suppliers/get_supplier

    def test_flagged_suppliers_never_appear_in_recent_suppliers(self, repo):
        supplier_id = _make_supplier(repo, canonical_name="Flagged Co")
        repo.update_supplier_fields(supplier_id, {"flagged": True})

        summary = repo.get_dashboard_summary()

        assert summary["recent_suppliers"] == []
