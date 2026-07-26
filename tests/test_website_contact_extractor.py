"""
tests/test_website_contact_extractor.py

Tests for verification.website_contact_extractor. The image-srcset
false positive gets its own section since it's a real, common failure
mode on exactly the kind of modern site this module reads, not a
hypothetical edge case.
"""

from __future__ import annotations

from verification.website_contact_extractor import (
    ContactFindings,
    best_contact_method,
    country_name_to_region_code,
    extract_contact_details,
    extract_emails,
    extract_phone_numbers,
)


class TestExtractEmails:

    def test_finds_a_plausible_email(self):
        assert extract_emails("Contact us at sales@acme-trailer.com for a quote") == ["sales@acme-trailer.com"]

    def test_lowercases_and_deduplicates(self):
        text = "Email: Sales@Acme.com or sales@acme.com for orders"
        assert extract_emails(text) == ["sales@acme.com"]

    def test_multiple_distinct_emails_preserve_first_seen_order(self):
        text = "Sales: sales@acme.com. Support: support@acme.com."
        assert extract_emails(text) == ["sales@acme.com", "support@acme.com"]

    def test_no_emails_returns_empty_list(self):
        assert extract_emails("We are a leading manufacturer of trailer components.") == []


class TestImageSrcsetFalsePositive:
    """The specific, common false positive this module exists partly
    to avoid: responsive-image markup like `photo@2x.png` matches a
    naive email regex perfectly."""

    def test_retina_image_srcset_is_not_treated_as_an_email(self):
        html_text = 'srcset="factory-photo@2x.png 2x, factory-photo@3x.jpg 3x"'
        assert extract_emails(html_text) == []

    def test_real_email_alongside_srcset_markup_is_still_found(self):
        text = 'srcset="logo@2x.png 2x" and contact us at info@acme.com'
        assert extract_emails(text) == ["info@acme.com"]

    def test_various_image_extensions_are_all_excluded(self):
        for ext in ("png", "jpg", "jpeg", "gif", "webp", "svg"):
            text = f"image@2x.{ext}"
            assert extract_emails(text) == [], f"{ext} should have been excluded"


class TestJunkFiltering:

    def test_placeholder_domains_are_excluded(self):
        assert extract_emails("Enter your email: you@example.com") == []
        assert extract_emails("test@test.com") == []

    def test_noreply_addresses_are_excluded(self):
        assert extract_emails("This message was sent from noreply@acme.com") == []
        assert extract_emails("do-not-reply@acme.com sent this") == []

    def test_real_looking_address_at_a_junk_domain_is_still_excluded(self):
        assert extract_emails("sales@sentry.io") == []


class TestExtractPhoneNumbers:

    def test_finds_a_number_with_explicit_country_code(self):
        numbers = extract_phone_numbers("Call us: +86 574 8765 4321")
        assert numbers == ["+8657487654321"]

    def test_no_number_found_without_region_hint_or_country_code(self):
        # A bare local-format number with no + prefix and no region
        # hint is genuinely ambiguous -- correctly finds nothing.
        assert extract_phone_numbers("Call 87654321") == []

    def test_region_hint_finds_a_locally_formatted_number(self):
        numbers = extract_phone_numbers("Tel: 0574-8765 4321", default_region="CN")
        assert len(numbers) == 1
        assert numbers[0].startswith("+86")

    def test_deduplicates_repeated_numbers(self):
        text = "Tel: +86 574 8765 4321. Fax: +86 574 8765 4321."
        assert extract_phone_numbers(text) == ["+8657487654321"]

    def test_no_phone_numbers_returns_empty_list(self):
        assert extract_phone_numbers("We manufacture trailer components.") == []

    def test_malformed_input_never_raises(self):
        # phonenumbers is defensive by design, but this module's own
        # try/except must never let a third-party library's failure on
        # one page abort a batch run.
        assert extract_phone_numbers("") == []
        assert extract_phone_numbers("+" * 50) == []


class TestCountryNameToRegionCode:

    def test_recognises_common_country_names(self):
        assert country_name_to_region_code("China") == "CN"
        assert country_name_to_region_code("United Kingdom") == "GB"

    def test_none_input_returns_none(self):
        assert country_name_to_region_code(None) is None

    def test_unrecognisable_input_returns_none_not_raise(self):
        assert country_name_to_region_code("Not A Real Country Name Xyz123") is None


