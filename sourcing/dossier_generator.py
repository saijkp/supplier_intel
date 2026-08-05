"""
sourcing/dossier_generator.py

Generates the detailed per-supplier procurement checklist assessment a
sourcing brief asks for -- OEM/ODM capability, factory & manufacturing
processes, engineering & testing capability, export experience,
suitability for the buyer's stated annual volume, and a payment-terms
assessment. Same grounded-only-JSON-prompt pattern as
verification_ai/narrative_generator.py (positive observations only,
must be traceable to the evidence given, omit rather than infer), but
built from a richer evidence bundle: verification_ai.CrossCheckResult's
sub-checks (reused, not duplicated) PLUS capability findings (with
their own evidence quotes and source URLs from
verification/capability_extractor.py) PLUS the supplier's own
payment_terms_offered/incoterms_supported PLUS the brief's stated
application/specifications/annual volume.

`verification_status` is deliberately NOT asked of the LLM -- it's
computed here, in plain Python, from which cross-check sub-checks
actually returned a real signal, so it stays auditable and
reproducible the same way verification_ai.confidence_scorer's
rule-based score does, rather than being one more thing the model
could get subtly wrong.

Never raises -- same contract as every AI-call-site in this codebase.
A missing/unusable dossier degrades gracefully (None) rather than
blocking sourcing_agent.SourcingAgentService.run().
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from llm.client import LLMClient
from sourcing.schemas import StructuredBrief, SourcingDossier
from verification_ai.cross_checker import CrossCheckResult

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are assessing a manufacturer against a buyer's procurement checklist, based ONLY on the structured evidence provided below (cross-check sub-signals, capability findings with their own quoted evidence, payment terms/incoterms on file, and the buyer's stated requirements). You have no other knowledge of this specific company -- do not use anything you might otherwise know or assume about a company with this name.

Rules, strictly enforced:
1. Base every statement ONLY on the evidence given below. Never invent facts, capabilities, certifications, factory details, or history not present in the evidence.
2. If the evidence is insufficient to support a section, say so explicitly in plain language (e.g. "No evidence of in-house tooling was found on the supplier's own website") rather than guessing or padding with generic language.
3. A capability finding's "relationship" field matters: "in_house" is a stronger claim than "subcontracted" or "asserted" -- reflect that distinction in your wording, don't flatten it.
4. For annual_volume_suitability: only assess plausibility against the buyer's stated annual volume using factory size/capability evidence given -- if no annual volume was stated by the buyer, or no relevant evidence exists, say so rather than guessing a production capacity.
5. For payment_terms_assessment: compare the buyer's preferred payment terms (if stated) against the supplier's own payment_terms_offered/incoterms_supported evidence -- if neither is available, say so.

Return ONLY a JSON object with exactly these keys, no other text, each a 1-3 sentence plain-English assessment:
{
  "oem_odm_capability": "...",
  "factory_manufacturing_processes": "...",
  "engineering_testing_capability": "...",
  "export_experience": "...",
  "annual_volume_suitability": "...",
  "payment_terms_assessment": "..."
}"""

_VERDICT_LABELS = {"True": "CONFIRMED", "False": "NOT CONFIRMED", "None": "NO SIGNAL"}


def _verification_status(cross_check_result: CrossCheckResult) -> str:
    """Deterministic, not LLM-judged -- see module docstring. 'verified'
    needs several independent signals AND no detected inconsistency;
    'unverified' means literally nothing corroborated; everything else
    is 'partially verified'."""
    signals = [c for c in cross_check_result.sub_checks if c.verdict is not None]
    if not signals:
        return "unverified"
    if len(signals) >= 3 and not cross_check_result.inconsistencies:
        return "verified"
    return "partially verified"


def _build_evidence_text(
    supplier: Dict[str, Any], brief: StructuredBrief,
    cross_check_result: CrossCheckResult, capability_findings: List[Dict[str, Any]],
) -> str:
    lines = [
        f"Company: {supplier.get('canonical_name', 'Unknown')}",
        f"Country: {supplier.get('country', 'Unknown')}",
        "",
        "Buyer's stated requirements:",
        f"- Product: {brief.product}",
    ]
    if brief.application:
        lines.append(f"- Application: {brief.application}")
    if brief.key_specifications:
        lines.append(f"- Key specifications: {', '.join(brief.key_specifications)}")
    if brief.annual_volume:
        lines.append(f"- Stated annual volume: {brief.annual_volume}")
    if brief.preferred_payment_terms:
        lines.append(f"- Preferred payment terms: {brief.preferred_payment_terms}")

    lines.append("")
    lines.append("Cross-check sub-signals:")
    for check in cross_check_result.sub_checks:
        label = _VERDICT_LABELS.get(str(check.verdict), "NO SIGNAL")
        lines.append(f"- {check.name}: {label} -- {check.detail}")
    if cross_check_result.inconsistencies:
        lines.append("")
        lines.append("Detected inconsistencies:")
        for inconsistency in cross_check_result.inconsistencies:
            lines.append(f"- {inconsistency}")

    lines.append("")
    if capability_findings:
        lines.append("Capability findings from the supplier's own website (relationship / confidence / evidence):")
        for finding in capability_findings:
            term = finding.get("canonical_term") or finding.get("reported_term")
            relationship = finding.get("relationship", "asserted")
            confidence = finding.get("confidence")
            evidence = finding.get("evidence", "")
            lines.append(f"- {term} ({relationship}, confidence {confidence}): \"{evidence}\"")
    else:
        lines.append("Capability findings from the supplier's own website: none on file.")

    lines.append("")
    payment_terms_offered = supplier.get("payment_terms_offered") or []
    incoterms_supported = supplier.get("incoterms_supported") or []
    lines.append(f"Payment terms on file: {', '.join(payment_terms_offered) if payment_terms_offered else 'none on file'}")
    lines.append(f"Incoterms on file: {', '.join(incoterms_supported) if incoterms_supported else 'none on file'}")

    return "\n".join(lines)


def _clean_text(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


class SourcingDossierGenerator:

    def __init__(self, llm_client: Optional[LLMClient] = None):
        self.llm_client = llm_client or LLMClient()

    def generate(
        self, supplier: Dict[str, Any], brief: StructuredBrief,
        cross_check_result: CrossCheckResult, capability_findings: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[SourcingDossier]:
        capability_findings = capability_findings or []
        evidence_text = _build_evidence_text(supplier, brief, cross_check_result, capability_findings)
        raw = self.llm_client.complete_json(SYSTEM_PROMPT, evidence_text, max_tokens=1200)
        if not isinstance(raw, dict):
            return None

        fields = {
            key: _clean_text(raw.get(key))
            for key in (
                "oem_odm_capability", "factory_manufacturing_processes",
                "engineering_testing_capability", "export_experience",
                "annual_volume_suitability", "payment_terms_assessment",
            )
        }
        if not any(fields.values()):
            return None

        fallback = "No evidence available to assess this."
        return SourcingDossier(
            oem_odm_capability=fields["oem_odm_capability"] or fallback,
            factory_manufacturing_processes=fields["factory_manufacturing_processes"] or fallback,
            engineering_testing_capability=fields["engineering_testing_capability"] or fallback,
            export_experience=fields["export_experience"] or fallback,
            annual_volume_suitability=fields["annual_volume_suitability"] or fallback,
            payment_terms_assessment=fields["payment_terms_assessment"] or fallback,
            verification_status=_verification_status(cross_check_result),
        )
