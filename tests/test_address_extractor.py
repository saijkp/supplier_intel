"""
tests/test_address_extractor.py

Tests for verification/address_extractor.py -- extracted out of
batch/batch_service.py so sourcing/sourcing_agent.py can reuse the
exact same tiered-candidate-source/grounded-extraction logic. No
network, no DB -- repo/llm_client are faked.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from verification.address_extractor import (
    address_candidate_sources,
    attempt_address_extraction,
    reject_reason_for_llm_extraction,
)


class FakePage:
    def __init__(self, url, text, footer_text=""):
        self.url = url
        self.text = text
        self.footer_text = footer_text


class FakeRepo:
    def __init__(self, suppliers: Optional[Dict[int, Dict[str, Any]]] = None):
        self.suppliers: Dict[int, Dict[str, Any]] = suppliers or {}
        self.provenance: List[Dict[str, Any]] = []
        self.history_calls: List[Dict[str, Any]] = []

    def get_supplier(self, supplier_id):
        return dict(self.suppliers[supplier_id]) if supplier_id in self.suppliers else None

    def update_supplier_fields_with_history(self, supplier_id, fields, *, changed_by, change_reason=None):
        self.history_calls.append({"supplier_id": supplier_id, "fields": fields, "changed_by": changed_by})
        self.suppliers.setdefault(supplier_id, {}).update(fields)
        return []

    def save_field_provenance(self, **kwargs):
        self.provenance.append(kwargs)


class FakeLLMClient:
    def __init__(self, response=None, raise_error=None):
        self._response = response
        self._raise_error = raise_error
        self.calls: List[tuple] = []

    def complete_json(self, system_prompt, user_prompt, **kwargs):
        self.calls.append((system_prompt, user_prompt))
        if self._raise_error:
            raise self._raise_error
        return self._response


class TestAddressCandidateSources:

    def test_prefers_contact_page_over_footer_and_impressum(self):
        pages = [
            FakePage("https://x.com/", "homepage text", footer_text="Acme Co, 1 Main St"),
            FakePage("https://x.com/impressum", "Acme Co GmbH, Impressum, 1 Main St, Berlin"),
            FakePage("https://x.com/contact", "Contact us: Acme Co, 1 Main St, Berlin"),
        ]
        candidates = address_candidate_sources(pages)
        assert candidates[0][0] == "contact page"
        assert candidates[0][1] == "https://x.com/contact"

    def test_falls_back_to_footer_when_no_contact_page(self):
        pages = [
            FakePage("https://x.com/", "homepage text", footer_text="Acme Co, 1 Main St"),
            FakePage("https://x.com/impressum", "Acme Co GmbH, Impressum, 1 Main St, Berlin"),
        ]
        candidates = address_candidate_sources(pages)
        assert candidates[0][0] == "footer"
        assert candidates[0][1] == "https://x.com/"

    def test_falls_back_to_impressum_when_no_contact_or_footer(self):
        pages = [
            FakePage("https://x.com/", "homepage text with no address"),
            FakePage("https://x.com/imprint", "Acme Co GmbH, 1 Main St, Berlin"),
        ]
        candidates = address_candidate_sources(pages)
        assert candidates[0][0] == "impressum page"
        assert candidates[0][1] == "https://x.com/imprint"

    def test_only_the_first_matching_page_per_tier_is_used(self):
        pages = [
            FakePage("https://x.com/contact", "first contact page"),
            FakePage("https://x.com/contact-us", "second contact-shaped page"),
        ]
        candidates = address_candidate_sources(pages)
        contact_candidates = [c for c in candidates if c[0] == "contact page"]
        assert len(contact_candidates) == 1
        assert contact_candidates[0][1] == "https://x.com/contact"

    def test_empty_footer_text_is_not_treated_as_a_footer_candidate(self):
        """Empty footer_text correctly yields no FOOTER-tier candidate --
        but the homepage fallback tier still fires since this same page
        (pages[0]) has non-empty body text."""
        pages = [FakePage("https://x.com/", "homepage text", footer_text="")]
        candidates = address_candidate_sources(pages)
        assert candidates == [("homepage", "https://x.com/", "homepage text")]

    def test_no_tier_match_falls_back_to_homepage(self):
        pages = [FakePage("https://x.com/products", "our products page")]
        assert address_candidate_sources(pages) == [("homepage", "https://x.com/products", "our products page")]

    def test_no_pages_at_all_returns_empty_list(self):
        assert address_candidate_sources([]) == []

    def test_homepage_used_as_last_resort_when_no_other_tier_matches(self):
        """The real gap found live: a single-page site (no separate
        /contact or /about page) with its address printed directly in
        the homepage's own footer HTML."""
        pages = [FakePage(
            "https://hinge-manufacturers.com/",
            "Contact info\nAddress: Liuqing Industrial Area, Beiyuan Street, Yiwu, Jinhua, Zhejiang, China",
        )]
        candidates = address_candidate_sources(pages)
        assert candidates == [(
            "homepage", "https://hinge-manufacturers.com/",
            "Contact info\nAddress: Liuqing Industrial Area, Beiyuan Street, Yiwu, Jinhua, Zhejiang, China",
        )]

    def test_contact_page_still_preferred_over_homepage_fallback(self):
        """Regression guard: a site with a dedicated contact page must
        still surface that tier first -- the homepage fallback is only
        ever a later, lower-priority candidate, never a replacement."""
        pages = [
            FakePage("https://x.com/", "Homepage marketing copy, nothing address-shaped here."),
            FakePage("https://x.com/contact", "Contact us: Acme Co, 1 Main St, Springfield, IL."),
        ]
        candidates = address_candidate_sources(pages)
        assert candidates[0][0] == "contact page"
        assert candidates[0][1] == "https://x.com/contact"
        assert candidates[-1][0] == "homepage"

    def test_homepage_is_not_duplicated_when_it_already_served_another_tier(self):
        """pages[0] is always the homepage by construction -- when it
        ALSO happens to be the page that matched a more specific tier
        (e.g. the about-page test above), it must not appear a second
        time under the "homepage" label."""
        pages = [FakePage("https://x.com/contact", "Contact us: Acme Co, 1 Main St, Springfield, IL.")]
        candidates = address_candidate_sources(pages)
        assert candidates == [("contact page", "https://x.com/contact", "Contact us: Acme Co, 1 Main St, Springfield, IL.")]

    def test_falls_back_to_about_page_when_no_contact_footer_or_impressum(self):
        """Added after a real gap-analysis run against the 29 confirmed
        injection-moulding candidates found 3 of 5 "no address found"
        suppliers would have been recovered by an about-page tier."""
        pages = [
            FakePage("https://x.com/", "homepage text with no address"),
            FakePage("https://x.com/about-us", "Acme Co was founded in 1990. Head office: 1 Main St, Berlin."),
        ]
        candidates = address_candidate_sources(pages)
        assert candidates[0][0] == "about page"
        assert candidates[0][1] == "https://x.com/about-us"

    def test_about_page_is_lowest_priority_tried_after_impressum(self):
        pages = [
            FakePage("https://x.com/about-us", "Acme Co, 1 Main St, Berlin"),
            FakePage("https://x.com/imprint", "Acme Co GmbH, 1 Main St, Berlin"),
        ]
        candidates = address_candidate_sources(pages)
        assert [c[0] for c in candidates] == ["impressum page", "about page"]

    def test_about_page_matches_company_url_convention_too(self):
        """Real sites use both conventions for the same page -- e.g.
        plasticmold.net/company/ vs hordrt.com/about-us-3/."""
        pages = [FakePage("https://x.com/company", "Acme Co, 1 Main St, Berlin")]
        candidates = address_candidate_sources(pages)
        assert candidates[0][0] == "about page"
        assert candidates[0][1] == "https://x.com/company"

    def test_empty_about_page_text_is_not_treated_as_a_candidate(self):
        pages = [FakePage("https://x.com/about-us", "")]
        assert address_candidate_sources(pages) == []


