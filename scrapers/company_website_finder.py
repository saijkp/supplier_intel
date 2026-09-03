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
import logging
from typing import Any, Optional

import tldextract
from rapidfuzz import fuzz

from deduplication.domain_utils import extract_domain, is_platform_subdomain
from deduplication.name_utils import (
    _shares_distinctive_token,
    names_plausibly_corroborate,
    normalise_company_name,
)
from llm.client import LLMClient
from llm.prompts import GROUNDED_COMPANY_NAME_EXTRACTION_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

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
    # Additional B2B marketplaces (same class as
    # PLATFORM_REGISTERED_DOMAINS's Alibaba/IndiaMART/etc., listed here
    # instead since that set is specifically "sources this codebase
    # already scrapes structured listings from"). tradekey.com confirmed
    # live: surfaced as a candidate for a real "trailer axle China"
    # brief, and a malformed contact-page href on it crashed
    # OwnWebsiteScraper with a URL-parsing error before that fix.
    "tradekey.com", "dhgate.com", "ec21.com", "exportersindia.com",
    "go4worldbusiness.com",
    # UK government digital-services domain -- covers
    # find-and-update.company-information.service.gov.uk (Companies
    # House's own public company-profile pages) and every other
    # *.service.gov.uk government service, none of which is ever a
    # private company's own site. Confirmed live via
    # discovery.companies_house_sic_source.py: a real company-name
    # search for a small UK company with no strong independent web
    # presence surfaces ITS OWN Companies House profile page as the
    # top result, which trivially "validates" (the profile page
    # literally contains the company's own registered name) --
    # downstream candidate_validator.py always correctly rejected the
    # resulting candidate (a CH profile page never mentions a real
    # product term), so no bad data was ever stored, but the shared
    # bogus domain caused a WORSE, silent failure: multiple different,
    # unrelated real companies all "resolving" to this exact same
    # domain string tripped CompaniesHouseSicSource's own within-batch
    # seen_domains dedup, silently dropping every occurrence after the
    # first as a false "already seen" duplicate -- real candidates
    # never got their own validation attempt at all. tldextract's own
    # PSL entry for .gov.uk treats "service.gov.uk" as the registered
    # domain (verified directly: extracting
    # find-and-update.company-information.service.gov.uk gives
    # domain="service", suffix="gov.uk"), so this one entry covers
    # every *.service.gov.uk subdomain.
    "service.gov.uk",
}

# Stock-photo/media platforms -- never a company's own site, same
# "not an independently-verifiable company identity" reasoning as
# PLATFORM_REGISTERED_DOMAINS/_NON_COMPANY_DOMAINS above, but matched on
# the registered domain's LABEL alone (via tldextract), not the full
# registered-domain string: unlike those two sets' entries, these
# platforms operate region-specific TLDs (gettyimages.com,
# gettyimages.co.uk, gettyimages.de, gettyimages.nl, ...), so a single
# full-string entry per brand would miss most of its real domains.
# Found live: gettyimages.nl was VALIDATED as a "trailer mudguard
# manufacturer" candidate (extracted_name "Getty Images" fuzzy-matched
# the search snippet at score=67) -- a stock-photo listing/caption page
# happened to mention "trailer mudguard", and this codebase had no
# domain-level check that would have rejected it before ever reaching
# that content check. Kept to unambiguous, globally-known pure stock-
# media agencies only (no company plausibly named identically) --
# istockphoto is Getty's own subsidiary, included for the same reason.
_STOCK_MEDIA_DOMAIN_LABELS = {
    "gettyimages", "istockphoto", "shutterstock", "alamy",
    "dreamstime", "123rf", "depositphotos",
}


