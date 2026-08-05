"""
discovery/candidate_validator.py

The only LLM call in Discovery Service -- and even here, the model only
ever READS a real fetched page, never generates a company from nothing.
For each candidate domain: fetch the real page, ask the LLM to extract
(only if actually present in the text) the company's self-stated name/
country, then cross-check that extracted name against the ORIGINAL
search result's title/snippet using the same rapidfuzz validation
scrapers.company_website_finder.CompanyWebsiteFinder.find_website()
already uses (reused via deduplication.name_utils.normalise_company_name,
not reimplemented).

A candidate is "validated" only if every gate passes:
1. Real SerpAPI search hit (candidate_extractor.py, upstream of this).
2. Real fetch of the candidate site succeeded.
3. The LLM found a company name explicitly stated in the page text
   (grounded-only prompt discipline -- same "quote-required, omit
   rather than infer" rules verification.capability_extractor.py's
   system prompt already established).
4. That extracted name fuzzy-matches the original search result
   (proves the fetched page is genuinely about the company the search
   surfaced, not an unrelated site that happens to share the domain).
5. The fetched page text actually mentions the searched product term
   -- a second, DETERMINISTIC keyword check, not another LLM call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from rapidfuzz import fuzz

from deduplication.name_utils import normalise_company_name
from discovery.candidate_extractor import Candidate
from llm.client import LLMClient

logger = logging.getLogger(__name__)

# Same threshold scrapers.company_website_finder's own
# _DEFAULT_MIN_NAME_SIMILARITY uses for the analogous "is this really
# the same company" question.
_NAME_MATCH_THRESHOLD = 55.0

# Mechanical, country-agnostic trader exclusion -- the codebase's only
# other trader signal (verification.manufacturer_verifier, via Qichacha
# business-scope data) only exists for China-registered companies, so a
# global "exclude traders/distributors/resellers" filter (sourcing.
# SourcingAgentService's own brief explicitly asks for this) needs a
# second signal that works for any country. Deliberately specific
# self-declarations, not the bare word "trading" -- a genuine
# manufacturer's page can easily mention "trading partners" or similar
# without being a trading company itself, so precision matters more
# than recall here: a false negative just means Qichacha/ManufacturerVerifier
# gets the final say later, a false positive silently drops a real
# manufacturer.
_TRADER_SELF_DECLARATION_PHRASES: tuple = (
    "we are a trading company",
    "we are a professional trading company",
    "we are a distributor",
    "we are a reseller",
    "we are an import and export company",
    "we are a sourcing agent",
    "we are a buying agent",
    "trading company specializing in",
    "we do not manufacture",
    "we don't manufacture",
)


def _find_trader_self_declaration(page_text: str) -> str | None:
    """The first matched phrase from _TRADER_SELF_DECLARATION_PHRASES
    found (case-insensitively) in `page_text`, or None."""
    haystack = page_text.lower()
    for phrase in _TRADER_SELF_DECLARATION_PHRASES:
        if phrase in haystack:
            return phrase
    return None

SYSTEM_PROMPT = """You are reading the text of a company website. Extract ONLY what is explicitly stated in the text below -- never guess, infer, or fill in based on typical industry patterns or the domain name.

Rules, strictly enforced:
1. Only report a company name if it is explicitly stated in the text (e.g. in a heading, footer, "About Us" section, or copyright notice).
2. If the company name is not clearly stated, return null for company_name -- do not guess it from the domain or from context.
3. Only report a country if it is explicitly stated (an address, a phone country code mentioned as text, "based in X").
4. Never invent certifications, products, or history not present in the text -- this task only asks for name and country.

Return ONLY a JSON object with exactly these keys, no other text:
{
  "company_name": "the exact company name as stated in the text, or null if not clearly stated",
  "country": "the country as stated in the text, or null if not clearly stated"
}"""


@dataclass
class ValidationResult:
    candidate: Candidate
    validated: bool
    extracted_name: Optional[str]
    extracted_country: Optional[str]
    name_match_score: Optional[float]
    reason: str


class CandidateValidator:

    def __init__(self, website_fetcher: Any, llm_client: Optional[LLMClient] = None):
        # Anything with `.fetch(domain) -> result with .success/.pages[0].text`
        # -- OwnWebsiteScraper or collection.SiteCollector both qualify,
        # same injectable seam the rest of this codebase already uses.
        self.website_fetcher = website_fetcher
        self.llm_client = llm_client or LLMClient()

    def validate(self, candidate: Candidate, product_term: str) -> ValidationResult:
        try:
            fetch_result = self.website_fetcher.fetch(candidate.domain)
        except Exception as e:
            logger.warning("discovery: fetch failed for %s: %s", candidate.domain, e)
            return ValidationResult(candidate, False, None, None, None, f"fetch failed: {e}")

        if not fetch_result.success or not fetch_result.pages:
            return ValidationResult(
                candidate, False, None, None, None,
                f"could not fetch candidate site: {getattr(fetch_result, 'error', 'unknown error')}",
            )

        page_text = fetch_result.pages[0].text
        if not page_text or not page_text.strip():
            return ValidationResult(candidate, False, None, None, None, "fetched page had no readable text")

        extracted = self.llm_client.complete_json(SYSTEM_PROMPT, f"Website page content:\n\n{page_text[:20_000]}")
        if not isinstance(extracted, dict):
            return ValidationResult(
                candidate, False, None, None, None, "LLM extraction failed or returned invalid JSON",
            )

        extracted_name = extracted.get("company_name")
        extracted_country = extracted.get("country") if isinstance(extracted.get("country"), str) else None
        if not isinstance(extracted_name, str) or not extracted_name.strip():
            return ValidationResult(
                candidate, False, None, extracted_country, None, "no company name found in page text",
            )
        extracted_name = extracted_name.strip()

        haystack = f"{candidate.title} {candidate.snippet}".lower()
        normalised_extracted = normalise_company_name(extracted_name)
        score = fuzz.partial_ratio(normalised_extracted, haystack)
        if score < _NAME_MATCH_THRESHOLD:
            return ValidationResult(
                candidate, False, extracted_name, extracted_country, score,
                f"extracted name '{extracted_name}' does not match the original search result (score={score:.0f})",
            )

        if product_term.lower() not in page_text.lower():
            return ValidationResult(
                candidate, False, extracted_name, extracted_country, score,
                f"fetched page text does not mention the searched term '{product_term}'",
            )

        self_declared_trader = _find_trader_self_declaration(page_text)
        if self_declared_trader:
            return ValidationResult(
                candidate, False, extracted_name, extracted_country, score,
                f"page self-identifies as a trading company/distributor (matched phrase: "
                f"'{self_declared_trader}') -- excluded, not a manufacturer",
            )

        return ValidationResult(
            candidate, True, extracted_name, extracted_country, score,
            f"validated: name corroborated (score={score:.0f}), product term found on page",
        )