class TestRejectReasonForLlmExtraction:

    def test_short_page_text_is_rejected(self):
        assert reject_reason_for_llm_extraction("Hello world") is not None

    def test_parking_page_text_is_rejected(self):
        page_text = "This domain is parked free, courtesy of GoDaddy.com. Would you like to buy this domain?"
        assert reject_reason_for_llm_extraction(page_text) is not None

    def test_ordinary_page_text_is_not_rejected(self):
        long_text = "This is a perfectly ordinary company homepage with real content. " * 2
        assert reject_reason_for_llm_extraction(long_text) is None


class TestAttemptAddressExtraction:

    def test_applied_when_supplier_has_no_existing_address(self):
        repo = FakeRepo({1: {"id": 1, "canonical_name": "Acme Co", "domain": "acme.com"}})
        llm = FakeLLMClient(response={"address": "1 Main St, Springfield, IL 62704, USA"})
        pages = [FakePage(
            "https://acme.com/contact",
            "Contact us at Acme Co, 1 Main St, Springfield, IL 62704, USA. We'd love to hear from you.",
        )]

        result = attempt_address_extraction(repo, llm, 1, pages, changed_by="sourcing_agent")

        assert result == "applied"
        assert repo.suppliers[1]["address"] == "1 Main St, Springfield, IL 62704, USA"
        assert repo.history_calls[0]["changed_by"] == "sourcing_agent"
        prov = [p for p in repo.provenance if p["field_name"] == "address"]
        assert len(prov) == 1
        assert prov[0]["source_url"] == "https://acme.com/contact"

    def test_conflicting_when_supplier_already_has_an_address(self):
        repo = FakeRepo({1: {
            "id": 1, "canonical_name": "Acme Co", "domain": "acme.com",
            "address": "Existing Trusted Address, Springfield, IL",
        }})
        llm = FakeLLMClient(response={"address": "99 Other St, Nowhere, USA"})
        pages = [FakePage(
            "https://acme.com/contact",
            "Get in touch with our team. Contact: 99 Other St, Nowhere, USA. We respond within one business day.",
        )]

        result = attempt_address_extraction(repo, llm, 1, pages, changed_by="sourcing_agent")

        assert result == "conflicting"
        assert repo.suppliers[1]["address"] == "Existing Trusted Address, Springfield, IL"  # untouched
        prov = [p for p in repo.provenance if p["field_name"] == "address_candidate"]
        assert len(prov) == 1
        assert prov[0]["value"] == "99 Other St, Nowhere, USA"

    def test_no_pages_is_skipped(self):
        repo = FakeRepo({1: {"id": 1, "canonical_name": "Acme Co"}})
        result = attempt_address_extraction(repo, FakeLLMClient(), 1, [], changed_by="sourcing_agent")
        assert result == "skipped"

    def test_no_address_found_anywhere_is_skipped(self):
        repo = FakeRepo({1: {"id": 1, "canonical_name": "Acme Co"}})
        llm = FakeLLMClient(response={"address": None})
        pages = [FakePage(
            "https://acme.com/contact",
            "Get in touch with our sales team for a quote today -- we ship worldwide and respond quickly.",
        )]
        result = attempt_address_extraction(repo, llm, 1, pages, changed_by="sourcing_agent")
        assert result == "skipped"

    def test_llm_failure_does_not_raise(self):
        repo = FakeRepo({1: {"id": 1, "canonical_name": "Acme Co"}})
        llm = FakeLLMClient(raise_error=RuntimeError("simulated failure"))
        pages = [FakePage(
            "https://acme.com/contact",
            "Contact us at 1 Main St, Springfield, IL. Our team responds to every enquiry within one business day.",
        )]
        result = attempt_address_extraction(repo, llm, 1, pages, changed_by="sourcing_agent")
        assert result == "skipped"

    def test_applies_from_homepage_when_no_dedicated_page_exists(self):
        """Regression test for the real Maka Hinge Manufacturer candidate
        found during a live sourcing-job test: a single-page site with
        the address printed directly in the homepage's own footer HTML,
        and no separate /contact or /about page at all."""
        repo = FakeRepo({1: {"id": 1, "canonical_name": "Maka Hinge Manufacturer"}})
        address = "Liuqing Industrial Area, Beiyuan Street, Yiwu, Jinhua, Zhejiang, China"
        llm = FakeLLMClient(response={"address": address})
        pages = [FakePage(
            "https://hinge-manufacturers.com/",
            f"Contact info\nAddress: {address}. We manufacture hinges for cabinets and doors worldwide.",
        )]

        result = attempt_address_extraction(repo, llm, 1, pages, changed_by="sourcing_agent")

        assert result == "applied"
        assert repo.suppliers[1]["address"] == address
        prov = [p for p in repo.provenance if p["field_name"] == "address"]
        assert prov[0]["source_url"] == "https://hinge-manufacturers.com/"

    def test_applies_from_contact_page_not_homepage_when_both_present(self):
        """Regression guard for the new homepage fallback: when a
        dedicated contact page exists AND yields a real address, that
        tier wins -- the homepage fallback is never consulted."""
        repo = FakeRepo({1: {"id": 1, "canonical_name": "Acme Co"}})
        llm = FakeLLMClient(response={"address": "1 Main St, Springfield, IL"})
        pages = [
            FakePage("https://acme.com/", "Homepage marketing copy about our great products, shipped worldwide every day."),
            FakePage(
                "https://acme.com/contact",
                "Contact us: Acme Co, 1 Main St, Springfield, IL. We reply to every enquiry fast.",
            ),
        ]

        result = attempt_address_extraction(repo, llm, 1, pages, changed_by="sourcing_agent")

        assert result == "applied"
        prov = [p for p in repo.provenance if p["field_name"] == "address"]
        assert prov[0]["source_url"] == "https://acme.com/contact"

    def test_changed_by_is_threaded_through_to_the_audit_trail(self):
        """The whole reason changed_by is a parameter, not a hardcoded
        'batch_service' string, is so sourcing_agent.py's own calls are
        attributed correctly in supplier_change_log."""
        repo = FakeRepo({1: {"id": 1, "canonical_name": "Acme Co"}})
        llm = FakeLLMClient(response={"address": "1 Main St, Springfield, IL"})
        pages = [FakePage(
            "https://acme.com/contact",
            "Contact us at 1 Main St, Springfield, IL. Our team responds to every enquiry within one business day.",
        )]

        attempt_address_extraction(repo, llm, 1, pages, changed_by="sourcing_agent")

        assert repo.history_calls[0]["changed_by"] == "sourcing_agent"
