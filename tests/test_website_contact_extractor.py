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
    PhoneFinding,
    best_contact_method,
    country_name_to_region_code,
    extract_contact_details,
    extract_emails,
    extract_mailto_emails,
    extract_phone_numbers,
    extract_tel_phones,
    extract_typed_phone_numbers,
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


class TestPlaceholderEmailFiltering:
    """abc@xyz.com, test@test.com, example@example.com, and similar
    template defaults -- never a real business's own contact, but a
    different category from _JUNK_DOMAINS' third-party-tooling
    artifacts (Sentry/Wix/GoDaddy) or _JUNK_LOCAL_PARTS' real
    functioning automated addresses (noreply@). See
    find_placeholder_emails for the counterpart that surfaces these
    for logging rather than just silently dropping them."""

    def test_abc_at_xyz_is_excluded(self):
        assert extract_emails("Contact: abc@xyz.com") == []

    def test_test_at_test_is_excluded(self):
        assert extract_emails("test@test.com") == []

    def test_example_at_example_is_excluded(self):
        assert extract_emails("example@example.com") == []

    def test_placeholder_local_part_excluded_even_on_an_unlisted_domain(self):
        """The local part alone is enough -- "test@realcompany.com" is
        exactly as much a placeholder as "test@test.com", and checking
        only a fixed domain list would miss it."""
        assert extract_emails("test@realcompany.com") == []
        assert extract_emails("sample@acmetrailer.com") == []

    def test_placeholder_domain_excluded_regardless_of_local_part(self):
        assert extract_emails("sales@placeholder.com") == []
        assert extract_emails("info@yourwebsite.com") == []

    def test_real_address_is_not_caught_by_the_placeholder_filter(self):
        assert extract_emails("Contact us at sales@acmetrailer.com") == ["sales@acmetrailer.com"]


class TestFindPlaceholderEmails:

    def test_finds_what_extract_emails_silently_dropped(self):
        from verification.website_contact_extractor import find_placeholder_emails
        text = "Contact: abc@xyz.com"
        assert extract_emails(text) == []
        assert find_placeholder_emails(text) == ["abc@xyz.com"]

    def test_does_not_surface_routine_junk_domain_rejections(self):
        """A different, older category (third-party tooling artifacts)
        -- not what this function exists to surface."""
        from verification.website_contact_extractor import find_placeholder_emails
        assert find_placeholder_emails("sales@sentry.io") == []

    def test_does_not_surface_system_address_rejections(self):
        from verification.website_contact_extractor import find_placeholder_emails
        assert find_placeholder_emails("noreply@acme.com") == []

    def test_does_not_surface_image_extension_false_positives(self):
        from verification.website_contact_extractor import find_placeholder_emails
        assert find_placeholder_emails("srcset=\"photo@2x.png\"") == []

    def test_real_accepted_email_is_not_surfaced_either(self):
        from verification.website_contact_extractor import find_placeholder_emails
        assert find_placeholder_emails("sales@acmetrailer.com") == []

    def test_no_placeholder_returns_empty_list(self):
        from verification.website_contact_extractor import find_placeholder_emails
        assert find_placeholder_emails("We manufacture trailer components.") == []

    def test_mailto_counterpart_finds_the_same_pattern(self):
        from verification.website_contact_extractor import find_placeholder_mailto_emails
        assert find_placeholder_mailto_emails(["abc@xyz.com"]) == ["abc@xyz.com"]
        assert find_placeholder_mailto_emails(["sales@acmetrailer.com"]) == []