def _is_stock_media_domain(domain: Optional[str]) -> bool:
    """True if `domain`'s registered-domain LABEL (ignoring TLD/suffix)
    matches a known stock-photo/media platform -- see
    _STOCK_MEDIA_DOMAIN_LABELS's own comment for why this checks the
    label alone rather than the full registered-domain string the other
    domain sets in this module use."""
    if not domain:
        return False
    extracted = tldextract.extract(domain)
    if not extracted.domain:
        return False
    return extracted.domain.lower() in _STOCK_MEDIA_DOMAIN_LABELS


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
    if _is_stock_media_domain(domain):
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
        llm_client: Optional[LLMClient] = None,
    ):
        self.google_scraper = google_scraper
        self.own_website_scraper = own_website_scraper
        self.min_name_similarity = min_name_similarity
        # Defaulted (never requires OPENAI_API_KEY to construct, same
        # contract as discovery.candidate_validator.CandidateValidator's
        # own llm_client -- see _corroborates_via_grounded_extraction's
        # own docstring for what this is used for).
        self.llm_client = llm_client or LLMClient()

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
        page_text = fetch_result.pages[0].text[:_VALIDATION_TEXT_CHARS]
        page_text_lower = page_text.lower()
        score = fuzz.partial_ratio(normalised_name, page_text_lower)

        if score < self.min_name_similarity:
            return WebsiteFindingResult(
                company_name=company_name, domain=None, validated=False,
                candidate_url=candidate_url, name_match_score=score,
                reason=f"candidate site found but name match too weak "
                       f"(score={score:.0f} < threshold {self.min_name_similarity:.0f})",
            )

        # Real false match, confirmed live: searching "Ashpock" (the
        # real trailer-lighting manufacturer is "Aspock"/"Aspöck")
        # resolved to shpock.com (Shpock, an unrelated classifieds
        # app) -- fuzz.partial_ratio("ashpock", "...shpock...") scores
        # ~92 because "shpock" aligns as a near-perfect SUBSTRING of
        # "ashpock", the exact blind spot partial_ratio has (it only
        # rewards the best local alignment, never penalises the rest
        # of either string). _shares_distinctive_token doesn't have
        # that blind spot -- "ashpock" and "shpock" are different exact
        # word-tokens, so they share none, and the page text is long
        # enough that this is checking "does the literal word
        # 'ashpock' appear anywhere on the page", not a fuzzy
        # approximation of it.
        if not _shares_distinctive_token(normalised_name, page_text_lower):
            return WebsiteFindingResult(
                company_name=company_name, domain=None, validated=False,
                candidate_url=candidate_url, name_match_score=score,
                reason=f"candidate site text fuzzy-matched (score={score:.0f}) but never "
                       f"actually says the company's own distinctive name -- likely a "
                       f"near-miss on an unrelated site's name",
            )

        if not self._corroborated_by_grounded_extraction(company_name, page_text):
            return WebsiteFindingResult(
                company_name=company_name, domain=None, validated=False,
                candidate_url=candidate_url, name_match_score=score,
                reason="page's own self-stated name (heading/footer/copyright) does not "
                       "corroborate the searched company -- likely a third-party page "
                       "that merely mentions the company (e.g. a directory or filing-agent "
                       "record), not the company's own site",
            )

        return WebsiteFindingResult(
            company_name=company_name, domain=candidate_domain, validated=True,
            candidate_url=candidate_url, name_match_score=score,
            reason=f"company name matched candidate site text (score={score:.0f})",
        )

    def _corroborated_by_grounded_extraction(self, company_name: str, page_text: str) -> bool:
        """Real false match, confirmed live: searching "IK Eng Ltd"
        resolved to easydigitalfiling.com (a UK company-formation/
        filing agent's site that merely lists client names, not "IK
        Eng"'s own site) -- the page-text/distinctive-token checks
        above can't catch this class at all, because "IK Eng Ltd" has
        no word >=4 characters once "Ltd" is stripped
        (_shares_distinctive_token's own "insufficient signal, don't
        reject" rule then lets ANY page through). This asks the same
        grounded question discovery.candidate_validator's gate 4 asks
        of a discovery candidate -- whose name does this page actually
        claim to be, per its own heading/footer/copyright, not merely
        mention in passing -- via the identical prompt, then compares
        that against `company_name` with
        deduplication.name_utils.names_plausibly_corroborate (handles
        the "IK Eng Ltd"-style short-name gap the check above can't).

        Absence of a clean extraction (LLM found no stated name, or
        the call itself failed/returned no key -- LLMClient.
        complete_json() never raises, only returns None) is treated as
        insufficient signal, not a rejection -- same discipline as
        every other gate in this codebase: many real product-catalogue
        homepages never state the company name in the first ~8,000
        characters at all, and the fuzzy/distinctive-token checks
        already run are real evidence on their own that shouldn't be
        thrown out just because this extra check found nothing either
        way."""
        try:
            extracted = self.llm_client.complete_json(
                GROUNDED_COMPANY_NAME_EXTRACTION_SYSTEM_PROMPT,
                f"Website page content:\n\n{page_text[:20_000]}",
            )
        except Exception as e:
            logger.warning("company_website_finder: grounded-name extraction failed: %s", e)
            return True

        if not isinstance(extracted, dict):
            return True
        extracted_name = extracted.get("company_name")
        if not isinstance(extracted_name, str) or not extracted_name.strip():
            return True

        return names_plausibly_corroborate(company_name, extracted_name.strip())
