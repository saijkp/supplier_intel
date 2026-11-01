from decimal import Decimal

from sourcing.quote_validation import (
    MasterRFQ, SupplierQuote, validate_quote, comparison_rows,
)


def rfq():
    return MasterRFQ(
        rfq_id="IK-001", version=1, quantity=Decimal("100"),
        mandatory_specs={"material": "aluminium"},
    )


def full_quote(**overrides):
    fields = {
        "unit_price": 125, "currency": "CNY", "quoted_quantity": 100,
        "moq": 50, "incoterm": "FOB", "named_place": "Shenzhen",
        "production_lead_days": 25, "payment_terms": "30/70",
        "valid_until": "2026-12-31",
    }
    fields.update(overrides)
    evidence = {key: f"Supplier confirmed {key}" for key in fields}
    evidence["spec:material"] = "Aluminium body"
    return SupplierQuote(
        supplier_id="S1", rfq_id="IK-001", rfq_version=1,
        fields=fields, quoted_specs={"material": "aluminium"},
        evidence=evidence,
    )


def test_complete_quote():
    assert validate_quote(rfq(), full_quote()).status == "comparable"


def test_missing_moq_generates_followup():
    q = full_quote(moq=None)
    review = validate_quote(rfq(), q)
    assert review.status == "clarification_required"
    assert "moq" in review.missing_fields
    assert any("minimum order quantity" in x.lower() for x in review.clarification_questions)


def test_spec_deviation_blocks_comparison():
    q = full_quote()
    q.quoted_specs["material"] = "steel"
    assert validate_quote(rfq(), q).status == "non_compliant"


def test_quantity_mismatch_blocks_comparison():
    assert validate_quote(rfq(), full_quote(quoted_quantity=200)).status == "non_compliant"


def test_missing_evidence_prevents_comparable_label():
    q = full_quote()
    q.evidence.pop("unit_price")
    assert validate_quote(rfq(), q).status == "clarification_required"


def test_invalid_price_blocks_comparison():
    assert validate_quote(rfq(), full_quote(unit_price=-5)).status == "non_compliant"


def test_compare_does_not_rank_mixed_terms():
    a = full_quote()
    b = full_quote(unit_price=99, incoterm="EXW")
    rows = comparison_rows(rfq(), [a, b])
    assert len(rows) == 2
    assert "rank" not in rows[0]
    assert rows[1]["incoterm"] == "EXW"
