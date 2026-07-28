"""
tests/test_capability_extractor.py

Tests for verification.capability_extractor.CapabilityExtractor and
assess_own_website_capability. Reuses the exact fake OpenAI-client
shape tests/test_manufacturer_verification.py already established for
FactoryPhotoVerifier/ProductMatcher, so no network call and no
OPENAI_API_KEY are needed to run these.
"""

from __future__ import annotations

import json

from verification.capability_extractor import (
    CapabilityExtractor,
    CapabilityFinding,
    RELATIONSHIP_ASSERTED,
    RELATIONSHIP_IN_HOUSE,
    RELATIONSHIP_SUBCONTRACTED,
    assess_own_website_capability,
    unmapped_terms,
)


# ═════════════════════════════════════════════════════════════
# Fake OpenAI-style client — identical shape to
# tests/test_manufacturer_verification.py's FakeOpenAIClient
# ═════════════════════════════════════════════════════════════

class FakeMessage:
    def __init__(self, text):
        self.content = text


class FakeChoice:
    def __init__(self, text):
        self.message = FakeMessage(text)


class FakeCompletion:
    def __init__(self, text):
        self.choices = [FakeChoice(text)]


class FakeChatCompletionsAPI:
    def __init__(self, response_text=None, raise_error=None):
        self._response_text = response_text
        self._raise_error = raise_error
        self.last_call_kwargs = None

    def create(self, **kwargs):
        self.last_call_kwargs = kwargs
        if self._raise_error:
            raise self._raise_error
        return FakeCompletion(self._response_text)


class FakeChatAPI:
    def __init__(self, response_text=None, raise_error=None):
        self.completions = FakeChatCompletionsAPI(response_text=response_text, raise_error=raise_error)


class FakeOpenAIClient:
    def __init__(self, response_text=None, raise_error=None):
        self.chat = FakeChatAPI(response_text=response_text, raise_error=raise_error)


def _assertion(**overrides):
    base = {
        "term": "rotomoulding",
        "relationship": RELATIONSHIP_IN_HOUSE,
        "evidence": "our two rotational moulding machines run three shifts",
        "confidence": 0.88,
    }
    base.update(overrides)
    return base


def _extractor(assertions=None, raw_text=None, raise_error=None):
    text = raw_text if raw_text is not None else json.dumps(assertions or [])
    client = FakeOpenAIClient(response_text=text, raise_error=raise_error)
    return CapabilityExtractor(client=client)


# ═════════════════════════════════════════════════════════════
# Extraction: the trading-vs-manufacturing distinction
# ═════════════════════════════════════════════════════════════

class TestExtractDistinguishesMakingFromSelling:

    def test_in_house_capability_is_extracted(self):
        extractor = _extractor([_assertion()])
        findings = extractor.extract("some page text", source_url="https://acme.example.com")
        assert len(findings) == 1
        assert findings[0].relationship == RELATIONSHIP_IN_HOUSE
        assert findings[0].canonical_term == "rotational moulding"

    def test_sold_only_produces_no_finding(self):
        """The single most important behaviour in this module: a page
        listing a rotomoulded product for sale must never become a
        claim that the company rotomoulds."""
        extractor = _extractor([_assertion(relationship="sold_only")])
        findings = extractor.extract("some page text")
        assert findings == []

    def test_subcontracted_is_extracted_as_its_own_relationship(self):
        extractor = _extractor([_assertion(relationship=RELATIONSHIP_SUBCONTRACTED)])
        findings = extractor.extract("some page text")
        assert len(findings) == 1
        assert findings[0].relationship == RELATIONSHIP_SUBCONTRACTED

    def test_mixed_relationships_are_kept_separate(self):
        extractor = _extractor([
            _assertion(term="rotomoulding", relationship=RELATIONSHIP_IN_HOUSE),
            _assertion(term="injection molding", relationship=RELATIONSHIP_SUBCONTRACTED),
            _assertion(term="sub assembly", relationship="sold_only"),
        ])
        findings = extractor.extract("page text")
        by_relationship = {f.reported_term: f.relationship for f in findings}
        assert by_relationship == {
            "rotomoulding": RELATIONSHIP_IN_HOUSE,
            "injection molding": RELATIONSHIP_SUBCONTRACTED,
        }


# ═════════════════════════════════════════════════════════════
# Vocabulary mapping
# ═════════════════════════════════════════════════════════════

class TestVocabularyMapping:

    def test_recognised_term_gets_canonical_and_category(self):
        extractor = _extractor([_assertion(term="rotomoulding")])
        finding = extractor.extract("text")[0]
        assert finding.canonical_term == "rotational moulding"
        assert finding.category == "process"

    def test_unrecognised_term_is_kept_not_dropped(self):
        extractor = _extractor([_assertion(term="hydroforming")])
        findings = extractor.extract("text")
        assert len(findings) == 1
        assert findings[0].canonical_term is None
        assert findings[0].reported_term == "hydroforming"

    def test_unmapped_terms_helper_lists_only_unrecognised_ones(self):
        extractor = _extractor([
            _assertion(term="rotomoulding"),
            _assertion(term="hydroforming"),
        ])
        findings = extractor.extract("text")
        assert unmapped_terms(findings) == ["hydroforming"]