class TestObfuscatedEmails:
    """Found via a real calibration run: "info[at]ap-bochum.de" was
    missed entirely before this."""

    def test_bracket_at_marker(self):
        assert extract_emails("Email us: info[at]ap-bochum.de for details") == ["info@ap-bochum.de"]

    def test_paren_at_marker(self):
        assert extract_emails("Email us: info(at)ap-bochum.de for details") == ["info@ap-bochum.de"]

    def test_bracket_dot_marker(self):
        assert extract_emails("Contact: info[at]ap-bochum[dot]de") == ["info@ap-bochum.de"]

    def test_paren_dot_marker(self):
        assert extract_emails("Contact: info(at)ap-bochum(dot)de") == ["info@ap-bochum.de"]

    def test_case_insensitive_markers(self):
        assert extract_emails("Contact: info[AT]ap-bochum[DOT]de") == ["info@ap-bochum.de"]

    def test_obfuscated_and_plain_emails_both_found(self):
        text = "Sales: sales@acme.com. Support: support[at]acme.com."
        assert extract_emails(text) == ["sales@acme.com", "support@acme.com"]

    def test_obfuscated_junk_domain_is_still_excluded(self):
        assert extract_emails("test[at]test.com") == []

    def test_ordinary_prose_with_the_word_at_produces_no_false_positive(self):
        """A bare " at "/" dot " marker was tried and reverted
        specifically because of this shape -- see the module-level
        comment above _AT_MARKERS. Only bracketed/parenthesised forms
        are recognised now."""
        text = "Look at our new products at booth 5 during the trade show this year."
        assert extract_emails(text) == []

    def test_bare_at_word_is_no_longer_treated_as_obfuscation(self):
        """Explicit non-regression: "word at domain.com" with no
        brackets is ordinary prose, not obfuscation -- must not produce
        an email."""
        assert extract_emails("Reach us at info at ap-bochum.de anytime") == []

    def test_bare_dot_word_is_no_longer_treated_as_obfuscation(self):
        assert extract_emails("Contact: info at ap-bochum dot de") == []

    def test_prose_available_at_a_domain_across_a_line_break_produces_no_false_email(self):
        """The exact real-world false positive found via a calibration
        run: a bare nginx default page's own boilerplate text --
        "Commercial support is available at\\nnginx.com." -- was
        previously turned into "available@nginx.com" by the (now
        removed) bare " at " marker, since \\s+ matches the newline
        between "at" and the domain too."""
        text = "Commercial support is available at\nnginx.com.\nThank you for using nginx."
        assert extract_emails(text) == []


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


class TestExtractTypedPhoneNumbers:
    """Found via a real calibration run: 6 of 20 suppliers had a
    mobile number displayed that the old first-number-only path
    silently discarded. This is the fix -- every number is kept, with
    a type label so they can be told apart."""

    def test_mobile_number_classified_via_phonenumbers_library(self):
        findings = extract_typed_phone_numbers("Mobile: +8613800001111")
        assert findings == [PhoneFinding(number="+8613800001111", phone_type="mobile")]

    def test_landline_number_classified_via_phonenumbers_library(self):
        findings = extract_typed_phone_numbers("Tel: +862112345678")
        assert findings == [PhoneFinding(number="+862112345678", phone_type="landline")]

    def test_both_a_landline_and_a_mobile_are_kept_not_just_the_first(self):
        """The exact bug: a landline appearing first in page order must
        not cause the mobile number later in the same text to be lost."""
        text = "Tel: +862112345678, Mobile: +8613800001111"
        findings = extract_typed_phone_numbers(text)
        assert len(findings) == 2
        types = {f.phone_type for f in findings}
        assert types == {"landline", "mobile"}

    def test_whatsapp_label_overrides_mobile_landline_classification(self):
        findings = extract_typed_phone_numbers("WhatsApp: +8613800001111")
        assert findings == [PhoneFinding(number="+8613800001111", phone_type="whatsapp")]

    def test_wechat_label_is_detected(self):
        findings = extract_typed_phone_numbers("WeChat: +8613800001111")
        assert findings[0].phone_type == "wechat"

    def test_fax_label_is_detected(self):
        findings = extract_typed_phone_numbers("Fax: +862112345678")
        assert findings[0].phone_type == "fax"

    def test_same_number_labelled_differently_on_different_lines_produces_two_findings(self):
        text = "Mobile: +8613800001111\nWhatsApp: +8613800001111"
        findings = extract_typed_phone_numbers(text)
        assert len(findings) == 2
        assert {f.phone_type for f in findings} == {"mobile", "whatsapp"}

    def test_a_labels_context_does_not_bleed_into_a_second_nearby_number(self):
        """A closely-following second number must not inherit the
        first number's label just because it's still within a naive
        fixed-size lookback window."""
        text = "WhatsApp: +8613800001111, WeChat: +8613900002222"
        findings = extract_typed_phone_numbers(text)
        by_number = {f.number: f.phone_type for f in findings}
        assert by_number["+8613800001111"] == "whatsapp"
        assert by_number["+8613900002222"] == "wechat"

    def test_region_hint_is_respected_same_as_extract_phone_numbers(self):
        findings = extract_typed_phone_numbers("Tel: 021-1234 5678", default_region="CN")
        assert len(findings) == 1
        assert findings[0].number.startswith("+86")

    def test_no_numbers_returns_empty_list(self):
        assert extract_typed_phone_numbers("We manufacture trailer components.") == []

    def test_malformed_input_never_raises(self):
        assert extract_typed_phone_numbers("") == []
        assert extract_typed_phone_numbers("+" * 50) == []

    def test_extract_phone_numbers_itself_is_unchanged(self):
        """Backward compatibility: existing callers of the flat-list
        function must see identical behaviour."""
        text = "Tel: +862112345678, Mobile: +8613800001111"
        assert extract_phone_numbers(text) == ["+862112345678", "+8613800001111"]


