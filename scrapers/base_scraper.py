"""
scrapers/base_scraper.py

Abstract base class for all data source scrapers, plus the shared
ScraperResult contract every concrete scraper returns. Keeping this
contract source-agnostic is what lets pipeline/orchestrator.py (Phase 3)
loop over scrapers without knowing anything about Alibaba vs ImportYeti
vs HKTDC internals.
"""

from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from config.settings import REQUEST_DELAYS

logger = logging.getLogger(__name__)


@dataclass
class ScraperResult:
    """Uniform output type for every scraper. `raw_data` is passed
    through to storage.repository.save_raw() completely untouched —
    normalisation happens later, never inside the scraper."""

    source: str
    source_id: str
    raw_data: Dict[str, Any]
    success: bool
    error: Optional[str] = None


class BaseScraper(ABC):
    """
    Every concrete scraper (AlibabaScraper, ImportYetiScraper, HKTDCScraper,
    etc.) subclasses this and implements `scrape()`.

    `enable_delays` exists so tests can disable the polite random sleeps —
    production code should always leave it True.
    """

    def __init__(self, source_name: str, enable_delays: bool = True):
        self.source_name = source_name
        self.enable_delays = enable_delays
        self.results: List[ScraperResult] = []

    @abstractmethod
    def scrape(self, query: str, **kwargs) -> List[ScraperResult]:
        """Run a search for `query` against this source and return a
        list of ScraperResult. Must never raise for ordinary failures
        (network errors, empty results, rate limiting) — return an
        error ScraperResult (see `error_result`) instead, so one bad
        source doesn't kill a multi-source pipeline run."""
        ...

    def _polite_delay(self, min_sec: Optional[float] = None, max_sec: Optional[float] = None) -> None:
        """Randomised delay to avoid rate limiting / bot detection.
        Falls back to this source's entry in config.settings.REQUEST_DELAYS
        when bounds aren't given explicitly. No-ops when enable_delays
        is False (test mode)."""
        if not self.enable_delays:
            return
        if min_sec is None or max_sec is None:
            default_min, default_max = REQUEST_DELAYS.get(self.source_name, (2.0, 5.0))
            min_sec = default_min if min_sec is None else min_sec
            max_sec = default_max if max_sec is None else max_sec
        delay = random.uniform(min_sec, max_sec)
        logger.debug("%s: waiting %.1fs before next request", self.source_name, delay)
        time.sleep(delay)

    def _safe_request(self, func: Callable, *args, max_retries: int = 3, **kwargs) -> Any:
        """Call `func(*args, **kwargs)` with exponential-backoff retries.
        Re-raises the last exception if every attempt fails, so callers
        can decide whether to return an error_result or abort."""
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 - intentionally broad, retried generically
                last_exc = e
                logger.warning(
                    "%s attempt %d/%d failed: %s",
                    self.source_name, attempt + 1, max_retries, e,
                )
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt * (5.0 if self.enable_delays else 0.0))
        assert last_exc is not None
        raise last_exc

    def error_result(self, error: str, source_id: str = "") -> ScraperResult:
        """Convenience constructor for a failed ScraperResult, used when
        a scrape attempt fails entirely (e.g. actor run errored, page
        request failed after retries)."""
        return ScraperResult(
            source=self.source_name,
            source_id=source_id,
            raw_data={},
            success=False,
            error=error,
        )
