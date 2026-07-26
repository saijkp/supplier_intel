"""
verification/facility_address_verifier.py

Confirms a supplier's claimed address resolves to a real, findable
place — independent of and much cheaper than photo verification. This
exists specifically because Street View access for Chinese addresses
turned out to be a real dead end, not a simple provider swap: see the
country-routing rationale below before assuming this covers what
Google Places would for a non-China address.

Why this is address verification, not facility photo verification
----------------------------------------------------------------------
Two different questions, two different providers, deliberately kept
separate:

- "Does this address exist and resolve to a real, named place?" --
  answered here, by geocoding. Catches a fabricated or copy-pasted
  address outright.
- "Does a photo actually show a manufacturing facility?" -- answered
  by `verification.factory_photo_verifier`, fed by photos
  `OwnWebsiteScraper` + `scrapers.photo_downloader` collect from the
  supplier's own site. Geocoding APIs generally don't hand back
  street-level imagery outside their premium products, and for China
  specifically the imagery product (Baidu Street View) has the access
  barrier described below -- so photo evidence for a Chinese supplier
  has to come from their own website, not from the map provider.

The China routing decision, and why it isn't Baidu
--------------------------------------------------------
Two real options exist for China, and neither is a drop-in Google
Places replacement:

- Baidu Maps has Street View (the only Chinese provider that does),
  but individual developer registration is restricted to Chinese
  citizens with real-name ID verification, and while company/overseas
  accounts are technically possible, they're documented as genuinely
  difficult to obtain and slow to access from outside China.
- Amap (Gaode, an Alibaba subsidiary) is the more standard choice for
  business/POI data and has geocoding and place search, but has *no*
  Street View product at all, and registration has similar
  non-Chinese-entity friction, just without the ID-verification
  barrier being quite as explicit.

Given neither offers a low-friction path to street-level imagery, this
module routes China to Amap for geocoding/address confirmation only --
the lower-friction half of the two APIs -- and leans on
`factory_photo_verifier` fed by the supplier's own website photos to
cover the imagery question that Street View would otherwise answer.

NOT YET TESTED AGAINST A LIVE API CALL
------------------------------------------
Both providers below are built against their publicly documented
request/response contracts, the same disclosed-limitation stance
`scrapers.google_search_scraper` already takes for SerpAPI: written
directly against confirmed parameter names, but the exact shape of a
live response has not been exercised against a real API key in this
environment. Confirm both still match current documentation, and
budget real registration time for Amap specifically (see above) before
relying on the China path.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Optional, Protocol

import httpx

logger = logging.getLogger(__name__)

GOOGLE_PLACES_FIND_PLACE_URL = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
AMAP_GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"

# ISO 3166-1 alpha-2 codes routed to Amap instead of Google Places.
# Deliberately just mainland China -- Hong Kong and Macao have full
# Google Maps coverage and aren't behind the same access barrier.
_CHINA_ROUTED_COUNTRIES = {"CN", "CHINA"}


@dataclasses.dataclass(frozen=True)
class AddressVerificationResult:
    verified: bool
    source: str  # 'google_places' | 'amap' | 'unavailable'
    formatted_address: Optional[str]
    reason: str


class AddressVerifier(Protocol):
    def verify(self, address: str, company_name: str = "") -> AddressVerificationResult: ...


class GooglePlacesAddressVerifier:
    """Uses the Places "Find Place From Text" endpoint -- a single
    call that resolves free-text (an address, or an address plus
    company name) to a real place if one matches, or nothing if not.
    Standard registration: a Google Cloud project and a credit card,
    no ID verification, no country-specific friction -- the contrast
    with the Amap path below is deliberate context, not incidental.
    """

    def __init__(self, api_key: Optional[str] = None, http_client: Optional[Any] = None):
        from config.settings import GOOGLE_PLACES_API_KEY

        self.api_key = api_key or GOOGLE_PLACES_API_KEY
        self._client = http_client or httpx.Client(timeout=15.0)

    def verify(self, address: str, company_name: str = "") -> AddressVerificationResult:
        if not self.api_key:
            return AddressVerificationResult(
                verified=False, source="unavailable", formatted_address=None,
                reason="GOOGLE_PLACES_API_KEY is not configured",
            )
        if not address or not address.strip():
            return AddressVerificationResult(
                verified=False, source="google_places", formatted_address=None, reason="no address provided",
            )

        query = f"{company_name} {address}".strip()
        params = {
            "input": query,
            "inputtype": "textquery",
            "fields": "formatted_address,name,place_id",
            "key": self.api_key,
        }
        try:
            response = self._client.get(GOOGLE_PLACES_FIND_PLACE_URL, params=params)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error("Google Places lookup failed for %r: %s", address, e)
            return AddressVerificationResult(
                verified=False, source="google_places", formatted_address=None, reason=f"request failed: {e}",
            )

        candidates = data.get("candidates") or []
        if not candidates:
            return AddressVerificationResult(
                verified=False, source="google_places", formatted_address=None,
                reason="no matching place found",
            )

        best = candidates[0]
        return AddressVerificationResult(
            verified=True, source="google_places",
            formatted_address=best.get("formatted_address"),
            reason="address resolved to a real place",
        )


class AmapAddressVerifier:
    """Uses Amap's Geocoding API (`/v3/geocode/geo`) -- converts a
    free-text address into coordinates plus a normalised formatted
    address if one matches. See the module docstring for why this is
    the routed China path instead of Baidu, and for the real,
    documented registration friction non-Chinese developers should
    expect before this can be used live.
    """

    def __init__(self, api_key: Optional[str] = None, http_client: Optional[Any] = None):
        from config.settings import AMAP_API_KEY

        self.api_key = api_key or AMAP_API_KEY
        self._client = http_client or httpx.Client(timeout=15.0)

    def verify(self, address: str, company_name: str = "") -> AddressVerificationResult:
        if not self.api_key:
            return AddressVerificationResult(
                verified=False, source="unavailable", formatted_address=None,
                reason="AMAP_API_KEY is not configured (see module docstring for the real "
                       "registration friction to expect as a non-Chinese developer/company)",
            )
        if not address or not address.strip():
            return AddressVerificationResult(
                verified=False, source="amap", formatted_address=None, reason="no address provided",
            )

        params = {"address": address, "key": self.api_key}
        try:
            response = self._client.get(AMAP_GEOCODE_URL, params=params)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error("Amap geocode lookup failed for %r: %s", address, e)
            return AddressVerificationResult(
                verified=False, source="amap", formatted_address=None, reason=f"request failed: {e}",
            )

        # Amap returns status="1" (a string, not a bool) for a
        # successful call, with geocodes=[] as a normal "no match" --
        # not itself an error, mirrored from the same distinction every
        # other module in this codebase draws between failure and a
        # genuinely empty result.
        if data.get("status") != "1":
            return AddressVerificationResult(
                verified=False, source="amap", formatted_address=None,
                reason=f"Amap API error: {data.get('info', 'unknown error')}",
            )

        geocodes = data.get("geocodes") or []
        if not geocodes:
            return AddressVerificationResult(
                verified=False, source="amap", formatted_address=None, reason="no matching place found",
            )

        return AddressVerificationResult(
            verified=True, source="amap",
            formatted_address=geocodes[0].get("formatted_address"),
            reason="address resolved to a real place",
        )


def select_address_verifier(
    country: Optional[str], google_verifier: AddressVerifier, amap_verifier: AddressVerifier
) -> AddressVerifier:
    """Routes China to Amap, everyone else to Google Places. See the
    module docstring for why Amap (geocoding-only) rather than Baidu
    (which has Street View but a harder registration path) was chosen
    for the China route.
    """
    if (country or "").strip().upper() in _CHINA_ROUTED_COUNTRIES:
        return amap_verifier
    return google_verifier
