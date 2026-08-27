"""
discovery/fabtech_exhibitor_import.py

Imports FABTECH's own real exhibitor directory -- a server-rendered,
A2Z Inc-powered listing (Exhibitors.aspx) with no login/JS/pagination
needed: one GET returns all ~1,400 real exhibitors (confirmed: name +
booth number for every one, no truncation). Found while scoping trade
show exhibitor directories the same way OEM dealer locators were
checked (see discovery/linde_dealer_import.py) -- Canton Fair is
login-walled, MODEX's directory redirects to general show content, but
FABTECH's loads as plain HTML.

Two real fetches per exhibitor, not one
------------------------------------------
Unlike Linde's dealer JSON (which embeds each dealer's own website
directly), FABTECH's exhibitor list itself has NO website field -- only
name and booth number. Each exhibitor's real company website lives on
a SEPARATE page: their own profile on FABTECH's SmallWorldLabs
platform (`fabtech2026.smallworldlabs.com`), reached via the exhibitor
list's own booth-ID-keyed URL. That profile page has a real, structured
"Website" field (confirmed via real fetches: Mazak Optonics Corporation
-> https://www.mazakoptonics.com; Intermark Steel ->
http://intermarksteel.com; 20 more spread across the full list, see
this module's own test suite) -- reached via a 302 redirect from the
boothId query-param URL to the profile's real slug URL, so the http
client MUST follow redirects (`follow_redirects=True`) or every fetch
silently returns an empty 0-byte body instead of the real page.

The profile page's "Website"/"Address"/"Phone"/"Pavilion" fields are
extracted by their own structured markup (`.text-secondary` label div
+ sibling `.profileResponse` value div), not by scraping arbitrary
`<a href>` links off the page -- confirmed more reliable: an early
href-based approach on Mazak's page also picked up platform/analytics
noise (a2zinc.net, mya2zevents.com) that had to be filtered out
after the fact, where the labeled "Website" field never includes that
noise in the first place.

Real data-quality issue found and handled, not guessed in advance
-----------------------------------------------------------------------
"CDS a2z SSO Multiuser Company 1" / "... Company 2" are A2Z's own
platform QA/test seed accounts, not real exhibitors -- confirmed via a
real fetch of their profile page, which shows "Address: 123 Test
Street, Test, KS 12345, United States". Excluded by an exact name-
prefix match (`_TEST_ACCOUNT_NAME_PREFIX`), not a broad guess: checked
the full 1,400-row list for anything matching a wider "test/dummy/SSO"
pattern first and found only these two, the rest being real companies
whose names happen to contain "Assoc"/"Associates" (e.g. "Precision
Metalforming Association").

Collision-risk check (same class of bug as the marketplace-root and
Linde-owned-domain collisions found earlier)
-----------------------------------------------------------------------
Checked before trusting this source's domain-based dedup, same
discipline as discovery/linde_dealer_import.py's own
_LINDE_OWNED_DOMAINS investigation:
  - No two exhibitors share a BoothID (checked all 1,400 rows: 1,400
    unique booth IDs) -- there is no pavilion/group-booth structure
    where multiple distinct companies would resolve to the same
    profile page and thus the same extracted website.
  - No two of a real 20-exhibitor spread-sample (every ~70th row
    across the full list) resolved to the same non-platform domain --
    see this module's own test suite's fixture data for the real
    company/domain pairs checked.
  - FABTECH's own platform domains (SmallWorldLabs, A2Z's event
    infrastructure) are excluded from ever being treated as an
    exhibitor's own website in the first place, since the "Website"
    field is read from the exhibitor's own structured profile data,
    never inferred from a platform link -- so there is no equivalent
    of Linde's "dealer's fallback website is Linde's own domain"
    failure mode to guard against here.

Deliberately bypasses discovery.candidate_validator.CandidateValidator
entirely, same reasoning as linde_dealer_import.py: a company
exhibiting at FABTECH under its own name and profile is a real,
self-reported identity signal from a legitimate industry trade show,
not an unverified search hit needing corroboration. Weaker evidence
than Linde's own-published OEM-dealer network though (paying for a
booth needs no ongoing vetted relationship) -- see
verification/scorer.py's SOURCE_QUALITY_WEIGHTS for where this is
positioned relative to every other source.

Reuses pipeline.static_list_import.import_static_supplier_list for the
actual save_raw -> normalise -> resolve_and_store flow -- the same
dedup/merge engine every other source in this codebase goes through.

Bot-challenge responses are NOT treated as "dead" (real finding, not
guessed in advance)
-----------------------------------------------------------------------
Reachability is classified via verification.website_reachability.
classify_website_reachability ("live"/"blocked"/"dead" -- see that
module's own docstring for the full real-data investigation behind the
3-way split: a real 20-exhibitor small test found several genuinely
live companies, e.g. 3M and 8020 Inc, failing a plain liveness check
only because their sites serve a Cloudflare bot-challenge, not because
they're actually dead). That function originated here and was promoted
to a shared module once monitoring/monitoring_service.py needed the
same classification for arbitrary supplier websites, not just FABTECH
exhibitors.

Unlike Linde's raw dealer JSON (where a liveness check exists
specifically to catch STALE/CLOSED dealers -- a real, demonstrated
problem there), FABTECH's "Website" field is self-reported on the
exhibitor's own profile for an upcoming 2026 show they are actively
paying to attend -- a materially stronger currency signal. Per an
explicit user decision: a "blocked" exhibitor is still imported WITH
its self-reported domain populated (never silently dropped just
because our own fetch was denied) -- but tagged as unconfirmed via a
field_provenance entry (source_tier="other", the closest existing
CHECK-constrained value to "not our own verified fetch" -- see
storage/database.py's field_provenance table; a distinct
source_tier='self_reported_tradeshow' value would need its own schema
migration, judged not worth it for what "other" already captures:
"not independently verified via our own domain fetch"), so nothing
downstream can mistake this for the same confidence level as a page
this codebase actually read. See _import_blocked_record's own
docstring for the exact mechanics.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from typing import Any, Callable, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup

from deduplication.matcher import SupplierMatcher
from normalizers.fabtech_exhibitor_normalizer import FabtechExhibitorNormalizer
from pipeline.static_list_import import StaticImportStats, import_static_supplier_list
from storage.repository import SupplierRepository
from verification.website_reachability import classify_website_reachability

logger = logging.getLogger(__name__)

FABTECH_EXHIBITOR_LIST_URL = "https://s36.a2zinc.net/clients/SME/FABTECH2026/Public/Exhibitors.aspx"
FABTECH_PROFILE_URL_TEMPLATE = (
    "https://fabtech2026.smallworldlabs.com/?page_id=2424&boothId={booth_id}&EventID=128"
)
SOURCE_LABEL = "trade-show-exhibitor-fabtech"

_LIST_FETCH_TIMEOUT = 30.0
_PROFILE_FETCH_TIMEOUT = 15.0

_BOOTH_ID_RE = re.compile(r"BoothID=(\d+)")

# A2Z's own platform QA/test seed accounts -- confirmed via a real
# fetch, not a guess. See this module's own docstring.
_TEST_ACCOUNT_NAME_PREFIX = "CDS a2z SSO"


@dataclasses.dataclass
class FabtechImportStats:
    total_exhibitors: int = 0
    test_accounts_excluded: int = 0
    no_website: int = 0          # imported anyway -- nothing to check reachability of
    website_live: int = 0
    website_blocked: int = 0     # bot-challenge response -- imported with domain, tagged unconfirmed
    website_dead: int = 0        # skipped -- never reaches the normaliser/matcher
    static_import: Optional[StaticImportStats] = None

    def as_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["static_import"] = self.static_import.as_dict() if self.static_import else None
        return d


def fetch_fabtech_exhibitor_list(
    http_client: Optional[httpx.Client] = None,
) -> "tuple[List[Dict[str, Any]], int]":
    """Real GET against FABTECH's own server-rendered exhibitor table --
    no auth, no pagination, all ~1,400 real exhibitors in one response.
    Raises on a genuine fetch failure (single one-shot bulk fetch, same
    convention as fetch_linde_dealers -- a failure here means nothing
    at all to import, not one candidate among many to skip).

    Excludes A2Z's own test/QA seed accounts (see this module's own
    docstring) before they ever reach a per-exhibitor profile fetch.
    Returns `(exhibitors, test_accounts_excluded)` -- the count is
    real, computed output, not a side channel."""
    client = http_client or httpx.Client(timeout=_LIST_FETCH_TIMEOUT)
    response = client.get(FABTECH_EXHIBITOR_LIST_URL)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    exhibitors: List[Dict[str, Any]] = []
    test_accounts = 0
    for row in soup.select(".listTableBody table tbody tr"):
        name_link = row.select_one("a.exhibitorName")
        if not name_link:
            continue
        name = name_link.get_text(strip=True)
        if not name:
            continue
        if name.startswith(_TEST_ACCOUNT_NAME_PREFIX):
            test_accounts += 1
            continue

        href = name_link.get("href") or ""
        match = _BOOTH_ID_RE.search(href)
        if not match:
            continue
        booth_id = match.group(1)

        booth_label = row.select_one("a.boothLabel")
        booth_number = ""
        if booth_label:
            booth_number = booth_label.get("sortval") or booth_label.get_text(strip=True)

        exhibitors.append({"name": name, "booth_id": booth_id, "booth_number": booth_number})

    if test_accounts:
        logger.info("fabtech_exhibitor_import: excluded %d test/QA seed account(s)", test_accounts)
    return exhibitors, test_accounts


def _extract_profile_fields(soup: BeautifulSoup) -> Dict[str, str]:
    """Reads FABTECH's own structured per-exhibitor profile fields
    (Name/Website/Pavilion/Hall/Phone/Address), keyed by their own
    `.text-secondary` label divs -- not scraped from arbitrary page
    links. The `Website` field's value is taken from its own `<a
    href>` when present (the actual link target, not just its display
    text); `Address`'s multi-line value (street / city-state-zip /
    country, separated by `<br>` in the source markup) is preserved as
    newline-joined text so the normaliser can parse country/city out of
    it the same way normalizers/automechanika_normalizer.py parses its
    own comma-delimited address format."""
    fields: Dict[str, str] = {}
    for label_div in soup.select(".text-secondary"):
        label = label_div.get_text(strip=True)
        if not label:
            continue
        row = label_div.find_parent("div", class_="row")
        if not row:
            continue
        response = row.select_one(".profileResponse")
        if not response:
            continue
        if label == "Website":
            link = response.find("a", href=True)
            fields[label] = link["href"].strip() if link else response.get_text(strip=True)
        else:
            fields[label] = response.get_text(separator="\n", strip=True)
    return fields


def fetch_exhibitor_profile(booth_id: str, http_client: Optional[httpx.Client] = None) -> Dict[str, str]:
    """Real fetch of one exhibitor's own SmallWorldLabs profile page.
    MUST follow redirects: the boothId query-param URL 302s to the
    profile's real slug URL (confirmed live -- a plain GET without
    follow_redirects returns a 0-byte body). Returns {} (never raises)
    on any fetch failure or a page with no recognisable profile
    fields -- one bad exhibitor profile must never abort the whole
    import, same discipline as check_website_live's own exception
    handling."""
    client = http_client or httpx.Client(timeout=_PROFILE_FETCH_TIMEOUT, follow_redirects=True)
    url = FABTECH_PROFILE_URL_TEMPLATE.format(booth_id=booth_id)
    try:
        response = client.get(url)
        if response.status_code >= 400:
            return {}
        text = getattr(response, "text", "") or ""
        if not text:
            return {}
        soup = BeautifulSoup(text, "html.parser")
        return _extract_profile_fields(soup)
    except Exception as e:  # noqa: BLE001 -- one unreachable profile page must never abort the import
        logger.info("fabtech_exhibitor_import: profile fetch failed for booth %s: %s", booth_id, e)
        return {}


def _import_blocked_record(
    repo: SupplierRepository, matcher: SupplierMatcher,
    record: Dict[str, Any], static_stats: StaticImportStats,
) -> None:
    """Mirrors pipeline.static_list_import.import_static_supplier_list's
    own save_raw -> normalise -> resolve_and_store flow for a single
    "blocked" record, with one addition: once the supplier is
    resolved, a field_provenance entry marks the stored `domain` as
    unconfirmed -- self-reported on the exhibitor's own FABTECH
    profile, never independently fetched (a bot-challenge blocked
    verification). Kept as a separate manual pass rather than
    extending import_static_supplier_list itself, since this
    per-record field_provenance write is specific to this module's own
    "blocked" classification, not something every other static-list
    source needs. See this module's own docstring for why "blocked"
    still gets a domain rather than being dropped like a genuinely
    dead site."""
    website = record.get("website", "")
    raw_id = repo.save_raw(source=SOURCE_LABEL, raw_data=record, source_id=website)

    normaliser = FabtechExhibitorNormalizer()
    try:
        candidate = normaliser.normalise(record)
    except Exception as e:
        logger.error("fabtech_exhibitor_import: normalisation failed for raw_id=%s: %s", raw_id, e)
        repo.mark_raw_processed(raw_id, status="failed", error_message=str(e))
        static_stats.failed += 1
        return

    if not candidate.get("canonical_name"):
        repo.mark_raw_processed(raw_id, status="failed", error_message="missing canonical_name")
        static_stats.skipped_no_name += 1
        return

    static_stats.total += 1
    static_stats.normalised += 1

    try:
        resolution = matcher.resolve_and_store(candidate)
    except Exception as e:
        logger.error("fabtech_exhibitor_import: dedup/store failed for raw_id=%s: %s", raw_id, e)
        repo.mark_raw_processed(raw_id, status="failed", error_message=str(e))
        static_stats.failed += 1
        return

    action = resolution["action"]
    if action == "created":
        static_stats.created += 1
    elif action == "merged":
        static_stats.merged += 1
    elif action == "review_queued":
        static_stats.review_queued += 1

    supplier_id = resolution.get("supplier_id") or resolution.get("new_supplier_id")
    repo.mark_raw_processed(raw_id, golden_record_id=supplier_id, status="processed")

    if supplier_id and candidate.get("domain"):
        profile_url = FABTECH_PROFILE_URL_TEMPLATE.format(booth_id=record.get("booth_id", ""))
        repo.save_field_provenance(
            supplier_id=supplier_id, field_name="domain", value=candidate["domain"],
            source_url=profile_url,
            raw_snippet=(
                f"Self-reported on FABTECH exhibitor profile as {website!r}; independent "
                f"verification was blocked by a bot-challenge response, not confirmed by "
                f"our own fetch."
            ),
            extraction_method="fabtech_exhibitor_profile_self_reported_bot_blocked",
            source_tier="other", claim_type="verifiable_fact",
        )


def import_fabtech_exhibitors(
    repo: Optional[SupplierRepository] = None,
    matcher: Optional[SupplierMatcher] = None,
    limit: Optional[int] = None,
    http_client: Optional[httpx.Client] = None,
    reachability_classifier: Optional[Callable[[str], str]] = None,
    profile_fetcher: Optional[Callable[[str], Dict[str, str]]] = None,
) -> FabtechImportStats:
    """Fetches FABTECH's real exhibitor list, then for each exhibitor:
    fetches their own profile page for a real website, classifies its
    reachability (live / blocked / dead -- see classify_website_
    reachability's own docstring), and imports survivors through the
    same dedup/merge engine every other source uses. `limit`, when
    given, caps how many exhibitors are processed (small-test-first,
    then the full ~1,400) -- applied AFTER fetching the real list, so
    a small test still sees a representative real slice.

    "live" exhibitors go through the normal batched
    import_static_supplier_list path. "blocked" exhibitors go through
    _import_blocked_record instead, one at a time, since each needs
    its own field_provenance write once its supplier_id is known --
    see that function's own docstring. "dead" exhibitors are recorded
    in raw_source_data as failed and never reach the normaliser/matcher
    at all, same as discovery.linde_dealer_import's own dead-dealer
    handling.

    `reachability_classifier` and `profile_fetcher`, if given, must be
    callables (defaulting to classify_website_reachability / fetch_
    exhibitor_profile, both using `http_client`) -- injectable so
    tests can fake both without any real network, same convention as
    import_linde_dealer_network's own `website_checker` parameter.
    """
    repo = repo or SupplierRepository()
    matcher = matcher or SupplierMatcher(repo)
    classify = reachability_classifier or (lambda url: classify_website_reachability(url, http_client=http_client))
    fetch_profile = profile_fetcher or (lambda booth_id: fetch_exhibitor_profile(booth_id, http_client=http_client))

    exhibitors, test_accounts_excluded = fetch_fabtech_exhibitor_list(http_client=http_client)
    if limit is not None:
        exhibitors = exhibitors[:limit]

    stats = FabtechImportStats(total_exhibitors=len(exhibitors), test_accounts_excluded=test_accounts_excluded)
    records_for_import: List[Dict[str, Any]] = []
    blocked_records: List[Dict[str, Any]] = []

    for exhibitor in exhibitors:
        fields = fetch_profile(exhibitor["booth_id"])
        raw_website = (fields.get("Website") or "").strip()

        record = dict(exhibitor)
        record["address"] = fields.get("Address", "")
        record["phone"] = fields.get("Phone", "")
        record["pavilion"] = fields.get("Pavilion", "")

        if not raw_website:
            stats.no_website += 1
            record["website"] = ""
            records_for_import.append(record)
            continue

        record["website"] = raw_website
        reachability = classify(raw_website)

        if reachability == "live":
            stats.website_live += 1
            records_for_import.append(record)
        elif reachability == "blocked":
            stats.website_blocked += 1
            blocked_records.append(record)
        else:
            stats.website_dead += 1
            raw_id = repo.save_raw(source=SOURCE_LABEL, raw_data=record, source_id=raw_website)
            repo.mark_raw_processed(
                raw_id, status="failed",
                error_message=f"website did not resolve: {raw_website}",
            )

    stats.static_import = import_static_supplier_list(
        repo, matcher, records_for_import,
        source_label=SOURCE_LABEL, normaliser=FabtechExhibitorNormalizer(),
    )

    for record in blocked_records:
        _import_blocked_record(repo, matcher, record, stats.static_import)

    return stats
