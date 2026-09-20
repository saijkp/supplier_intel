"""
discovery/input_classifier.py

Classifies a short piece of free text a buyer typed into Find
Suppliers' single search box as either a specific COMPANY name or a
PRODUCT/CATEGORY search term -- frontend/index.html's own
findClassifyInput() already handles the unambiguous cases for free
(a URL, or an inline "find 15 ..." target count implying a product
search); this covers the genuinely ambiguous remainder, which used to
require the buyer to click one of two manual buttons ("It's a
company" / "It's a product/category") on every such query. Automated
here so that manual choice is needed only when the buyer disagrees
with the model's own read -- never removed outright, since a model
guess can be wrong and the buyer's own correction must always win.

One tiny, cheap LLM call (max_tokens=20, nothing else asked for) --
deliberately not reusing sourcing.brief_parser.BriefParser, which asks
for a much larger structured extraction (application, key specs,
countries, certifications, quantities, payment terms) this single
yes/no question has no use for and would needlessly pay tokens to
generate. Never raises -- returns None on any failure (missing text,
LLM call failure, unusable response), same never-raises contract every
other LLM call site in this codebase follows; callers must treat None
as "couldn't classify," never silently default it to either kind.
"""

from __future__ import annotations

from typing import Optional

from llm.client import LLMClient

SYSTEM_PROMPT = """You are classifying a short piece of text a buyer typed into a supplier-search box. Decide whether it is most likely:
- "company": the name of a SPECIFIC company/business (e.g. "Acme Trailer Co", "JCB", "Beta Bearings Ltd")
- "product": a PRODUCT, PART, or CATEGORY search term (e.g. "trailer axle", "injection moulding manufacturers", "LED marker lights")

Rules:
1. Base your answer only on how the text itself reads -- you have no other context about what the buyer meant, and must not guess based on anything else.
2. If genuinely unsure, prefer "product" -- a category search is the safer default for an ambiguous short noun phrase (worst case, it returns few/no results rather than searching for the wrong company).

Return ONLY a JSON object with exactly this key, no other text:
{"kind": "company" | "product"}"""

_VALID_KINDS = ("company", "product")


def classify_search_input(text: str, llm_client: Optional[LLMClient] = None) -> Optional[str]:
    """Returns "company", "product", or None (see module docstring for
    when and why)."""
    text = (text or "").strip()
    if not text:
        return None

    client = llm_client or LLMClient()
    try:
        result = client.complete_json(SYSTEM_PROMPT, text, max_tokens=20)
    except Exception:  # noqa: BLE001 -- never raise into a caller that just wants a best-effort classification
        return None

    if not isinstance(result, dict):
        return None
    kind = result.get("kind")
    return kind if kind in _VALID_KINDS else None