class _FakePage:
    def __init__(self, url, text):
        self.url = url
        self.text = text


class TestExtractContactDetails:

    def test_aggregates_across_pages(self):
        pages = [
            _FakePage("https://acme.example.com", "Call +86 574 8765 4321"),
            _FakePage("https://acme.example.com/contact", "Email: sales@acme.example.com"),
        ]
        findings = extract_contact_details(pages)
        assert len(findings) == 2
        assert findings[0].phone_numbers == ["+8657487654321"]
        assert findings[1].emails == ["sales@acme.example.com"]

    def test_page_with_neither_is_excluded_from_results(self):
        pages = [
            _FakePage("https://acme.example.com/about", "We have been manufacturing since 1995."),
            _FakePage("https://acme.example.com/contact", "Email: sales@acme.example.com"),
        ]
        findings = extract_contact_details(pages)
        assert len(findings) == 1
        assert findings[0].source_url == "https://acme.example.com/contact"

    def test_empty_page_list_returns_empty(self):
        assert extract_contact_details([]) == []

    def test_region_hint_is_passed_through_to_phone_extraction(self):
        pages = [_FakePage("https://acme.example.com", "Tel: 0574-8765 4321")]
        without_hint = extract_contact_details(pages)
        with_hint = extract_contact_details(pages, default_region="CN")
        assert without_hint == []
        assert len(with_hint) == 1


class _FakePageWithForm:
    def __init__(self, url, text, has_contact_form=False):
        self.url = url
        self.text = text
        self.has_contact_form = has_contact_form


class TestContactFormFallback:

    def test_page_with_only_a_form_still_produces_a_finding(self):
        pages = [_FakePageWithForm(
            "https://acme.example.com/contact", "Get in touch with us", has_contact_form=True,
        )]
        findings = extract_contact_details(pages)
        assert len(findings) == 1
        assert findings[0].has_contact_form is True
        assert findings[0].emails == []

    def test_page_with_neither_email_phone_nor_form_is_excluded(self):
        pages = [_FakePageWithForm("https://acme.example.com/about", "We were founded in 1998.")]
        assert extract_contact_details(pages) == []

    def test_object_without_has_contact_form_attribute_defaults_to_false(self):
        """Backward compatible with any caller passing a plain object
        that predates this field."""
        pages = [_FakePage("https://acme.example.com/about", "We were founded in 1998.")]
        assert extract_contact_details(pages) == []


class TestBestContactMethod:

    def test_prefers_email_over_everything_else(self):
        findings = [
            ContactFindings(emails=["sales@acme.com"], phone_numbers=["+861234567890"],
                             source_url="https://acme.example.com", has_contact_form=True),
        ]
        result = best_contact_method(findings)
        assert result == {"method": "email", "value": "sales@acme.com"}

    def test_falls_back_to_phone_when_no_email(self):
        findings = [
            ContactFindings(emails=[], phone_numbers=["+861234567890"],
                             source_url="https://acme.example.com", has_contact_form=True),
        ]
        result = best_contact_method(findings)
        assert result == {"method": "phone", "value": "+861234567890"}

    def test_falls_back_to_contact_form_when_no_email_or_phone(self):
        findings = [
            ContactFindings(emails=[], phone_numbers=[], source_url="https://acme.example.com/contact",
                             has_contact_form=True),
        ]
        result = best_contact_method(findings)
        assert result == {"method": "contact_form", "value": "https://acme.example.com/contact"}

    def test_nothing_found_at_all_returns_none_method(self):
        assert best_contact_method([]) == {"method": None, "value": None}

    def test_email_on_a_later_page_still_wins_over_an_earlier_forms_only_page(self):
        findings = [
            ContactFindings(emails=[], phone_numbers=[], source_url="https://acme.example.com/contact",
                             has_contact_form=True),
            ContactFindings(emails=["sales@acme.com"], phone_numbers=[],
                             source_url="https://acme.example.com/about", has_contact_form=False),
        ]
        result = best_contact_method(findings)
        assert result == {"method": "email", "value": "sales@acme.com"}
