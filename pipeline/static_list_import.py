"""
pipeline/static_list_import.py

Imports a static, already-in-hand list of suppliers (an exhibitor
export, a spreadsheet you've built by hand, anything that isn't a live
scrape) through the exact same pipeline a live scraper's results go
through: `save_raw` (nothing is ever lost to a downstream parsing
bug) -> normalise -> `SupplierMatcher.resolve_and_store` (the same
USCC -> domain -> fuzzy-name merge/review/create decision every other
source uses).

Why this goes through the real pipeline instead of a shortcut
------------------------------------------------------------------
It would be faster to just call `repo.create_golden_record()` directly
for each row. That was deliberately rejected: some of the 300
trailer-specific exhibitors in a real Automechanika export are very
likely already in the database from a live Alibaba/HKTDC search, or
will be found later by one. Bypassing the matcher would create a
second, duplicate record for the same real company instead of merging
into the one that already exists -- exactly the failure this whole
dedup layer exists to prevent. Going through `resolve_and_store`
means a static import and a live scrape are indistinguishable to
every downstream stage (capability extraction, scoring, search).

The two-piece split
------------------------
This module is the *generic* half: it takes already-normalised
candidate dicts (the same shape `create_golden_record` and
`resolve_and_store` already expect everywhere else in this codebase)
and doesn't know or care what file format or column layout they came
from. The *source-specific* half -- reading one particular
spreadsheet's actual columns and mapping them to that shape -- is a
`BaseNormalizer` subclass, the same pattern `AlibabaNormalizer` and
every other normalizer in this codebase already follows, and belongs
in its own file once the real column layout is known. Splitting it
this way means the reusable 90% doesn't have to be rewritten every
time a new static list with different columns shows up.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Dict, List, Optional

from deduplication.matcher import SupplierMatcher
from storage.repository import SupplierRepository

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class StaticImportStats:
    total: int = 0
    normalised: int = 0
    skipped_no_name: int = 0
    created: int = 0
    merged: int = 0
    review_queued: int = 0
    failed: int = 0

    def as_dict(self) -> Dict[str, int]:
        return dataclasses.asdict(self)


def import_static_supplier_list(
    repo: SupplierRepository,
    matcher: SupplierMatcher,
    raw_records: List[Dict[str, Any]],
    *,
    source_label: str,
    normaliser: Optional[Any] = None,
) -> StaticImportStats:
    """Import a static list of supplier records.

    `raw_records` is a list of plain dicts -- one per row of whatever
    file this came from, in that file's own original column names,
    unmodified. Each one is saved to `raw_source_data` first (so the
    original export is never lost even if a normaliser has a bug),
    tagged with `source_label` (e.g. `"automechanika_2026"`) so it's
    always traceable back to which list it came from.

    `normaliser`, if given, must have a `.normalise(raw_data) ->
    dict` method (the same interface every `BaseNormalizer` subclass
    in this codebase already implements) mapping one raw row to the
    candidate shape `resolve_and_store` expects (`canonical_name` at
    minimum; `country`, `domain`, `product_keywords`, etc. wherever
    the source data actually has them). If omitted, `raw_records` are
    assumed to already be in that normalised shape -- useful for
    testing this module's own dedup-routing logic independent of any
    particular file's column layout (see this module's own test
    suite), and for a caller that's already done its own mapping.

    A row with no resolvable `canonical_name` is skipped and counted,
    never silently dropped without a trace -- `raw_source_data` still
    has it, marked `failed`, with the reason recorded.
    """
    stats = StaticImportStats(total=len(raw_records))

    for raw_data in raw_records:
        raw_id = repo.save_raw(source=source_label, raw_data=raw_data)

        try:
            candidate = normaliser.normalise(raw_data) if normaliser else dict(raw_data)
        except Exception as e:
            logger.error("Static import: normalisation failed for raw_id=%s: %s", raw_id, e)
            repo.mark_raw_processed(raw_id, status="failed", error_message=str(e))
            stats.failed += 1
            continue

        if not candidate.get("canonical_name"):
            repo.mark_raw_processed(raw_id, status="failed", error_message="missing canonical_name")
            stats.skipped_no_name += 1
            continue

        stats.normalised += 1

        try:
            resolution = matcher.resolve_and_store(candidate)
        except Exception as e:
            logger.error("Static import: dedup/store failed for raw_id=%s: %s", raw_id, e)
            repo.mark_raw_processed(raw_id, status="failed", error_message=str(e))
            stats.failed += 1
            continue

        action = resolution["action"]
        if action == "created":
            stats.created += 1
        elif action == "merged":
            stats.merged += 1
        elif action == "review_queued":
            stats.review_queued += 1

        resolved_id = resolution.get("supplier_id") or resolution.get("new_supplier_id")
        repo.mark_raw_processed(raw_id, golden_record_id=resolved_id, status="processed")

    logger.info(
        "Static import '%s' complete: %d total, %d created, %d merged, %d queued for review, %d failed",
        source_label, stats.total, stats.created, stats.merged, stats.review_queued, stats.failed,
    )
    return stats
