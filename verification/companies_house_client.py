"""
verification/companies_house_client.py

Low-level client for the UK Companies House public data API
(developer.company-information.service.gov.uk) -- real registered-
company data (legal status, registered office, incorporation date,
SIC codes), used as the UK-office validation source for categories
that require it (currently: Material Handling). No category awareness
lives here or in uk_company_verification_service.py -- both are
invoked manually against whatever candidate list/supplier ids the
caller hands them, same as batch-upload and the discovery pipeline.

Auth: HTTP Basic Auth with the API key as the username and a BLANK
password -- Companies House's own documented scheme, not a bug or an
oversight here.

Two endpoints:
1. GET /search/companies?q=<name> -- free-text company name search,
   returns candidate matches (company_number, title, company_status,
   address_snippet) but NOT the full profile.
2. GET /company/{company_number} -- full profile for one company by
   its number: company_status, date_of_creation, sic_codes,
   registered_office_address.

Never raises for ordinary failures (no API key, network error, no
matches, rate limit) -- returns an empty list / None instead, same
contract as every other verifier in this codebase
(facility_address_verifier.py, linkedin_presence.py,
apollo_contact_finder.py). Matching/confidence-scoring and any DB
writes are uk_company_verification_service.py's job, not this
client's -- this module only talks to the API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from config.settings import COMPANIES_HOUSE_API_KEY

logger = logging.getLogger(__name__)

COMPANIES_HOUSE_BASE_URL = "https://api.company-information.service.gov.uk"
# The public-facing company page -- not an API endpoint, but a real,
# human-clickable URL worth storing as field_provenance.source_url so
# a reviewer can open the exact record this data came from.
COMPANIES_HOUSE_PUBLIC_URL = "https://find-and-update.company-information.service.gov.uk/company"


@dataclass
class CompanySearchMatch:
    company_number: str
    title: str  # the registered name as Companies House has it on file
    company_status: Optional[str] = None
    address_snippet: Optional[str] = None


@dataclass
class CompanyProfile:
    company_number: str
    company_name: str
    company_status: Optional[str] = None
    date_of_creation: Optional[str] = None
    sic_codes: List[str] = field(default_factory=list)
    registered_office_address: Optional[str] = None
    source_url: str = ""


def _format_address(address: Dict[str, Any]) -> Optional[str]:
    """Companies House returns registered_office_address as separate
    fields (address_line_1, address_line_2, locality, region,
    postal_code, country) -- joined into one display string, empty
    parts skipped, exactly what field_provenance.value and
    suppliers.companies_house_registered_office both expect."""
    if not address:
        return None
    parts = [
        address.get("address_line_1"), address.get("address_line_2"),
        address.get("locality"), address.get("region"),
        address.get("postal_code"), address.get("country"),
    ]
    joined = ", ".join(p.strip() for p in parts if p and p.strip())
    return joined or None


class CompaniesHouseClient:

    def __init__(self, api_key: Optional[str] = None, http_client: Optional[httpx.Client] = None):
        self.api_key = api_key or COMPANIES_HOUSE_API_KEY
        self._client = http_client or httpx.Client(timeout=15)

    def search_companies(self, name: str, max_results: int = 5) -> List[CompanySearchMatch]:
        if not self.api_key or not name:
            return []
        try:
            response = self._client.get(
                f"{COMPANIES_HOUSE_BASE_URL}/search/companies",
                params={"q": name, "items_per_page": max_results},
                auth=(self.api_key, ""),
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:  # noqa: BLE001 -- never raise, see module docstring
            logger.warning("companies_house: search failed for %r: %s", name, e)
            return []

        matches: List[CompanySearchMatch] = []
        for item in (data.get("items") or [])[:max_results]:
            company_number = item.get("company_number")
            if not company_number:
                continue
            matches.append(CompanySearchMatch(
                company_number=company_number,
                title=item.get("title") or "",
                company_status=item.get("company_status"),
                address_snippet=item.get("address_snippet"),
            ))
        return matches

    def get_company_profile(self, company_number: str) -> Optional[CompanyProfile]:
        if not self.api_key or not company_number:
            return None
        try:
            response = self._client.get(
                f"{COMPANIES_HOUSE_BASE_URL}/company/{company_number}",
                auth=(self.api_key, ""),
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:  # noqa: BLE001 -- never raise, see module docstring
            logger.warning("companies_house: profile lookup failed for %r: %s", company_number, e)
            return None

        return CompanyProfile(
            company_number=company_number,
            company_name=data.get("company_name") or "",
            company_status=data.get("company_status"),
            date_of_creation=data.get("date_of_creation"),
            sic_codes=list(data.get("sic_codes") or []),
            registered_office_address=_format_address(data.get("registered_office_address") or {}),
            source_url=f"{COMPANIES_HOUSE_PUBLIC_URL}/{company_number}",
        )
