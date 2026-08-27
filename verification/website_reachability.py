"""
verification/website_reachability.py

Classifies a URL as "live", "blocked", or "dead" via a real, lightweight
HTTP GET. Originally built inside discovery/fabtech_exhibitor_import.py
(see that module's own docstring for the full real-data investigation
behind this), promoted here once monitoring/monitoring_service.py
needed the same reachability check for arbitrary supplier websites, not
just FABTECH exhibitors -- both modules import from here now.

Why "blocked" is a real, distinct outcome from "dead"
------------------------------------------------------
A real 20-exhibitor small test (see discovery/fabtech_exhibitor_import.py's
git history) found several genuinely live, real companies (3M, 8020 Inc,
5 Star Engineering, 1stSource, Accurex Measurement, ABC Sheet Metal,
7 Seas Sourcing) failing a plain liveness check. 6 of 7 returned an
IDENTICAL, unambiguous signature: HTTP 403, response header
`server: cloudflare` + `cf-mitigated: challenge`, body titled "Just a
moment..." -- Cloudflare's own bot-challenge page, which a plain httpx
GET can never pass regardless of headers (confirmed: adding a real
browser User-Agent made no difference). Treating this the same as a
dead domain would silently drop real suppliers just because this
codebase's fetch tooling has no JS engine.

A connection timeout is classified "dead", not "blocked" -- there's no
positive signal distinguishing bot-throttling from a genuine outage the
way a served challenge page provides. A resolving page that's a generic
parked/default page (verification.website_contact_extractor.
parking_page_reason, reused rather than reinvented) is "dead" too --
same standard as discovery.linde_dealer_import.check_website_live.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from verification.website_contact_extractor import parking_page_reason

logger = logging.getLogger(__name__)

_LIVENESS_TIMEOUT = 15.0

# Confirmed live against 6 real, unrelated exhibitor domains (see this
# module's own docstring): a Cloudflare bot-challenge always returns
# one of these statuses together with a "cloudflare" server header
# and/or a "cf-mitigated" response header and/or a "Just a moment..."
# challenge page -- checking all three, not just one, since a site
# behind a DIFFERENT anti-bot vendor could still 403 without being a
# Cloudflare challenge specifically.
_BOT_CHALLENGE_STATUS_CODES = frozenset({403, 503})
_BOT_CHALLENGE_TEXT_SIGNATURES = ("just a moment", "checking your browser", "attention required")


def classify_website_reachability(url: str, http_client: Optional[httpx.Client] = None) -> str:
    """Returns "live", "blocked", or "dead" -- see this module's own
    docstring for why a bot-challenge response is NOT the same as a
    dead domain."""
    if not url or not url.strip():
        return "dead"
    client = http_client or httpx.Client(timeout=_LIVENESS_TIMEOUT, follow_redirects=True)
    try:
        response = client.get(url)
    except Exception as e:  # noqa: BLE001 -- one unreachable site must never abort a caller's batch
        logger.info("website_reachability: reachability check failed for %s: %s", url, e)
        return "dead"

    status = response.status_code
    if status in _BOT_CHALLENGE_STATUS_CODES:
        headers = getattr(response, "headers", {}) or {}
        server = str(headers.get("server", "")).lower()
        cf_mitigated = headers.get("cf-mitigated")
        text = (getattr(response, "text", "") or "").lower()
        if cf_mitigated or server == "cloudflare" or any(sig in text for sig in _BOT_CHALLENGE_TEXT_SIGNATURES):
            return "blocked"
        return "dead"
    if status >= 400:
        return "dead"

    text = getattr(response, "text", "") or ""
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    page_text = soup.get_text(separator=" ", strip=True)
    if parking_page_reason(page_text):
        return "dead"
    return "live"