class TestExtractMailtoEmails:
    """See extract_mailto_emails's own docstring for the real site
    (truckmasters.co.uk) that surfaced why this needs to exist: a
    "Click Here"-style visible link with the real address only in the
    href, invisible to extract_emails' text-only regex scan."""

    def test_extracts_address_from_mailto_href(self):
        assert extract_mailto_emails(["mail@acme.example.com"]) == ["mail@acme.example.com"]

    def test_deduplicates(self):
        result = extract_mailto_emails(["sales@acme.example.com", "sales@acme.example.com"])
        assert result == ["sales@acme.example.com"]

    def test_junk_domain_rejected_same_as_extract_emails(self):
        assert extract_mailto_emails(["info@example.com"]) == []

    def test_image_extension_lookalike_rejected(self):
        assert extract_mailto_emails(["logo@2x.png"]) == []

    def test_non_email_shaped_value_rejected(self):
        assert extract_mailto_emails(["not-an-email"]) == []

    def test_empty_list_returns_empty(self):
        assert extract_mailto_emails([]) == []


class TestExtractTelPhones:
    """See extract_tel_phones's own docstring: a tel: href is parsed
    as a single isolated candidate (phonenumbers.parse), not scanned
    as free text -- unlike extract_typed_phone_numbers, which expects
    a page of prose with possibly-multiple numbers."""

    def test_finds_a_number_with_explicit_country_code(self):
        findings = extract_tel_phones(["+441754880481"])
        assert findings == [PhoneFinding(number="+441754880481", phone_type="landline")]

    def test_national_format_requires_default_region(self):
        assert extract_tel_phones(["01754880481"]) == []
        findings = extract_tel_phones(["01754880481"], default_region="GB")
        assert findings == [PhoneFinding(number="+441754880481", phone_type="landline")]

    def test_deduplicates_by_number_and_type(self):
        findings = extract_tel_phones(["+441754880481", "+441754880481"])
        assert len(findings) == 1

    def test_invalid_number_produces_no_finding(self):
        assert extract_tel_phones(["not-a-number"]) == []

    def test_empty_list_returns_empty(self):
        assert extract_tel_phones([]) == []

    def test_url_encoded_href_is_decoded_before_parsing(self):
        """Real gap found live: BeautifulSoup returns an href attribute
        exactly as written in the HTML source, with no decoding of its
        own -- a real site's tel: href had its number's spaces percent-
        encoded (tel:0808%20100%203430), which phonenumbers.parse()
        rejected outright before this fix, silently, even though the
        decoded number is a perfectly ordinary valid UK number."""
        findings = extract_tel_phones(["0808%20100%203430"], default_region="GB")
        assert findings == [PhoneFinding(number="+448081003430", phone_type="other")]

    def test_url_encoded_and_plain_forms_of_the_same_number_deduplicate(self):
        findings = extract_tel_phones(["0808%20100%203430", "08081003430"], default_region="GB")
        assert len(findings) == 1

    def test_unparseable_href_is_logged_not_silently_dropped(self, caplog):
        with caplog.at_level("WARNING"):
            findings = extract_tel_phones(["not-a-real-number-at-all"])
        assert findings == []
        assert any("not-a-real-number-at-all" in record.message for record in caplog.records)