# ═════════════════════════════════════════════════════════════
# No silent confidence fabrication / malformed input handling
# ═════════════════════════════════════════════════════════════

class TestMalformedAssertions:

    def test_missing_confidence_drops_the_assertion(self):
        bad = _assertion()
        del bad["confidence"]
        extractor = _extractor([bad])
        assert extractor.extract("text") == []

    def test_out_of_range_confidence_drops_rather_than_clamps(self):
        extractor = _extractor([_assertion(confidence=1.4), _assertion(confidence=-0.1)])
        assert extractor.extract("text") == []

    def test_boolean_confidence_is_rejected(self):
        extractor = _extractor([_assertion(confidence=True)])
        assert extractor.extract("text") == []

    def test_missing_evidence_drops_the_assertion(self):
        extractor = _extractor([_assertion(evidence="")])
        assert extractor.extract("text") == []

    def test_unrecognised_relationship_value_is_dropped(self):
        extractor = _extractor([_assertion(relationship="maybe")])
        assert extractor.extract("text") == []

    def test_one_malformed_assertion_does_not_poison_the_batch(self):
        extractor = _extractor([
            _assertion(term="rotomoulding"),
            _assertion(term="injection molding", confidence="high"),
            _assertion(term="sub assembly"),
        ])
        findings = extractor.extract("text")
        assert {f.reported_term for f in findings} == {"rotomoulding", "sub assembly"}


# ═════════════════════════════════════════════════════════════
# Empty input / failure handling — never raises
# ═════════════════════════════════════════════════════════════

class TestEmptyAndFailureHandling:

    def test_empty_page_text_returns_empty_without_calling_the_model(self):
        client = FakeOpenAIClient(response_text="[]")
        extractor = CapabilityExtractor(client=client)
        assert extractor.extract("") == []
        assert client.chat.completions.last_call_kwargs is None

    def test_empty_array_response_is_a_valid_empty_result(self):
        extractor = _extractor([])
        assert extractor.extract("a page with nothing on it") == []

    def test_model_error_is_caught_and_returns_empty_list(self):
        extractor = _extractor(raise_error=RuntimeError("API down"))
        assert extractor.extract("text") == []

    def test_unparseable_response_returns_empty_list_not_a_raise(self):
        extractor = _extractor(raw_text="I cannot help with that.")
        assert extractor.extract("text") == []

    def test_fenced_json_is_tolerated(self):
        fenced = "```json\n" + json.dumps([_assertion()]) + "\n```"
        extractor = _extractor(raw_text=fenced)
        assert len(extractor.extract("text")) == 1


# ═════════════════════════════════════════════════════════════
# extract_from_pages
# ═════════════════════════════════════════════════════════════

class _FakePage:
    def __init__(self, url, text):
        self.url = url
        self.text = text


class TestExtractFromPages:

    def test_aggregates_findings_across_pages(self):
        extractor = _extractor([_assertion()])
        pages = [_FakePage("https://a.example.com/1", "text1"), _FakePage("https://a.example.com/2", "text2")]
        findings = extractor.extract_from_pages(pages)
        assert len(findings) == 2  # same fake response returned for every call
        assert {f.source_url for f in findings} == {"https://a.example.com/1", "https://a.example.com/2"}


# ═════════════════════════════════════════════════════════════
# assess_own_website_capability — the standalone, not-yet-wired-in
# signal function
# ═════════════════════════════════════════════════════════════

