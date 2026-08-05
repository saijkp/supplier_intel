"""
verification/linkedin_presence.py

Confirms whether a supplier has a findable LinkedIn company page,
without ever touching LinkedIn's own site directly.

Why this doesn't scrape LinkedIn
------------------------------------
Direct scraping of LinkedIn pages violates their Terms of Service and
is aggressively technically blocked besides. This module instead
reuses `scrapers.google_search_scraper.GoogleSearchScraper` (already
in this codebase, already paid for elsewhere in the pipeline) with
`site_filter="linkedin.com"` — the same mechanism as typing
`site:linkedin.com company name` into Google. That reads only what
Google has already indexed and made publicly available as a search
result; it never requests a page from linkedin.com, logs in, or
extracts anything LinkedIn hasn't already chosen to expose to a search
engine crawler.

What this can and can't tell you
-------------------------------------
A hit confirms a LinkedIn company page exists and is findable — real
signal that a company has *some* public professional presence, which a
pure trading-company shell account is less likely to maintain
convincingly. It is not a strong manufacturer-vs-trader signal on its
own (plenty of legitimate trading companies have LinkedIn pages too,
and plenty of small but genuine factories don't bother with one) —
treat `presence_confirmed=True` as one weak-to-moderate corroborating
fact, not a verdict. The snippet text is opportunistically read for an
employee-count or follower-count mention when Google's own result
happens to surface one, but this is not reliable enough to report as a
count with any confidence — it's included as free-text context only.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Optional


@dataclasses.dataclass(frozen=True)
class LinkedInPresenceResult:
    company_name: str
    presence_confirmed: bool
    linkedin_url: Optional[str]
    snippet: Optional[str]
    reason: str
    source: str = "linkedin_search"  # 'linkedin_search' (a real search completed) | 'unavailable'


class LinkedInPresenceChecker:
    """`google_scraper` is injectable, matching every other module in
    this codebase built on `GoogleSearchScraper` -- a fake with a
    `.scrape(query, site_filter=...)` method is enough for tests, no
    network required.
    """

    def __init__(self, google_scraper: Any):
        self.google_scraper = google_scraper

    def check(self, company_name: str) -> LinkedInPresenceResult:
        if not company_name or not company_name.strip():
            return LinkedInPresenceResult(
                company_name=company_name, presence_confirmed=False,
                linkedin_url=None, snippet=None, reason="no company name provided",
            )

        try:
            results = self.google_scraper.scrape(
                f'"{company_name}"', max_results=5, site_filter="linkedin.com"
            )
        except Exception as e:
            return LinkedInPresenceResult(
                company_name=company_name, presence_confirmed=False, source="unavailable",
                linkedin_url=None, snippet=None, reason=f"search failed: {e}",
            )

        # GoogleSearchScraper.scrape() doesn't raise for an ordinary
        # failure (e.g. SERPAPI_KEY not configured, quota exhausted) --
        # it returns [error_result(...)] instead (see scrapers/
        # base_scraper.py's own convention), which is a NON-EMPTY list
        # with success=False, distinct from a genuinely empty list
        # (a successful call that simply found zero organic results --
        # see GoogleSearchScraper.scrape's own contract). Real bug this
        # guards against: if every entry in a non-empty list failed, the
        # loop below would previously fall through to "no LinkedIn page
        # found" -- indistinguishable from a real negative even though
        # the search never actually ran.
        if not results:
            return LinkedInPresenceResult(
                company_name=company_name, presence_confirmed=False,
                linkedin_url=None, snippet=None, reason="no LinkedIn page found",
            )

        any_successful = False
        for result in results:
            if not getattr(result, "success", True):
                continue
            any_successful = True
            raw = result.raw_data or {}
            link = raw.get("link")
            if link and "linkedin.com" in link:
                return LinkedInPresenceResult(
                    company_name=company_name, presence_confirmed=True,
                    linkedin_url=link, snippet=raw.get("snippet"),
                    reason="LinkedIn page found via search",
                )

        if not any_successful:
            first_error = next(
                (getattr(r, "error", None) for r in results if not getattr(r, "success", True)), None,
            )
            return LinkedInPresenceResult(
                company_name=company_name, presence_confirmed=False, source="unavailable",
                linkedin_url=None, snippet=None,
                reason=first_error or "LinkedIn search could not be completed",
            )

        return LinkedInPresenceResult(
            company_name=company_name, presence_confirmed=False,
            linkedin_url=None, snippet=None, reason="no LinkedIn page found",
        )
