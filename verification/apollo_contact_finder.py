"""
verification/apollo_contact_finder.py

Finds NAMED contacts (Procurement Manager, Sales Manager, CEO) at a
supplier, via Apollo.io -- the one gap every other module in this
codebase deliberately doesn't cover: `website_contact_extractor.py`
only ever finds a company-level `primary_email`/`primary_phone`, never
a person to actually address an email to.

The real Apollo API shape, confirmed against Apollo's own live
documentation (docs.apollo.io) while building this, not assumed:

1. `POST /api/v1/mixed_people/api_search` -- searches Apollo's existing
   database by `q_organization_domains_list`/`person_titles`. Genuinely
   FREE (does not consume credits). BUT deliberately returns only
   `first_name` and an *obfuscated* surname per person, no email, no
   LinkedIn URL -- Apollo's own privacy/monetisation design, not
   something this module can work around. Documented as requiring a
   "master" API key specifically; if search calls come back rejected
   or empty even for a company you know is in Apollo's database, check
   the configured key has that permission level first.
2. `POST /api/v1/people/match` (People Enrichment) -- the only way to
   get a full name, LinkedIn URL, and (with `reveal_personal_emails`)
   a real email for ONE specific person (by their Apollo `id` from step
   1). Costs 1 credit per email revealed. GDPR-protected individuals
   never get their email revealed regardless.

Cost governance this module enforces, deliberately: step 1 runs once
per `find_contacts()` call (free). Step 2 runs AT MOST 3 times per
call -- one enrichment per target role category (procurement, sales,
ceo) -- never once per person found in search. A `find_contacts()`
call therefore costs at most 3 Apollo credits, bounded and predictable,
regardless of how many people search happens to surface.

Explicitly NOT implemented in this version: phone number reveal.
Apollo's `reveal_phone_number` requires a `webhook_url` (an
asynchronous callback flow) -- this module only does synchronous
request/response calls, matching every other verifier in this
codebase. A future version could add this as its own webhook-receiving
endpoint; until then, `ApolloContact.phone` is always `None`, an
honest, disclosed gap rather than a silent one.

Exact request encoding (JSON body vs query string) for both endpoints
is this module's single biggest live-verification risk -- Apollo's own
docs describe both as taking "query parameters" while also specifying
`Content-Type: application/json`, which is the kind of ambiguity that
only gets resolved by an actual live call. See this module's own
`main.py doctor --live`-style smoke test before relying on it in the
batch pipeline.

Never raises -- same contract as every other verifier in this
codebase (`facility_address_verifier.py`, `linkedin_presence.py`).
`source="unavailable"` is used, deliberately distinct from a genuine
"searched, found nobody" result, for: no API key configured, the
search request itself failing, or an error response -- absence of a
signal must never look like a real negative to a caller (see
`verification_ai/cross_checker.py`'s own "silence isn't a red flag"
discipline, though this module isn't wired into cross_checker at all
-- it's a separate, opt-in enrichment stage, not a verification
sub-check).
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

APOLLO_BASE_URL = "https://api.apollo.io/api/v1"

# Keyword buckets for mechanical (no LLM needed) title categorisation --
# same "a pattern match doesn't need to cost money" philosophy
# sourcing/dossier_generator.py's own deterministic verification_status
# already uses. Also DOUBLES as the `person_titles` search filter, so
# search only ever surfaces people plausibly in one of these three
# roles in the first place, rather than searching broadly then
# discarding most of what comes back.
_ROLE_TITLE_KEYWORDS: Dict[str, tuple] = {
    "procurement": ("procurement", "purchasing", "sourcing", "buyer"),
    "sales": ("sales", "business development", "account manager", "export manager"),
    "ceo": ("ceo", "chief executive", "president", "managing director", "founder", "owner"),
}
_ALL_TARGET_TITLES: List[str] = [kw for kws in _ROLE_TITLE_KEYWORDS.values() for kw in kws]

# Order matters: checked in this sequence, first match wins, so a title
# like "Chief Executive & Founder" resolves to "ceo" consistently
# rather than depending on dict iteration order.
_ROLE_CHECK_ORDER = ("ceo", "procurement", "sales")


def _categorise_title(title: str) -> str:
    """Mechanical keyword match against _ROLE_TITLE_KEYWORDS -- returns
    'other' rather than dropping the contact when nothing matches,
    since omitting a real found contact just because its title doesn't
    match a keyword would throw away real evidence."""
    haystack = (title or "").lower()
    for role in _ROLE_CHECK_ORDER:
        if any(keyword in haystack for keyword in _ROLE_TITLE_KEYWORDS[role]):
            return role
    return "other"


@dataclasses.dataclass(frozen=True)
class ApolloContact:
    name: str
    title: str
    email: Optional[str]
    phone: Optional[str]  # always None in this version -- see module docstring
    linkedin_url: Optional[str]
    role_category: str  # 'procurement' | 'sales' | 'ceo' | 'other'


@dataclasses.dataclass(frozen=True)
class ApolloContactResult:
    contacts: List[ApolloContact]
    source: str  # 'apollo' (a real search completed) | 'unavailable'
    reason: str


class ApolloContactFinder:
    """`api_key`/`http_client` injectable, matching every other
    verifier in this codebase -- construction never requires
    credentials, only `find_contacts()` does."""

    def __init__(self, api_key: Optional[str] = None, http_client: Optional[Any] = None):
        from config.settings import APOLLO_API_KEY

        self.api_key = api_key or APOLLO_API_KEY
        self._client = http_client or httpx.Client(timeout=15.0)

    def find_contacts(self, company_name: str, domain: Optional[str] = None) -> ApolloContactResult:
        if not self.api_key:
            return ApolloContactResult(contacts=[], source="unavailable", reason="APOLLO_API_KEY is not configured")
        if not domain:
            return ApolloContactResult(
                contacts=[], source="unavailable", reason="no domain on file to search Apollo by",
            )

        candidates = self._search_people(domain)
        if candidates is None:
            # The search itself failed (network error, non-2xx, error
            # response) -- NOT evidence nobody was found. See module
            # docstring's "never let absence of a signal look like a
            # real negative" discipline.
            return ApolloContactResult(
                contacts=[], source="unavailable", reason="Apollo people search request failed",
            )

        by_role: Dict[str, Dict[str, Any]] = {}
        for candidate in candidates:
            role = _categorise_title(candidate.get("title") or "")
            if role != "other" and role not in by_role:
                by_role[role] = candidate

        if not by_role:
            return ApolloContactResult(
                contacts=[], source="apollo",
                reason=f"Apollo search completed but found nobody matching a target role at {domain}",
            )

        contacts: List[ApolloContact] = []
        for role, candidate in by_role.items():
            apollo_id = candidate.get("id")
            enriched = self._enrich_person(apollo_id) if apollo_id else None
            contacts.append(ApolloContact(
                name=(enriched or {}).get("name") or candidate.get("first_name") or "",
                title=(enriched or {}).get("title") or candidate.get("title") or "",
                email=(enriched or {}).get("email"),
                phone=None,
                linkedin_url=(enriched or {}).get("linkedin_url"),
                role_category=role,
            ))

        return ApolloContactResult(
            contacts=contacts, source="apollo", reason=f"found {len(contacts)} contact(s) at {domain}",
        )

    def _search_people(self, domain: str) -> Optional[List[Dict[str, Any]]]:
        """Returns None on failure (never a real signal), or a list --
        possibly empty, a genuine "found nobody" result -- on success.
        Free: does not consume Apollo credits."""
        try:
            response = self._client.post(
                f"{APOLLO_BASE_URL}/mixed_people/api_search",
                headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
                json={
                    "q_organization_domains_list": [domain],
                    "person_titles": _ALL_TARGET_TITLES,
                    "per_page": 10,
                },
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error("Apollo people search failed for domain=%r: %s", domain, e)
            return None
        people = data.get("people")
        return people if isinstance(people, list) else []

    def _enrich_person(self, apollo_id: str) -> Optional[Dict[str, Any]]:
        """Returns None on failure or no match -- a single contact's
        enrichment failing degrades that one contact (falls back to
        the name/title already known from search), never the whole
        find_contacts() call. Costs 1 Apollo credit on success (email
        revealed) per the module docstring's cost governance -- called
        at most 3 times per find_contacts() call."""
        try:
            response = self._client.post(
                f"{APOLLO_BASE_URL}/people/match",
                headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
                params={"id": apollo_id, "reveal_personal_emails": "true"},
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.warning("Apollo person enrichment failed for id=%r: %s", apollo_id, e)
            return None
        person = data.get("person") or {}
        if not person:
            return None
        return {
            "name": person.get("name"),
            "title": person.get("title"),
            "email": person.get("email"),
            "linkedin_url": person.get("linkedin_url"),
        }
