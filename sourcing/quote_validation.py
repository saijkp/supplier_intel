"""Evidence-first RFQ quote validation and targeted clarification.

Pure business logic: no network requests, messaging, database writes or LLM calls.
Do not consider a supplier quote firm merely because these checks pass.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from decimal import Decimal, InvalidOperation
from datetime import date
import re
from typing import Any, Dict, List, Optional

COMMERCIAL_REQUIRED = (
    "unit_price", "currency", "quoted_quantity", "moq",
    "incoterm", "named_place", "production_lead_days",
    "payment_terms", "valid_until",
)
SUPPORTED_INCOTERMS = {"EXW", "FCA", "FOB", "CFR", "CIF", "CPT", "CIP", "DAP", "DPU", "DDP"}


@dataclass
class MasterRFQ:
    rfq_id: str
    version: int
    quantity: Decimal
    mandatory_specs: Dict[str, str]
    required_fields: List[str] = field(default_factory=lambda: list(COMMERCIAL_REQUIRED))


@dataclass
class SupplierQuote:
    supplier_id: str
    rfq_id: str
    rfq_version: int
    fields: Dict[str, Any]
    quoted_specs: Dict[str, str]
    evidence: Dict[str, str] = field(default_factory=dict)
    # Evidence maps field names (including "spec:<name>") to source excerpts.
    deviations: List[str] = field(default_factory=list)


@dataclass
class QuoteReview:
    supplier_id: str
    status: str
    missing_fields: List[str]
    deviations: List[str]
    errors: List[str]
    clarification_questions: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def validate_quote(rfq: MasterRFQ, quote: SupplierQuote) -> QuoteReview:
    """Never infer missing facts; mismatched specifications block comparability."""
    missing: List[str] = []
    errors: List[str] = []
    deviations = list(quote.deviations)

    if quote.rfq_id != rfq.rfq_id or quote.rfq_version != rfq.version:
        errors.append("Quote references the wrong RFQ or an outdated RFQ version")

    for key in rfq.required_fields:
        if _is_missing(quote.fields.get(key)):
            missing.append(key)
        elif not str(quote.evidence.get(key, '')).strip():
            missing.append(key + "_evidence")

    for key, expected in rfq.mandatory_specs.items():
        actual = quote.quoted_specs.get(key)
        if _is_missing(actual):
            missing.append("spec:" + key)
        elif str(actual).strip().casefold() != str(expected).strip().casefold():
            deviations.append(f"{key}: requested {expected!r}, quoted {actual!r}")
        elif not str(quote.evidence.get("spec:" + key, '')).strip():
            missing.append("spec:" + key + "_evidence")

    for key in ("unit_price", "quoted_quantity", "moq"):
        if not _is_missing(quote.fields.get(key)):
            n = _decimal(quote.fields[key])
            if n is None or n <= 0:
                errors.append(f"{key} must be a positive number")

    if not _is_missing(quote.fields.get("production_lead_days")):
        lead = _decimal(quote.fields["production_lead_days"])
        if lead is None or lead < 0:
            errors.append("production_lead_days must be non-negative")

    if not _is_missing(quote.fields.get("incoterm")):
        if str(quote.fields["incoterm"]).strip().upper() not in SUPPORTED_INCOTERMS:
            errors.append("Unsupported Incoterm")

    moq = _decimal(quote.fields.get("moq"))
    currency = quote.fields.get('currency')
    if not _is_missing(currency) and not re.fullmatch(r'[A-Z]{3}', str(currency)):
        errors.append('currency must be a three-letter uppercase currency code')
    validity = quote.fields.get('valid_until')
    if not _is_missing(validity):
        try:
            expiry = date.fromisoformat(str(validity))
            if expiry < date.today():
                errors.append('Quotation validity date has expired')
        except ValueError:
            errors.append('valid_until must be a valid YYYY-MM-DD date')
    quoted_quantity = _decimal(quote.fields.get("quoted_quantity"))
    if moq and quoted_quantity and quoted_quantity < moq:
        errors.append("Quoted quantity falls below supplier MOQ")
    if quoted_quantity and quoted_quantity != rfq.quantity:
        deviations.append(f"quantity: requested {rfq.quantity}, quoted {quoted_quantity}")

    # A mismatch is never silently normalised using guessed alternatives.
    status = (
        "non_compliant" if errors or deviations else
        "clarification_required" if missing else "comparable"
    )
    questions = clarification_questions(missing, deviations, errors)
    return QuoteReview(
        quote.supplier_id, status, sorted(set(missing)), deviations, errors, questions
    )


def clarification_questions(
    missing_fields: List[str], deviations: List[str], errors: List[str]
) -> List[str]:
    """Generate explicit, reviewable questions, not an automatically sent message."""
    names = {
        "unit_price": "Confirm the unit price.",
        "currency": "Confirm the quotation currency.",
        "quoted_quantity": "Confirm the quantity covered by this price.",
        "moq": "Confirm minimum order quantity (MOQ).",
        "incoterm": "Confirm the Incoterm (e.g. EXW, FCA or FOB).",
        "named_place": "Confirm the named delivery place or port.",
        "production_lead_days": "Confirm production lead time in days.",
        "payment_terms": "Confirm payment terms.",
        "valid_until": "Confirm quotation validity date.",
    }
    questions = []
    for key in sorted(set(missing_fields)):
        if key.endswith("_evidence"):
            questions.append("Provide source confirmation for " + key[:-9] + ".")
        elif key.startswith("spec:"):
            questions.append("Confirm specification " + key[5:] + ".")
        else:
            questions.append(names.get(key, "Confirm " + key + "."))
    for item in deviations:
        questions.append("Please confirm or correct deviation: " + item)
    for item in errors:
        questions.append("Please resolve: " + item)
    return questions


def comparison_rows(rfq: MasterRFQ, quotes: List[SupplierQuote]) -> List[Dict[str, Any]]:
    """Return audit-friendly rows; never rank mixed terms or currencies by price."""
    rows = []
    for quote in quotes:
        review = validate_quote(rfq, quote)
        rows.append({
            "supplier_id": quote.supplier_id,
            "rfq_id": quote.rfq_id,
            "status": review.status,
            "unit_price": quote.fields.get("unit_price"),
            "currency": quote.fields.get("currency"),
            "incoterm": quote.fields.get("incoterm"),
            "named_place": quote.fields.get("named_place"),
            "quoted_quantity": quote.fields.get("quoted_quantity"),
            "missing_fields": review.missing_fields,
            "deviations": review.deviations,
            "errors": review.errors,
        })
    return rows
