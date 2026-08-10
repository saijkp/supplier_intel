"""
verification_ai/narrative_generator.py

Generates the AI-written company summary/strengths/risks/suitable-
customer-types the redesign brief asks for -- nothing in this codebase
produced this kind of narrative before (confirmed: grepping the whole
repo for "summary"/"strengths"/"risks" before this found only
ManufacturerVerifier's short FIXED-SENTENCE summary, never AI-generated
prose). Uses the shared llm.client.LLMClient, same grounded-only prompt
discipline verification.capability_extractor.py's system prompt already
established (positive observations only, must be traceable to the
evidence given, omit rather than infer) -- applied here to
verification_ai.cross_checker's structured sub-check evidence instead
of raw webpage text.

Never raises -- same contract as every AI-call-site in this codebase
(LLMClient itself already never raises; this module's own job is just
to validate the shape of what comes back and return None on anything
unusable, so a bad/missing narrative degrades gracefully rather than
blocking VerificationService.verify()).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from llm.client import LLMClient
from verification_ai.confidence_scorer import CHECK_WEIGHTS
from verification_ai.cross_checker import CrossCheckResult

logger = logging.getLogger(__name__)

# linkedin_presence and phone_format answer "does this exist and is it
# formatted plausibly" -- not "can this supplier actually make the
# product." Real bug this guards against: a correctly-formatted phone
# number with every other check at NO SIGNAL produced narrative text
# like "indicating some level of operational legitimacy" -- prose
# analysis over what is, substantively, an empty evidence base.
# phone_format is already excluded from CHECK_WEIGHTS (see
# confidence_scorer.py's own comment) but was still being shown to the
# narrative LLM as evidence; linkedin_presence IS weighted (it's a real,
# if weak, signal for the numeric ai_confidence_score) but must not be
# what a narrative gets written from either. Excluded from the evidence
# text entirely below (_build_evidence_text) and from what counts as
# "enough signal to narrate at all" (generate()) -- neither change
# touches CrossChecker/ConfidenceScorer, so ai_confidence_score's number
# is unaffected.
_EXISTENCE_ONLY_CHECKS = frozenset({"linkedin_presence", "phone_format"})
_SUBSTANTIVE_CHECK_NAMES = frozenset(name for name, _ in CHECK_WEIGHTS) - _EXISTENCE_ONLY_CHECKS

INSUFFICIENT_EVIDENCE_SUMMARY = "Insufficient evidence — not assessed."

SYSTEM_PROMPT = """You are assessing a B2B supplier's profile for a procurement buyer, based ONLY on the structured evidence provided below (cross-check sub-signals and any detected inconsistencies). You have no other knowledge of this specific company -- do not use anything you might otherwise know or assume about a company with this name.

Rules, strictly enforced:
1. Base every statement ONLY on the evidence given below. Never invent facts, certifications, capabilities, financials, or history not present in the evidence.
2. If the evidence is insufficient to support a strengths, risks, or suitable_customer_types section, say so explicitly (e.g. an empty list, or a summary noting the limitation) rather than guessing or padding with generic language.
3. A "NO SIGNAL" sub-check means that check could not be run or found nothing either way -- it is not itself evidence of a strength or a risk. Do not treat absence of a signal as either.
4. A "NOT CONFIRMED" sub-check and any listed inconsistency ARE real, evidence-backed risks -- always reflect them in the risks list, do not soften or omit them.
5. Do not restate raw sub-check names verbatim; translate them into plain, buyer-relevant language.

Return ONLY a JSON object with exactly these keys, no other text:
{
  "summary": "2-4 sentence plain-English summary of what this evidence supports about this supplier",
  "strengths": ["short evidence-backed strength", "..."],
  "risks": ["short evidence-backed risk", "..."],
  "suitable_customer_types": ["short description of a buyer type this evidence suggests is a good fit"]
}"""

_VERDICT_LABELS = {"True": "CONFIRMED", "False": "NOT CONFIRMED", "None": "NO SIGNAL"}


@dataclass
class NarrativeResult:
    summary: str
    strengths: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    suitable_customer_types: List[str] = field(default_factory=list)
    model_used: str = ""


def _build_evidence_text(supplier: Dict[str, Any], cross_check_result: CrossCheckResult, confidence_score: int) -> str:
    lines = [
        f"Company: {supplier.get('canonical_name', 'Unknown')}",
        f"Country: {supplier.get('country', 'Unknown')}",
        f"Composite platform score: {supplier.get('composite_score', 'N/A')}/100",
        f"AI cross-check confidence score: {confidence_score}/100",
        "",
        "Cross-check sub-signals:",
    ]
    for check in cross_check_result.sub_checks:
        if check.name in _EXISTENCE_ONLY_CHECKS:
            # Existence-only signal (see module docstring) -- never
            # shown to the narrative LLM, so it structurally cannot
            # write commentary "from" a formatted phone number or a
            # LinkedIn page existing.
            continue
        label = _VERDICT_LABELS.get(str(check.verdict), "NO SIGNAL")
        lines.append(f"- {check.name}: {label} -- {check.detail}")
    if cross_check_result.inconsistencies:
        lines.append("")
        lines.append("Detected inconsistencies:")
        for inconsistency in cross_check_result.inconsistencies:
            lines.append(f"- {inconsistency}")
    return "\n".join(lines)


def _clean_str_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


class NarrativeGenerator:

    def __init__(self, llm_client: Optional[LLMClient] = None):
        self.llm_client = llm_client or LLMClient()

    def generate(
        self, supplier: Dict[str, Any], cross_check_result: CrossCheckResult, confidence_score: int,
    ) -> Optional[NarrativeResult]:
        by_name = {c.name: c.verdict for c in cross_check_result.sub_checks}
        has_substantive_signal = any(
            by_name.get(name) is not None for name in _SUBSTANTIVE_CHECK_NAMES
        )
        if not has_substantive_signal:
            # Every weighted check that actually speaks to manufacturing
            # capability/identity is NO SIGNAL -- linkedin_presence/
            # phone_format alone (existence checks, not capability
            # signals) must never be enough to justify writing narrative
            # commentary, so this returns a real, explicit result
            # (actively overwriting any stale narrative from a
            # previous, better-evidenced run) rather than calling the
            # LLM at all.
            return NarrativeResult(summary=INSUFFICIENT_EVIDENCE_SUMMARY, model_used="")

        evidence_text = _build_evidence_text(supplier, cross_check_result, confidence_score)
        raw = self.llm_client.complete_json(SYSTEM_PROMPT, evidence_text)
        if not isinstance(raw, dict):
            return None

        summary = raw.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            return None

        return NarrativeResult(
            summary=summary.strip(),
            strengths=_clean_str_list(raw.get("strengths")),
            risks=_clean_str_list(raw.get("risks")),
            suitable_customer_types=_clean_str_list(raw.get("suitable_customer_types")),
            model_used=self.llm_client.text_model,
        )