class TestAssessOwnWebsiteCapability:

    def test_no_findings_returns_none_not_false(self):
        verdict, explanation = assess_own_website_capability([])
        assert verdict is None
        assert "no capability findings" in explanation.lower() or "not yet assessed" in explanation.lower()

    def test_in_house_finding_is_positive_evidence(self):
        finding = CapabilityFinding(
            reported_term="rotomoulding", canonical_term="rotational moulding", category="process",
            relationship=RELATIONSHIP_IN_HOUSE, confidence=0.9, evidence="we operate...", source_url="",
        )
        verdict, explanation = assess_own_website_capability([finding])
        assert verdict is True
        assert "rotational moulding" in explanation

    def test_subcontracted_only_is_not_negative_evidence(self):
        """A real manufacturer legitimately subcontracts some
        processes — this must never be treated as evidence the
        company is a trader."""
        finding = CapabilityFinding(
            reported_term="injection molding", canonical_term="injection moulding", category="process",
            relationship=RELATIONSHIP_SUBCONTRACTED, confidence=0.8, evidence="our partners provide...",
            source_url="",
        )
        verdict, explanation = assess_own_website_capability([finding])
        assert verdict is None  # not True, and critically not False

    def test_in_house_takes_priority_over_subcontracted_when_both_present(self):
        findings = [
            CapabilityFinding("rotomoulding", "rotational moulding", "process", RELATIONSHIP_IN_HOUSE, 0.9, "e1", ""),
            CapabilityFinding("injection molding", "injection moulding", "process", RELATIONSHIP_SUBCONTRACTED, 0.8, "e2", ""),
        ]
        verdict, _ = assess_own_website_capability(findings)
        assert verdict is True

    def test_asserted_only_findings_are_invisible_to_the_manufacturer_verdict(self):
        """The specific new behaviour this extension must get right:
        'we serve the UK market' says nothing about whether a company
        manufactures anything -- must return None, not True or False."""
        finding = CapabilityFinding(
            reported_term="serves uk market", canonical_term="serves uk market", category="market_presence",
            relationship=RELATIONSHIP_ASSERTED, confidence=0.85, evidence="we supply customers across the UK",
            source_url="",
        )
        verdict, explanation = assess_own_website_capability([finding])
        assert verdict is None

    def test_asserted_findings_do_not_mask_a_real_in_house_finding(self):
        """A page can assert market presence AND describe real
        manufacturing -- the in-house signal must still surface."""
        findings = [
            CapabilityFinding("serves uk market", "serves uk market", "market_presence", RELATIONSHIP_ASSERTED, 0.85, "e1", ""),
            CapabilityFinding("rotomoulding", "rotational moulding", "process", RELATIONSHIP_IN_HOUSE, 0.9, "e2", ""),
        ]
        verdict, _ = assess_own_website_capability(findings)
        assert verdict is True


class TestCommercialIntelligenceExtension:
    """Tests for the v9 extension: the 'asserted' relationship value
    and the new logistics/market_presence/oem_readiness categories."""

    def test_asserted_relationship_is_accepted(self):
        extractor = _extractor([_assertion(
            term="serves uk market", relationship=RELATIONSHIP_ASSERTED,
            evidence="we have served customers across the UK for over a decade",
        )])
        findings = extractor.extract("text")
        assert len(findings) == 1
        assert findings[0].relationship == RELATIONSHIP_ASSERTED

    def test_market_presence_term_maps_to_the_right_category(self):
        extractor = _extractor([_assertion(
            term="serves uk market", relationship=RELATIONSHIP_ASSERTED,
            evidence="we have served customers across the UK for over a decade",
        )])
        finding = extractor.extract("text")[0]
        assert finding.canonical_term == "serves uk market"
        assert finding.category == "market_presence"

    def test_logistics_term_maps_correctly(self):
        extractor = _extractor([_assertion(
            term="ddp", relationship=RELATIONSHIP_IN_HOUSE, evidence="we offer DDP shipping to all our customers",
        )])
        finding = extractor.extract("text")[0]
        assert finding.canonical_term == "ddp shipping"
        assert finding.category == "logistics"

    def test_logistics_term_can_be_subcontracted(self):
        """Unlike market presence, logistics facts DO have a
        meaningful in-house-vs-subcontracted distinction (own fleet
        vs a named freight partner)."""
        extractor = _extractor([_assertion(
            term="customs clearance", relationship=RELATIONSHIP_SUBCONTRACTED,
            evidence="customs clearance is handled by our logistics partner",
        )])
        finding = extractor.extract("text")[0]
        assert finding.canonical_term == "customs expertise"
        assert finding.relationship == RELATIONSHIP_SUBCONTRACTED

    def test_oem_readiness_term_maps_correctly(self):
        extractor = _extractor([_assertion(term="ppap", relationship=RELATIONSHIP_IN_HOUSE)])
        finding = extractor.extract("text")[0]
        assert finding.canonical_term == "ppap capability"
        assert finding.category == "oem_readiness"

    def test_mixed_manufacturing_and_commercial_findings_on_one_page(self):
        extractor = _extractor([
            _assertion(term="rotomoulding", relationship=RELATIONSHIP_IN_HOUSE),
            _assertion(term="ddp", relationship=RELATIONSHIP_IN_HOUSE, evidence="we offer DDP shipping to all our customers"),
            _assertion(term="serves europe market", relationship=RELATIONSHIP_ASSERTED),
        ])
        findings = extractor.extract("text")
        categories = {f.category for f in findings}
        assert categories == {"process", "logistics", "market_presence"}

    def test_still_rejects_sold_only_for_a_manufacturing_term(self):
        """The original, most important rule must survive the
        extension unchanged."""
        extractor = _extractor([_assertion(relationship="sold_only")])
        assert extractor.extract("text") == []

    def test_bulk_import_still_rejects_a_genuinely_invalid_relationship(self):
        extractor = _extractor([_assertion(relationship="not_a_real_relationship")])
        assert extractor.extract("text") == []
