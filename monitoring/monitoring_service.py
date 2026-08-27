"""
monitoring/monitoring_service.py

Opt-in, cadence-based re-checking of a small set of low-cost supplier
signals, storing every observation (not just deltas) in
supplier_snapshots so a future check can diff against the past --
"X was true on date A, Y is true on date B", always a neutral fact,
never a system-assigned severity (same discipline as CLAUDE.md standing
rule 2, extended from "never write a verdict into tracker columns" to
"never write a verdict into a diff string either").

Same single/batch/semaphore/wall-clock-budget shape as
collection/collection_service.py's CollectionService and
verification/contact_finder_service.py's ContactFinderService, its
direct templates -- but the idempotency marker is different by design:
those services use a `*_at IS NULL` marker (skip forever once
attempted, since the work is one-shot). Monitoring is recurring, so the
marker here is `next_check_due_at <= now` (skip until due again, not
skip forever) -- see capture_snapshot_pending.

v1 tracked fields, and why each is genuinely free
-----------------------------------------------------
- certifications_claimed -- read from repo.get_capabilities(), already
  on file. No new fetch.
- primary_email / primary_phone -- read straight off the current
  suppliers row. No new fetch.
- companies_house_status -- read straight off the current
  suppliers.companies_house_status column, NOT a fresh Companies House
  API call. Re-querying would mean re-running the full
  verification/uk_company_verification_service.py match logic, a
  heavier, CLI-only/manual operation per CLAUDE.md standing rule 9 --
  out of scope here. This signal only moves when someone re-runs
  `main.py verify-uk-company` manually; a monitoring check snapshots
  whatever is already on file, it does not itself re-verify.
- website_reachability -- a real fetch, classified via
  verification.website_reachability.classify_website_reachability
  ("live"/"blocked"/"dead"). A plain unauthenticated GET, no paid API.

STANDING GUARD, read this before adding a new tracked field
-----------------------------------------------------------------
Every field above costs $0 -- this is why enable_monitoring's cost
disclosure (shown by the CLI/API caller BEFORE opt-in, not by this
module) can honestly say "no paid API calls," and why
capture_snapshot_pending can run unattended on a schedule with no
per-run human confirmation. If a future field is added that costs real
money (re-running reverse-image search, catalogue-depth extraction,
etc.), it must NOT be silently folded into this same free, no-
confirmation auto-run set -- it needs its own explicit opt-in/
confirmation step at the point it's added, the same as every other
paid stage in this codebase (see CLAUDE.md standing rule 5). This is a
documentation/discipline guard, not something a type system enforces --
so it's repeated here, above VALID_SNAPSHOT_FIELDS, and in
capture_snapshot's own docstring, the two places a future editor is
most likely to read before adding a field.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from config.settings import MONITORING_JOB_MAX_SECONDS, MONITORING_MAX_CONCURRENT_JOBS
from storage.repository import SupplierRepository
from verification.capability_vocabulary import CATEGORY_STANDARD
from verification.website_reachability import classify_website_reachability

logger = logging.getLogger(__name__)

# See this module's own "STANDING GUARD" docstring section above before
# adding a new entry here -- every field in this tuple is assumed free
# (no paid API call) by capture_snapshot_pending's unattended,
# no-confirmation auto-run.
VALID_SNAPSHOT_FIELDS = (
    "certifications_claimed",
    "primary_email",
    "primary_phone",
    "companies_house_status",
    "website_reachability",
)

_LIST_SHAPED_FIELDS = frozenset({"certifications_claimed"})

_FIELD_LABELS = {
    "certifications_claimed": "Certifications claimed",
    "primary_email": "Primary email",
    "primary_phone": "Primary phone",
    "companies_house_status": "Companies House status",
    "website_reachability": "Website reachability",
}

_CADENCE_INTERVALS = {
    "monthly": timedelta(days=30),
    "quarterly": timedelta(days=90),
}

# The real, current, honest cost of a v1 check -- shown by the CLI/API
# at opt-in time (see this module's own docstring for why opt-in, not
# per-run, is the disclosure point for these specific free fields).
MONITORING_COST_DISCLOSURE = (
    "$0 per check -- certifications/contact fields are read from data "
    "already on file, Companies House status is read from the last "
    "manual verify-uk-company run (not re-queried), and website "
    "reachability is one free, unauthenticated HTTP fetch. No paid API "
    "calls."
)

# Shared across MonitoringService instances within one process -- same
# reasoning as ContactFinderService's own _BATCH_SEMAPHORE.
_BATCH_SEMAPHORE = threading.Semaphore(MONITORING_MAX_CONCURRENT_JOBS)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _next_due_at(cadence: str) -> str:
    interval = _CADENCE_INTERVALS[cadence]
    return (datetime.now(timezone.utc) + interval).strftime("%Y-%m-%d %H:%M:%S")


def _normalise_for_diff(field_name: str, value: Optional[str]) -> Any:
    """List-shaped fields are compared as sets (order-independent) --
    everything else compares as the plain stored string. Guards
    against a false "changed" diff purely from JSON key/list ordering,
    even though capture_snapshot already writes list fields sorted."""
    if field_name in _LIST_SHAPED_FIELDS:
        if not value:
            return frozenset()
        try:
            return frozenset(json.loads(value))
        except (json.JSONDecodeError, TypeError):
            return value
    return value


def _display_value(field_name: str, value: Optional[str]) -> str:
    if field_name in _LIST_SHAPED_FIELDS and value:
        try:
            items = json.loads(value)
            return ", ".join(items) if items else "(none)"
        except (json.JSONDecodeError, TypeError):
            return value
    return value if value else "(none)"


def _date_only(captured_at: Optional[str]) -> str:
    return (captured_at or "")[:10]


class MonitoringService:

    def __init__(
        self,
        repo: Optional[SupplierRepository] = None,
        reachability_classifier: Optional[Any] = None,
        job_max_seconds: int = MONITORING_JOB_MAX_SECONDS,
    ):
        self.repo = repo or SupplierRepository()
        self.classify_reachability = reachability_classifier or classify_website_reachability
        self.job_max_seconds = job_max_seconds

    def enable_monitoring(self, supplier_id: int, cadence: str) -> Dict[str, Any]:
        """Opt a supplier into recurring monitoring. `cadence` must be
        'monthly' or 'quarterly'. The caller (CLI/API) is responsible
        for showing MONITORING_COST_DISCLOSURE to the buyer BEFORE
        calling this -- this method does not itself gate on
        confirmation, since it's the single opt-in action, not a
        recurring run (see module docstring)."""
        if cadence not in _CADENCE_INTERVALS:
            raise ValueError(f"cadence must be one of {sorted(_CADENCE_INTERVALS)}, got {cadence!r}")
        if self.repo.get_supplier(supplier_id) is None:
            raise ValueError(f"No supplier with id={supplier_id}")

        next_due_at = _next_due_at(cadence)
        self.repo.upsert_monitoring_settings(
            supplier_id=supplier_id, cadence=cadence, next_check_due_at=next_due_at,
        )
        return {"supplier_id": supplier_id, "cadence": cadence, "next_check_due_at": next_due_at}

    def disable_monitoring(self, supplier_id: int) -> None:
        self.repo.delete_monitoring_settings(supplier_id)

    def capture_snapshot(self, supplier_id: int) -> Dict[str, Optional[str]]:
        """The single-supplier, always-runs primitive -- writes one
        supplier_snapshots row per VALID_SNAPSHOT_FIELDS entry, always,
        even when the value is unchanged or absent (None is a real,
        storable observation: "checked, nothing on file"). See this
        module's own STANDING GUARD docstring section before adding a
        new field here -- every field captured today is free."""
        supplier = self.repo.get_supplier(supplier_id)
        if supplier is None:
            raise ValueError(f"No supplier with id={supplier_id}")

        values: Dict[str, Optional[str]] = {}

        certifications = sorted({
            cap.get("canonical_term") or cap.get("reported_term")
            for cap in self.repo.get_capabilities(supplier_id)
            if cap.get("category") == CATEGORY_STANDARD and (cap.get("canonical_term") or cap.get("reported_term"))
        })
        values["certifications_claimed"] = json.dumps(certifications) if certifications else None

        values["primary_email"] = supplier.get("primary_email") or None
        values["primary_phone"] = supplier.get("primary_phone") or None
        values["companies_house_status"] = supplier.get("companies_house_status") or None

        domain = supplier.get("domain")
        if domain:
            values["website_reachability"] = self.classify_reachability(f"https://{domain}")
        else:
            values["website_reachability"] = None

        for field_name in VALID_SNAPSHOT_FIELDS:
            self.repo.save_snapshot(supplier_id=supplier_id, field_name=field_name, value=values[field_name])

        return values

    def capture_snapshot_pending(self, limit: Optional[int] = None) -> Dict[str, Any]:
        """Batch pass across every supplier currently due
        (next_check_due_at <= now) -- mirrors ContactFinderService.
        find_contacts_pending() exactly, semaphore + wall-clock budget
        included. Reports scope (how many suppliers, real HTTP fetch
        count) in the returned dict for the caller to log/print before
        or after the run -- this method itself doesn't block on
        confirmation, since it's meant to run unattended on a schedule
        (see module docstring's Trigger/disclosure section)."""
        acquired = _BATCH_SEMAPHORE.acquire(blocking=False)
        if not acquired:
            return {
                "attempted": 0, "total_due": 0, "status": "skipped",
                "reason": "another monitoring batch is already running on this instance",
            }

        try:
            due = self.repo.get_suppliers_due_for_monitoring(limit=limit)
            start_time = time.monotonic()
            attempted = 0
            stopped_early = False

            for settings in due:
                if time.monotonic() - start_time > self.job_max_seconds:
                    stopped_early = True
                    break
                supplier_id = settings["supplier_id"]
                self.capture_snapshot(supplier_id)
                self.repo.upsert_monitoring_settings(
                    supplier_id=supplier_id, cadence=settings["cadence"],
                    next_check_due_at=_next_due_at(settings["cadence"]), last_checked_at=_now_iso(),
                )
                attempted += 1

            return {
                "attempted": attempted, "total_due": len(due),
                "status": "partial" if stopped_early else "completed",
                "paid_api_calls": 0,
                "free_http_fetches": attempted,  # one website_reachability fetch per supplier checked
            }
        finally:
            _BATCH_SEMAPHORE.release()

    def diff_field(self, supplier_id: int, field_name: str) -> Optional[str]:
        """Compares the last two supplier_snapshots rows for this
        field. Returns None when there are fewer than 2 observations,
        or when the two are equal after field-appropriate
        normalisation. Otherwise returns a plain, neutral fact string
        -- no severity word (no "warning"/"flag"/"risk"). The buyer
        assigns severity, not this codebase (CLAUDE.md standing rule
        2, extended the same way to diff output)."""
        if field_name not in VALID_SNAPSHOT_FIELDS:
            raise ValueError(f"field_name must be one of {VALID_SNAPSHOT_FIELDS}, got {field_name!r}")

        snapshots = self.repo.get_snapshots(supplier_id, field_name=field_name)
        if len(snapshots) < 2:
            return None

        old, new = snapshots[-2], snapshots[-1]
        if _normalise_for_diff(field_name, old.get("value")) == _normalise_for_diff(field_name, new.get("value")):
            return None

        label = _FIELD_LABELS.get(field_name, field_name)
        old_display = _display_value(field_name, old.get("value"))
        new_display = _display_value(field_name, new.get("value"))
        old_date = _date_only(old.get("captured_at"))
        new_date = _date_only(new.get("captured_at"))
        return f"{label} was '{old_display}' on {old_date}, is '{new_display}' on {new_date}"

    def diff_all_fields(self, supplier_id: int) -> List[str]:
        """Convenience wrapper: diff_field for every tracked field,
        only the ones that actually produced a real diff (never a
        placeholder "no change" entry)."""
        diffs = []
        for field_name in VALID_SNAPSHOT_FIELDS:
            diff = self.diff_field(supplier_id, field_name)
            if diff:
                diffs.append(diff)
        return diffs
