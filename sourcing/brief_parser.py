"""
sourcing/brief_parser.py

Turns one free-text sourcing brief ("find 20 genuine winch manufacturers
for off-road trailer applications, ISO 9001, prioritise China then
India, annual volume 5,000pcs, 30-day payment terms") into the
structured parameters sourcing.sourcing_agent.SourcingAgentService.run()
actually drives Discovery Service with. One LLMClient.complete_json()
call, same grounded-extraction discipline discovery/
candidate_validator.py's system prompt already established for this
codebase: extract only what the buyer actually said, never invent a
requirement, country, certification, or quantity they didn't state.

Unlike most extractors in this codebase, `parse()` RAISES
(BriefParsingError) rather than returning None on failure -- a sourcing
run genuinely cannot proceed without at least a product, so the caller
(SourcingAgentService.run()) is meant to catch this and record the
sourcing_runs row as 'failed' with a clear reason, rather than silently
doing nothing.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from llm.client import LLMClient
from sourcing.schemas import StructuredBrief
from verification.capability_vocabulary import map_to_canonical

logger = logging.getLogger(__name__)

# A brief asking for hundreds of suppliers would blow through the
# examine-ceiling (target_count * max_multiplier) in cost terms before
# ever finishing -- capped regardless of what the buyer asked for.
MAX_TARGET_COUNT = 50
DEFAULT_TARGET_COUNT = 10

SYSTEM_PROMPT = """You are extracting a structured supplier-sourcing brief from a buyer's free-text request. Extract ONLY what the buyer actually stated -- never invent a requirement, country, certification, specification, or quantity they didn't mention.

Rules, strictly enforced:
1. "product" is the physical thing being sourced (e.g. "winch", "LED marker light"). If no product is stated, return null for product.
2. "application" and "key_specifications" -- only include what's explicitly stated (e.g. "for off-road trailers", "12V, 5000lb capacity"). Omit entirely if not mentioned -- do not guess typical specifications for the product.
3. "countries" -- only countries/regions explicitly named, in the order the buyer stated or implied a preference (e.g. "prioritise China then India" -> ["China", "India"]). Empty list if none stated.
4. "required_capabilities" -- certifications or capabilities explicitly required (e.g. "ISO 9001", "must have in-house tooling"), in the buyer's own words. Empty list if none stated.
5. "target_count" -- the NUMBER of suppliers the buyer wants sourced and enriched. If not stated, return null.
6. "annual_volume" -- the buyer's own stated volume/quantity for the deal itself (e.g. "5,000 pcs/year"). This is NOT the same thing as target_count -- do not confuse the number of suppliers wanted with the deal's order volume. Null if not stated.
7. "preferred_payment_terms" -- e.g. "30 day", "DDP", "FOB" -- only if explicitly stated. Null if not.

Return ONLY a JSON object with exactly these keys, no other text:
{
  "product": "string or null",
  "application": "string or null",
  "key_specifications": ["string", ...],
  "countries": ["string", ...],
  "required_capabilities": ["string", ...],
  "target_count": integer or null,
  "annual_volume": "string or null",
  "preferred_payment_terms": "string or null"
}"""


class BriefParsingError(Exception):
    """Raised when a brief cannot be turned into a usable
    StructuredBrief -- always caught by SourcingAgentService.run() and
    recorded as a failed sourcing_runs row, never left to propagate to
    an HTTP caller as a 500."""


def _clean_str_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _clean_optional_str(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


class BriefParser:

    def __init__(self, llm_client: Optional[LLMClient] = None):
        self.llm_client = llm_client or LLMClient()

    def parse(self, brief_text: str) -> StructuredBrief:
        if not brief_text or not brief_text.strip():
            raise BriefParsingError("The sourcing brief was empty.")

        raw = self.llm_client.complete_json(SYSTEM_PROMPT, brief_text)
        if not isinstance(raw, dict):
            raise BriefParsingError(
                "Could not extract a structured brief from that request -- "
                "try rephrasing with a clearer product and how many suppliers you need."
            )

        product = _clean_optional_str(raw.get("product"))
        if not product:
            raise BriefParsingError(
                "No product could be identified in the request -- "
                "please state what you're sourcing (e.g. \"winch\", \"LED marker light\")."
            )

        raw_capabilities = _clean_str_list(raw.get("required_capabilities"))
        required_capabilities: List[str] = []
        unmapped_terms: List[str] = []
        for term in raw_capabilities:
            mapped = map_to_canonical(term)
            if mapped is not None:
                if mapped.canonical not in required_capabilities:
                    required_capabilities.append(mapped.canonical)
            else:
                unmapped_terms.append(term)

        target_count = raw.get("target_count")
        if not isinstance(target_count, int) or isinstance(target_count, bool) or target_count <= 0:
            target_count = DEFAULT_TARGET_COUNT
        target_count = min(target_count, MAX_TARGET_COUNT)

        return StructuredBrief(
            product=product,
            application=_clean_optional_str(raw.get("application")),
            key_specifications=_clean_str_list(raw.get("key_specifications")),
            countries=_clean_str_list(raw.get("countries")),
            required_capabilities=required_capabilities,
            unmapped_terms=unmapped_terms,
            target_count=target_count,
            annual_volume=_clean_optional_str(raw.get("annual_volume")),
            preferred_payment_terms=_clean_optional_str(raw.get("preferred_payment_terms")),
        )
