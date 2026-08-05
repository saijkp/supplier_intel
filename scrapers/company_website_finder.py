"""
scrapers/company_website_finder.py

Answers exactly the gap you asked about: an Alibaba/IndiaMart/
Made-in-China listing that gives a company name but no usable website,
phone, or email. Everything downstream of a domain — contact
extraction, capability extraction — already exists; this module is
what supplies the missing domain in the first place, by searching the
company's name and validating the result before trusting it.

Why validation is not optional here
--------------------------------------
A company-name web search is a fundamentally noisier signal than a
platform listing's self-reported URL. Generic supplier names are
genuinely common ("Zhejiang Trading Co.", "Ningbo Industries") and a
search can easily surface an unrelated company, a stale listing, or —
most likely of all — the *same* Alibaba/Made-in-China page you already
have, just re-indexed by Google. Silently accepting the first search
result and writing it as the supplier's domain would risk exactly the
failure this whole platform exists to prevent: attributing one
company's contact details and capability claims to a different real
company. That is worse than having no data at all, because it looks
authoritative.

So this module never trusts a search result on its own. It fetches the
candidate site (reusing `OwnWebsiteScraper` — no new fetching logic)
and checks whether the company's own name is actually findable,
fuzzy-matched, in that site's text. Only a validated match is ever
written back as the supplier's domain. An unvalidated top result is
still returned on the result object (never just silently dropped) so a
human can eyeball a near-miss the automatic threshold rejected, rather
than that signal disappearing entirely.

Reused, not reimplemented
----------------------------
Every piece of matching logic here already existed for a different
purpose and is reused as-is: `deduplication.domain_utils` for
recognising a search result that's just another B2B-platform page
(not a company's own site) and `deduplication.name_utils` +
`rapidfuzz` for the name-similarity check `deduplication.matcher.py`
already uses to decide two listings are the same company. This module
adds no new fuzzy-matching algorithm — it applies the one this
codebase already trusts to a new comparison (name vs. page text
instead of name vs. name).

Cost, honestly
----------------
Each company-name search is one SerpAPI call — meaningfully more
expensive than the free regex contact extraction or the cheap
gpt-4o-mini capability extraction, and worth knowing that before
running this across a large batch. The validation fetch itself is
free (same `OwnWebsiteScraper` HTTP fetch capability_extractor already
uses), and no LLM call is involved anywhere in this module — the name
check is pure string matching.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Optional

import tldextract
from rapidfuzz import fuzz

from deduplication.domain_utils import extract_domain, is_platform_subdomain
from deduplication.name_utils import normalise_company_name

# Non-platform domains that are still never a company's own site --
# social networks, general business directories, and reference sites
# a name search commonly surfaces. Deliberately separate from
# `PLATFORM_REGISTERED_DOMAINS` (which is specifically the B2B
# sourcing platforms this codebase already scrapes) since the
# reasoning for excluding each is different, even though the effect is
# the same.
_NON_COMPANY_DOMAINS = {
    "facebook.com", "linkedin.com", "youtube.com", "twitter.com", "x.com",
    "instagram.com", "pinterest.com", "wikipedia.org", "yellowpages.com",
    "europages.com", "tradeindia.com", "yelp.com", "crunchbase.com",
    "bloomberg.com", "glassdoor.com", "indeed.com", "yahoo.com",
    "bing.com", "google.com",
    # Industry news/data portals and B2B directories -- a real
    # candidate for a supplier's own site otherwise: they rank highly
    # for exactly the "<product> manufacturer/supplier" queries this
    # codebase's own search discipline generates, and a directory/
    # profile page fetched from one of these is never the company's own
    # site. Confirmed live: marklines.com and gasgoo.com both surfaced
    # as "candidates" for a real "wheel bearing units China" Sourcing
    # Agent brief and burned the entire discovery pass on dead fetches
    # (DNS failure, expired/self-signed cert) before this fix.
    "marklines.com", "gasgoo.com", "thomasnet.com", "globalspec.com",
    "kompass.com", "panjiva.com", "importgenius.com", "just-auto.com",
}

_DEFAULT_MIN_NAME_SIMILARITY = 55.0  # rapidfuzz partial_ratio is 0-100, not 0-1
_VALIDATION_TEXT_CHARS = 8_000  # how much of the fetched homepage text to search


@dataclasses.dataclass
class WebsiteFindingResult:
    company_name: str
    domain: Optional[str]              # set only when validated -- the field to trust
    validated: bool
    candidate_url: Optional[str]       # the top surviving search result, whether or not it validated
    name_match_score: Optional[float]  # 0-100, None if nothing was ever fetched to check
    reason: str


def _is_cloudflare_internal_path(link: Optional[str]) -> bool:
    """True for a link pointing at Cloudflare's own internal routing
    path (most commonly `/cdn-cgi/l/email-protection`, an obfuscated-
    mailto redirect that 404s to anything but a real browser executing
    its decode script) -- never a usable "read this company's homepage"
    URL, on ANY domain. Real Discovery Service failure this guards
    against: a search result pointing at exactly this path on an
    unrelated forum was treated as a candidate company site and burned
    an entire discovery pass on a dead fetch."""
    return bool(link) and "/cdn-cgi/" in link


def _is_usable_candidate_domain(domain: Optional[str]) -> bool:
    """True only for a domain that's plausibly a company's own site --
    not a known B2B platform, not a social network or general
    directory. Compares registered domains (via tldextract), not raw
    substrings, so this correctly ignores e.g. a company legitimately
    named with "facebook" or "google" somewhere in an unrelated
    domain string.
    """
    if not domain:
        return False
    if is_platform_subdomain(domain):
        return False
    extracted = tldextract.extract(domain)
    if not extracted.domain or not extracted.suffix:
        return False
    registered = f"{extracted.domain}.{extracted.suffix}".lower()
    return registered not in _NON_COMPANY_DOMAINS


class CompanyWebsiteFinder:
    """`google_scraper` and `own_website_scraper` are both injectable
    and only need to satisfy the same minimal shape
    `SupplierIntelligencePipeline` already constructs them with
    (`.scrape(query, max_results=...)` and `.fetch(domain)`
    respectively) -- no new dependency, and tests can fake both with
    no network.
    """

    def __init__(
        self,
        google_scraper: Any,
        own_website_scraper: Any,
        min_name_similarity: float = _DEFAULT_MIN_NAME_SIMILARITY,
    ):
        self.google_scraper = google_scraper
        self.own_website_scraper = own_website_scraper
        self.min_name_similarity = min_name_similarity

    def find_website(self, company_name: str, country: Optional[str] = None) -> WebsiteFindingResult:
        if not company_name or not company_name.strip():
            return WebsiteFindingResult(
                company_name=company_name, domain=None, validated=False,
                candidate_url=None, name_match_score=None, reason="no company name provided",
            )

        query = f'"{company_name}" {country}' if country else f'"{company_name}"'

        try:
            results = self.google_scraper.scrape(query, max_results=10)
        except Exception as e:
            return WebsiteFindingResult(
                company_name=company_name, domain=None, validated=False,
                candidate_url=None, name_match_score=None, reason=f"search failed: {e}",
            )

        candidate_url = None
        candidate_domain = None
        for result in results:
            if not getattr(result, "success", True):
                continue
            link = (result.raw_data or {}).get("link")
            if _is_cloudflare_internal_path(link):
                continue
            domain = extract_domain(link) if link else None
            if _is_usable_candidate_domain(domain):
                candidate_url = link
                candidate_domain = domain
                break

        if candidate_domain is None:
            return WebsiteFindingResult(
                company_name=company_name, domain=None, validated=False,
                candidate_url=None, name_match_score=None,
                reason="no non-platform, non-directory result found",
            )

        fetch_result = self.own_website_scraper.fetch(candidate_domain)
        if not fetch_result.success or not fetch_result.pages:
            return WebsiteFindingResult(
                company_name=company_name, domain=None, validated=False,
                candidate_url=candidate_url, name_match_score=None,
                reason=f"could not fetch candidate site: {fetch_result.error}",
            )

        # Only the normalised *name* goes through
        # normalise_company_name (strip legal suffixes/geo tokens,
        # lowercase) -- it's built for comparing one name against
        # another, not for processing a full page of prose. The page
        # text is only lowercased, so the comparison is fair (both
        # sides case-insensitive) without mangling page content that
        # was never a company name to begin with.
        normalised_name = normalise_company_name(company_name)
        page_text_lower = fetch_result.pages[0].text[:_VALIDATION_TEXT_CHARS].lower()
        score = fuzz.partial_ratio(normalised_name, page_text_lower)

        if score >= self.min_name_similarity:
            return WebsiteFindingResult(
                company_name=company_name, domain=candidate_domain, validated=True,
                candidate_url=candidate_url, name_match_score=score,
                reason=f"company name matched candidate site text (score={score:.0f})",
            )

        return WebsiteFindingResult(
            company_name=company_name, domain=None, validated=False,
            candidate_url=candidate_url, name_match_score=score,
            reason=f"candidate site found but name match too weak "
                   f"(score={score:.0f} < threshold {self.min_name_similarity:.0f})",
        )
