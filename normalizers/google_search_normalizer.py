"""
normalizers/google_search_normalizer.py

Maps raw Google search results (from scrapers.google_search_scraper) into
the supplier_data shape storage.repository.SUPPLIER_WRITABLE_FIELDS expects.

Google results are the least structured source in this pipeline — just
a title, a link, and a text snippet, with no dedicated "company name"
field at all. This normalizer treats the page title as the candidate
company name (stripped of common suffixes like "| Home" or "- About
Us"), which works reasonably well for a company's own homepage but
should be treated as the weakest-confidence source in the pipeline —
its main value is as an independent corroborating signal for the dedup
matcher (does this domain/name also show up outside the B2B platforms),
not as a primary data-rich source.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict

from deduplication.domain_utils import extract_domain, is_platform_subdomain
from normalizers.base_normalizer import BaseNormalizer

logger = logging.getLogger(__name__)

# Trailing separators + boilerplate commonly appended to <title> tags,
# stripped so "ABC Trailer Parts Co | Home - Manufacturer in China"
# becomes just "ABC Trailer Parts Co".
_TITLE_SUFFIX_RE = re.compile(
    r"\s*[\|\-–—]\s*(home|welcome|official website|about us|manufacturer.*|supplier.*)\s*$",
    re.IGNORECASE,
)


class GoogleSearchNormalizer(BaseNormalizer):
    source_name = "google"

    def normalise(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        title = self.clean_str(raw_data.get("title"))
        company_name = self._clean_title(title)
        if not company_name:
            logger.warning("Google result missing a usable title — canonical_name will be empty.")

        link = self.clean_str(raw_data.get("link"))
        domain = extract_domain(link) if link else None

        # A result whose domain is itself a B2B platform (alibaba.com,
        # made-in-china.com, etc.) isn't independent corroboration the
        # way a company's own domain is — still worth keeping (it's a
        # real find), but not conflated with "this is their own website".
        own_website = domain and not is_platform_subdomain(domain)

        supplier_data: Dict[str, Any] = {
            "canonical_name": company_name,
            "domain": domain if own_website else None,
            "moq_notes": self.clean_str(raw_data.get("snippet")) or None,
        }

        return {
            k: v for k, v in supplier_data.items()
            if k == "canonical_name" or v not in (None, "", [])
        }

    @staticmethod
    def _clean_title(title: str) -> str:
        if not title:
            return ""
        return _TITLE_SUFFIX_RE.sub("", title).strip()
