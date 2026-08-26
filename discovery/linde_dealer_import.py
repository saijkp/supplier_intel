"""
discovery/linde_dealer_import.py

Imports Linde Material Handling's own published authorized-dealer
network -- a real, static JSON file (Dealer-Finder-App-Data.json) Linde
serves directly from linde-mh.com, no search interaction needed. Found
while scoping OEM/certifier approved-supplier lists as a discovery
source: unlike the Toyota/Jungheinrich dealer locators (a zip-search
widget with no bulk export -- confirmed live, see that investigation's
own findings), Linde's page loads the ENTIRE worldwide network (370
dealers at the time this was built) as one plain JSON GET.

Deliberately bypasses discovery.candidate_validator.CandidateValidator
entirely -- no trader gate, no product-term check, no LLM name
corroboration. Those gates exist to establish that an UNVERIFIED
candidate (a search hit, an LLM guess, a bare Companies House SIC
match) is actually real and actually relevant. A dealer Linde itself
lists as part of its own authorized network doesn't need that same
corroboration -- the OEM relationship IS the evidence, stronger than
any inference validate() could make about it. See
normalizers/linde_dealer_normalizer.py's own docstring for the exact
field mapping.

Real data-quality issues found and fixed via a real 15-dealer small
test, not guessed in advance
-----------------------------------------------------------------------
1. Malformed URLs. Some raw `website` values use a single slash after
   the scheme ("http:/www.bravomontacargas.com") -- extract_domain()
   parses this as domain "http", which would silently collapse every
   dealer hitting this typo into ONE shared-domain golden record.
   _normalize_malformed_scheme() fixes the scheme before domain
   extraction.

2. Linde's own corporate domain used as a fallback "website" for a
   dealer with no independent site of its own. Confirmed on two real
   entries (Agritec Ltd -> linde-mh.co.za; Alkhorayef Commercial -> a
   linde-mh.com link with a UTM tracking parameter) -- neither is that
   dealer's own site. Checked the full list: 74 of 370 dealers (20%)
   resolve to a domain containing "linde", the majority Linde's own
   generic multi-region corporate template (confirmed by REAL fetch,
   not string pattern-matching -- see _LINDE_OWNED_DOMAINS' own
   docstring for exactly how each entry was verified, and which
   "linde"-containing domains turned out to be genuine independent
   local distributors instead, same as fenwick-linde.fr). Left
   unexcluded, the domain-exact-match dedup tier (0.95 auto-merge)
   would silently collapse unrelated dealers sharing one of these --
   the same class of bug as the marketplace-root collision fixed
   earlier, just with Linde's own domain playing that role. A dealer
   resolving to one of these is imported with no `domain` at all
   (same "no independent website" path automechanika_normalizer.py's
   own website-less rows already use), never treated as a real,
   shareable identity for dedup.

3. A domain can resolve (HTTP 200) while serving no real content at
   all -- tq-linde.vn returns a bare, unconfigured "Welcome to nginx!"
   default page. A plain status-code check would wrongly count this as
   live. check_website_live() also runs the page text through
   verification.website_contact_extractor.parking_page_reason() --
   the same shared parking-page signature list this codebase already
   uses everywhere else content gets trusted (CLAUDE.md standing rule
   7), reused here rather than reinvented. Still far short of a full
   CandidateValidator.validate() pass: no LLM call, no product-term
   check, no name corroboration -- just "is there a real page here at
   all."

Reuses pipeline.static_list_import.import_static_supplier_list for the
actual save_raw -> normalise -> resolve_and_store flow -- the exact
same dedup/merge engine every other source in this codebase goes
through, so a Linde dealer already on file (found earlier via a live
search, say) merges rather than duplicates.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from typing import Any, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup

from deduplication.domain_utils import extract_domain
from deduplication.matcher import SupplierMatcher
from normalizers.linde_dealer_normalizer import LindeDealerNormalizer
from pipeline.static_list_import import StaticImportStats, import_static_supplier_list
from storage.repository import SupplierRepository
from verification.website_contact_extractor import parking_page_reason

logger = logging.getLogger(__name__)

LINDE_DEALER_DATA_URL = "https://www.linde-mh.com/en/technical/Dealer-finder-app/Dealer-Finder-App-Data.json"
SOURCE_LABEL = "linde-oem-dealer-network"

_LIVENESS_TIMEOUT = 10.0

# Linde's own generic, centrally-templated regional corporate sites --
# NOT an independent dealer's own domain. Every entry here was checked
# with a real fetch before being added, not guessed from the domain
# string: each one currently renders the identical (or near-identical,
# per-locale-translated) "Homepage/Startseite Linde Material Handling"
# corporate template, the same content Linde's own linde-mh.com (the
# very site this module fetches the dealer JSON from) shows. Real,
# distinct local businesses that happen to have "linde" in their own
# domain were deliberately checked and EXCLUDED from this list once
# confirmed genuine -- same rigor fenwick-linde.fr (a real French
# co-branded distributor, 17 real branches) already passed:
#   - linde-hl.cl / montacargas.linde-hl.pe: "Linde High Lift" is a
#     distinct regional subsidiary brand, not the generic template.
#   - linde-mh.com.vn: real Vietnamese company "CTY Hoang Gia Lam" as
#     primary brand.
#   - lindemh.co.kr: real Korean company "SAY T&C" as primary brand.
#   - lindemhe.com, lindetrukit.fi: real distinct India/Finland
#     distributor branding ("Wihuri Tekninen Kauppa" in Finland's case).
#   - lindemh.com.au: real, substantive region-specific promotional
#     content (an actual sale, actual pricing) -- a genuinely operating
#     independent site, not a shared static template.
#   - linde-mh.rs: redirects to a real distinct company (Ekotehnika,
#     Serbia) -- not excluded here, though the domain STORED for that
#     dealer is still the pre-redirect linde-mh.rs rather than
#     ekotehnika.rs, since this module doesn't capture the final
#     post-redirect URL (see the separately-queued off-domain-redirect
#     fix for CandidateValidator's own fetcher path -- the same root
#     cause, not yet applied here). linde-mh.rs is used by only one
#     dealer in the real list, so this is a data-ACCURACY gap, not a
#     dedup-collision risk.
# hasel-linde-mh.com is included below despite not being 100% certain:
# real fetch returned an empty page (no distinct business identity
# found), and its domain matches the generic template's own naming
# convention exactly -- treated conservatively (excluded) rather than
# risking a false-collision on an unconfirmed identity.
#
# linde-mh.hu was missed in the first pass despite fetching the exact
# same template family -- title "Linde Magyarorszag Anyagmozgatasi
# Kft." is the same "Linde's own [Country] entity" naming pattern as
# linde-mh.it ("Linde Material Handling Italia", already excluded),
# not a genuinely distinct third-party company name the way Ekotehnika/
# Wihuri Tekninen Kauppa/SAY T&C are. Caught via a full-370-run spot
# check, added after the fact -- a real inconsistency in the first
# classification pass, not a deliberate "genuine third party" call.
_LINDE_OWNED_DOMAINS = frozenset({
    "linde-mh.at", "linde-mh.ch", "linde-mh.co.th", "linde-mh.com",
    "linde-mh.com.br", "linde-mh.com.my", "linde-mh.com.sg", "linde-mh.cz",
    "linde-mh.de", "linde-mh.es", "linde-mh.hu", "linde-mh.it", "linde-mh.pl",
    "linde-mh.pt", "linde-mh.se", "linde-mh.sk", "lindemh.com.hk",
    "hasel-linde-mh.com",
})

# Some raw `website` values use a single slash after the scheme
# ("http:/www.example.com") -- confirmed on real entries (Bravo
# Montacargas, Lift Truck Service Center). extract_domain() parses this
# malformed form as domain "http", which would silently collapse every
# dealer hitting this typo into one shared-domain golden record.
_MALFORMED_SCHEME_RE = re.compile(r"^(https?):/(?!/)")


def _normalize_malformed_scheme(url: str) -> str:
    return _MALFORMED_SCHEME_RE.sub(r"\1://", url.strip())


@dataclasses.dataclass
class LindeImportStats:
    total_dealers: int = 0
    no_website: int = 0              # imported anyway -- nothing to check liveness of
    linde_owned_domain: int = 0      # website is Linde's own domain, not the dealer's -- imported with no domain
    website_live: int = 0
    website_dead: int = 0            # skipped -- never reaches the normaliser/matcher
    static_import: Optional[StaticImportStats] = None

    def as_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["static_import"] = self.static_import.as_dict() if self.static_import else None
        return d


def fetch_linde_dealers(http_client: Optional[httpx.Client] = None) -> List[Dict[str, Any]]:
    """Real GET against Linde's own published JSON -- no auth, no
    pagination, the whole worldwide network in one response. Raises on
    a genuine fetch failure rather than swallowing it (unlike this
    codebase's scrapers, which never raise for an ORDINARY scrape
    failure among many candidates -- this is a single, one-shot bulk
    fetch where a failure means there's nothing at all to import, not
    one candidate among many to skip)."""
    client = http_client or httpx.Client(timeout=30.0)
    response = client.get(LINDE_DEALER_DATA_URL)
    response.raise_for_status()
    data = response.json()
    return list(data.get("dealers") or [])


def check_website_live(url: str, http_client: Optional[httpx.Client] = None) -> bool:
    """Real, lightweight liveness check -- a plain GET with a short
    timeout. Live means BOTH a 2xx/3xx response AND page text that
    doesn't match a known server-default/parking-page signature (see
    verification.website_contact_extractor.parking_page_reason,
    reused rather than reinvented -- a resolving domain with no real
    content, like a bare default nginx page, is not a usable dealer
    site). Still NOT a full CandidateValidator.validate() pass: no LLM
    call, no product-term check, no name corroboration."""
    if not url or not url.strip():
        return False
    client = http_client or httpx.Client(timeout=_LIVENESS_TIMEOUT, follow_redirects=True)
    try:
        response = client.get(url)
        if response.status_code >= 400:
            return False
        text = getattr(response, "text", "") or ""
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        page_text = soup.get_text(separator=" ", strip=True)
        if parking_page_reason(page_text):
            return False
        return True
    except Exception as e:  # noqa: BLE001 -- one dead/unreachable dealer site must never abort the import
        logger.info("linde_dealer_import: liveness check failed for %s: %s", url, e)
        return False


def import_linde_dealer_network(
    repo: Optional[SupplierRepository] = None,
    matcher: Optional[SupplierMatcher] = None,
    limit: Optional[int] = None,
    http_client: Optional[httpx.Client] = None,
    website_checker: Optional[Any] = None,
) -> LindeImportStats:
    """Fetches Linde's real dealer list, filters out Linde's own
    corporate domains and liveness-dead entries, then imports the
    survivors through the exact same dedup/merge engine every other
    source uses. `limit`, when given, caps how many dealers are
    processed (small-test-first, then the full ~370) -- applied AFTER
    fetching the real list, so a small test still sees a representative
    real slice, not an artificially truncated response.

    `website_checker`, if given, must be a callable(url) -> bool
    (defaults to check_website_live using `http_client`) -- injectable
    so tests can fake liveness without any real network, same
    convention as every other injectable dependency in this codebase.
    """
    repo = repo or SupplierRepository()
    matcher = matcher or SupplierMatcher(repo)
    checker = website_checker or (lambda url: check_website_live(url, http_client=http_client))

    dealers = fetch_linde_dealers(http_client=http_client)
    if limit is not None:
        dealers = dealers[:limit]

    stats = LindeImportStats(total_dealers=len(dealers))
    live_dealers: List[Dict[str, Any]] = []

    for dealer in dealers:
        raw_website = (dealer.get("website") or "").strip()
        if not raw_website:
            stats.no_website += 1
            live_dealers.append(dealer)
            continue

        website = _normalize_malformed_scheme(raw_website)
        domain = extract_domain(website)

        if domain and domain.lower() in _LINDE_OWNED_DOMAINS:
            stats.linde_owned_domain += 1
            dealer_for_import = dict(dealer)
            dealer_for_import["website"] = ""
            live_dealers.append(dealer_for_import)
            continue

        if checker(website):
            stats.website_live += 1
            dealer_for_import = dict(dealer)
            dealer_for_import["website"] = website
            live_dealers.append(dealer_for_import)
        else:
            stats.website_dead += 1
            raw_id = repo.save_raw(source=SOURCE_LABEL, raw_data=dealer, source_id=website)
            repo.mark_raw_processed(
                raw_id, status="failed",
                error_message=f"website did not resolve: {website}",
            )

    stats.static_import = import_static_supplier_list(
        repo, matcher, live_dealers,
        source_label=SOURCE_LABEL, normaliser=LindeDealerNormalizer(),
    )
    return stats