class TestCountryNameToRegionCode:

    def test_recognises_common_country_names(self):
        assert country_name_to_region_code("China") == "CN"
        assert country_name_to_region_code("United Kingdom") == "GB"

    def test_none_input_returns_none(self):
        assert country_name_to_region_code(None) is None

    def test_unrecognisable_input_returns_none_not_raise(self):
        assert country_name_to_region_code("Not A Real Country Name Xyz123") is None


class _FakePage:
    def __init__(self, url, text, mailto_emails=None, tel_phones=None):
        self.url = url
        self.text = text
        self.mailto_emails = mailto_emails or []
        self.tel_phones = tel_phones or []


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

    def test_typed_phone_numbers_are_populated_alongside_the_flat_list(self):
        pages = [_FakePage("https://acme.example.com/contact", "Mobile: +8613800001111")]
        findings = extract_contact_details(pages)
        assert findings[0].typed_phone_numbers == [PhoneFinding(number="+8613800001111", phone_type="mobile")]

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


class TestExtractContactDetailsMailtoTel:
    """The real bug this covers: a page whose VISIBLE text hides the
    real email/phone behind "Click Here"-style link text, while the
    real values sit in mailto:/tel: hrefs -- found on truckmasters.co.uk's
    contact page (see extract_mailto_emails's docstring)."""

    def test_mailto_email_found_even_when_visible_text_has_none(self):
        pages = [_FakePage(
            "https://acme.example.com/contact", "Email: Click Here",
            mailto_emails=["sales@acme.example.com"],
        )]
        findings = extract_contact_details(pages)
        assert findings[0].emails == ["sales@acme.example.com"]

    def test_mailto_email_merged_with_text_regex_finding_not_duplicated(self):
        pages = [_FakePage(
            "https://acme.example.com/contact", "Email: sales@acme.example.com",
            mailto_emails=["sales@acme.example.com"],
        )]
        findings = extract_contact_details(pages)
        assert findings[0].emails == ["sales@acme.example.com"]

    def test_tel_phone_found_even_when_visible_text_has_none(self):
        pages = [_FakePage(
            "https://acme.example.com/contact", "Call Us",
            tel_phones=["01754880481"],
        )]
        findings = extract_contact_details(pages, default_region="GB")
        assert findings[0].phone_numbers == ["+441754880481"]
        assert findings[0].typed_phone_numbers == [PhoneFinding(number="+441754880481", phone_type="landline")]

    def test_tel_phone_still_needs_a_region_hint_for_national_format(self):
        pages = [_FakePage("https://acme.example.com/contact", "Call Us", tel_phones=["01754880481"])]
        findings = extract_contact_details(pages)
        assert findings == []

    def test_page_with_only_a_junk_page_flag_still_contributes_nothing(self):
        """mailto:/tel: findings must not bypass the parking-page
        filter -- same "nothing on a junk page is trusted" discipline
        as the text-based regex scan."""
        pages = [_FakePage(
            "https://parked.example.com", "The nginx web server is successfully installed.",
            mailto_emails=["admin@parked.example.com"], tel_phones=["+441754880481"],
        )]
        assert extract_contact_details(pages) == []

    def test_a_page_object_without_mailto_tel_attributes_is_unaffected(self):
        """OwnWebsitePage (scrapers/own_website_scraper.py) has no
        mailto_emails/tel_phones fields -- getattr's default must keep
        this function working exactly as before for that caller."""
        class BarePage:
            def __init__(self, url, text):
                self.url = url
                self.text = text

        pages = [BarePage("https://acme.example.com/contact", "Email: sales@acme.example.com")]
        findings = extract_contact_details(pages)
        assert findings[0].emails == ["sales@acme.example.com"]


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
