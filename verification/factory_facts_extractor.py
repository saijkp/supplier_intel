"""
verification/factory_facts_extractor.py

Reads a supplier's already-collected own-website pages and extracts
factory-level facts a procurement manager actually cares about but
this codebase never captured before: production line count/type,
named machinery/equipment, and factory ownership (owned vs leased vs
shared vs unclear). `employee_count`/`factory_size_sqm`/
`year_established` already exist on the suppliers table (v1-v2); this
is the next tier of factory evidence, not a replacement for those.

Uses the shared `llm.client.LLMClient` -- same grounded-only-JSON-
prompt discipline `verification_ai/narrative_generator.py` and
`sourcing/dossier_generator.py` already establish (positive
observations only, must be traceable to the page text given, say so
explicitly rather than guess when evidence is missing). A single
JSON object is asked for, not an array (this is one assessment across
a supplier's page set, not N discrete findings like
`verification/capability_extractor.py`'s vocabulary-mapped output),
so `LLMClient.complete_json` is the closer template than
`CapabilityExtractor`'s own raw-array-parsing machinery.

`factory_ownership` is soft-corrected to "unclear" if the model
returns anything outside the four allowed values -- same deterministic
post-hoc validation discipline `capability_extractor.py`'s
`_EVIDENCE_MUST_CONTAIN` marker check already establishes for a
different failure mode (an LLM instruction is a request it can ignore;
a value-set check cannot be talked around).

Never raises -- same contract as every AI-call-site in this codebase.
A missing/unusable result degrades gracefully (None) rather than
blocking `verification/factory_facts_service.py`'s `find_facts()`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional

from llm.client import LLMClient

logger = logging.getLogger(__name__)

VALID_OWNERSHIP_VALUES = ("owned", "leased", "shared", "unclear")

SYSTEM_PROMPT = """You are a procurement analyst assessing a manufacturer's factory operations, based ONLY on the supplier's own website page content given below. You have no other knowledge of this specific company -- do not use anything you might otherwise know or assume about a company with this name.

Rules, strictly enforced:
1. Base every statement ONLY on the page content given below. Never invent production line counts, machinery names, or ownership details not present in the text.
2. If there is no evidence for a field, say so explicitly (e.g. "No evidence of production line count found on the supplier's own website") rather than guessing or padding with generic language.
3. For factory_ownership, use ONLY one of these four exact values: "owned" (the page states the company owns its factory/premises/land), "leased" (the page states the factory is rented/leased), "shared" (a shared facility, co-manufacturing arrangement, or shared workshop), "unclear" (no clear statement either way).

Return ONLY a JSON object with exactly these keys, no other text:
{
  "production_lines_notes": "1-3 sentence plain-English note on production line count/type, or an explicit statement that no evidence was found",
  "machinery_notes": "1-3 sentence plain-English note on named machinery/equipment, or an explicit statement that no evidence was found",
  "factory_ownership": "owned" | "leased" | "shared" | "unclear"
}"""


def _build_evidence_text(pages: List[Any]) -> Optional[str]:
    """Returns None if no page has any real text -- distinct from an
    empty string, since the header line alone would otherwise make an
    all-blank page set look non-empty to a bare truthiness/strip()
    check."""
    lines = []
    for page in pages:
        text = getattr(page, "text", "") or ""
        if not text.strip():
            continue
        lines.append(f"\n--- Page: {getattr(page, 'url', '')} ---")
        lines.append(text[:8000])
    if not lines:
        return None
    return "Supplier's own website page content:\n" + "\n".join(lines)


def _clean_text(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


@dataclass(frozen=True)
class FactoryFactsResult:
    production_lines_notes: str
    machinery_notes: str
    factory_ownership: str  # always one of VALID_OWNERSHIP_VALUES
    model_used: str = ""


class FactoryFactsExtractor:

    def __init__(self, llm_client: Optional[LLMClient] = None):
        self.llm_client = llm_client or LLMClient()

    def extract_from_pages(self, pages: List[Any]) -> Optional[FactoryFactsResult]:
        """Never raises -- returns None if there's no usable page text,
        the LLM call fails, or the response has no usable content
        (LLMClient.complete_json already returns None on any failure,
        so this only needs to validate shape)."""
        evidence_text = _build_evidence_text(pages)
        if not pages or evidence_text is None:
            return None

        raw = self.llm_client.complete_json(SYSTEM_PROMPT, evidence_text, max_tokens=800)
        if not isinstance(raw, dict):
            return None

        production_lines_notes = _clean_text(raw.get("production_lines_notes"))
        machinery_notes = _clean_text(raw.get("machinery_notes"))
        if not production_lines_notes and not machinery_notes:
            return None

        ownership_raw = raw.get("factory_ownership")
        if isinstance(ownership_raw, str) and ownership_raw.strip().lower() in VALID_OWNERSHIP_VALUES:
            ownership = ownership_raw.strip().lower()
        else:
            ownership = "unclear"  # soft-corrected -- see module docstring

        fallback = "No evidence available to assess this."
        return FactoryFactsResult(
            production_lines_notes=production_lines_notes or fallback,
            machinery_notes=machinery_notes or fallback,
            factory_ownership=ownership,
            model_used=self.llm_client.text_model,
        )
