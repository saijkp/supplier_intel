"""
verification/website_contact_extractor.py

Pulls contact details (email, phone) out of a supplier's own website
pages — the same pages `capability_extractor.py` already reads for
process/capability claims. This module deliberately costs nothing per
call: emails and phone numbers are both pattern-detectable, so there
is no reason to spend an LLM call on them when a regex and a proven
phone-parsing library do the job exactly. Every page fetched via
`scrapers.own_website_scraper` is now read twice for the price of one
fetch — once here, once by `CapabilityExtractor` — which is the whole
point: the HTTP cost was already being paid and most of what came back
was being thrown away.

Why this exists as a separate module from capability_extractor.py
--------------------------------------------------------------------
Different failure mode, different cost profile, different confidence
model. Capability extraction is a judgment call ("does this page
assert an in-house process") that only a model can make and that can
be wrong in either direction. Whether a string is a syntactically
valid, plausible business email or phone number is not a judgment
call — it is pattern matching, and conflating the two into one LLM
prompt would spend money on the part that has no reason to cost
anything and add unnecessary noise to the part that does.

The image-srcset false positive, and why it matters here specifically
--------------------------------------------------------------------
A naive email regex over raw scraped HTML reliably produces false
positives from responsive-image markup: `photo@2x.png`, `logo@3x.jpg`
match `[\\w.+-]+@[\\w-]+\\.[\\w.-]+` perfectly, because a two- or
three-letter image extension is indistinguishable from a two- or
three-letter TLD to a regex that doesn't know about image formats.
This is not a hypothetical edge case — retina/srcset markup is
extremely common on exactly the kind of modern corporate website this
module reads, so `_IMAGE_EXTENSION_TLDS` exists specifically to reject
it, and it is the first thing this module's own tests check.
"""

from __future__ import annotations

import dataclasses
import re
from typing import List, Optional

import phonenumbers

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# TLD-shaped strings that are actually image file extensions caught by
# the srcset false positive described in the module docstring. Matched
# case-insensitively against whatever the regex captured as the final
# domain label.
_IMAGE_EXTENSION_TLDS = {
    "png", "jpg", "jpeg", "gif", "webp", "svg", "ico", "bmp", "avif",
}

# Local-parts and domains that are real, syntactically valid email
# addresses but never useful as a procurement contact — placeholder
# text left in unfilled form templates, or auto-generated addresses
# that go nowhere. Excluded outright rather than surfaced and left for
# a human to filter every time.
_JUNK_LOCAL_PARTS = {"noreply", "no-reply", "donotreply", "do-not-reply", "postmaster", "webmaster"}
_JUNK_DOMAINS = {
    "example.com", "example.org", "example.net", "yourdomain.com",
    "domain.com", "email.com", "test.com", "sentry.io", "wixpress.com",
    "godaddy.com", "yourcompany.com",
}

# Common ways a page written for humans (not scrapers) obfuscates an
# email address to dodge naive scrapers -- checked case-insensitively,
# as a text-level substitution BEFORE re-running the same strict
# _EMAIL_RE below, not a separate looser regex -- every existing
# junk-filter (image-extension TLDs, junk local parts/domains) still
# applies unchanged to whatever this normalises into a real-looking
# address. Found via a real calibration run: "info[at]ap-bochum.de"
# was missed entirely before this.
#
# Bracketed/parenthesised forms only -- deliberately NO bare " at "/
# " dot " marker. That was tried and reverted: ordinary prose like
# "support is available at\nnginx.com" (note \s+ matches the newline
# too) gets turned into a syntactically-valid-looking fake email by a
# blanket substitution, since nothing about plain English marks it as
# an address. Someone actually hiding an email from scrapers uses
# brackets/parens; plain "word at domain.com" is just a sentence.
_AT_MARKERS = (r"\[\s*at\s*\]", r"\(\s*at\s*\)")
_DOT_MARKERS = (r"\[\s*dot\s*\]", r"\(\s*dot\s*\)")

# Checked as a substring against a page's fetched TEXT -- real
# server-default/parking-page boilerplate phrases, so substring
# matching here doesn't carry the false-positive risk a blocklist
# substring match against an extracted NAME would (see
# batch.batch_service._JUNK_NAME_EXACT_BLOCKLIST, which is whole-name
# only for exactly that reason).
_PARKING_PAGE_TEXT_SIGNATURES: tuple = (
    "the nginx web server is successfully installed",
    "apache2 ubuntu default page",
    "apache2 debian default page",
    "this is the default welcome page",
    "the web server software is running but no content has been added",
    "this domain is parked",
    "domain is parked",
    "buy this domain",
    "this domain is for sale",
    "godaddy.com",
    "namecheap parking",
    "further configuration is required",
)

def parking_page_reason(page_text: str) -> Optional[str]:
    """None if `page_text` doesn't look like a server-default/parking/
    holding page; otherwise a human-readable reason it does.
    Deliberately signature-only -- no minimum-length check here (that
    was tried and reverted: a real "Contact Us" page is often
    legitimately short -- just an address/phone block, no prose -- and
    a length floor tuned for "is there enough context for an LLM claim
    to be trustworthy" incorrectly filtered those out once this became
    the shared gate for contact extraction too. batch/batch_service.py
    layers its own length floor on top of this for name/address
    extraction specifically, where that rationale actually applies --
    see its _reject_reason_for_llm_extraction).

    Lives here, not in batch/batch_service.py (where it originated) --
    a page that's a server default/parking page produces a confident
    WRONG answer for every kind of extraction run against it, not just
    the name extraction it was first built for (found via a real
    calibration run: a bare nginx default page produced both a bogus
    company name AND, via a since-fixed email-deobfuscation bug, a
    bogus email). verification/ is the shared layer every collection
    run passes through (batch AND standalone `main.py collect`), so
    this needs to live below batch/, not inside it -- batch_service.py
    imports this rather than defining its own copy.

    Used as a per-page pre-filter in three places: name extraction and
    address extraction (both in batch/batch_service.py, layered with
    that module's own length floor) and extract_contact_details below
    (a page this flags contributes no ContactFindings at all -- same
    "silently skip, don't guess" discipline, signature-only, no length
    floor)."""
    haystack = (page_text or "").lower()
    for signature in _PARKING_PAGE_TEXT_SIGNATURES:
        if signature in haystack:
            return f"page text matches a known server-default/parking-page signature ('{signature}')"
    return None


@dataclasses.dataclass(frozen=True)
class PhoneFinding:
    number: str          # E.164
    phone_type: str      # 'mobile' | 'landline' | 'whatsapp' | 'wechat' | 'fax' | 'other'


@dataclasses.dataclass(frozen=True)
class ContactFindings:
    emails: List[str]
    phone_numbers: List[str]
    source_url: str
    has_contact_form: bool = False
    typed_phone_numbers: List[PhoneFinding] = dataclasses.field(default_factory=list)


def _deobfuscate_email_markers(text: str) -> str:
    """Normalises "info[at]domain.de" / "info(at)domain.de" /
    "info at domain dot de"-style obfuscation into a real "@"/"." --
    see the module-level comment above _AT_MARKERS for why this is a
    text substitution rather than a second regex."""
    for pattern in _AT_MARKERS:
        text = re.sub(pattern, "@", text, flags=re.IGNORECASE)
    for pattern in _DOT_MARKERS:
        text = re.sub(pattern, ".", text, flags=re.IGNORECASE)
    return text


def _filter_email_candidate(match: str) -> Optional[str]:
    """Shared junk/image-extension filter used by both `extract_emails`
    (regex matches over page text) and `extract_mailto_emails` (already-
    structured `mailto:` href values) -- same rejection rules either
    way, so a junk address isn't trusted just because it came from an
    href instead of free text."""
    candidate = match.lower().strip(".")
    local_part, _, domain = candidate.partition("@")
    if not domain:
        return None
    tld = domain.rsplit(".", 1)[-1]
    if tld in _IMAGE_EXTENSION_TLDS:
        return None
    if local_part in _JUNK_LOCAL_PARTS:
        return None
    if domain in _JUNK_DOMAINS:
        return None
    return candidate


def extract_emails(text: str) -> List[str]:
    """Every plausible, non-junk email address in `text`, deduplicated
    and lowercased, in first-seen order. See the module docstring for
    why `_IMAGE_EXTENSION_TLDS` matters here specifically. Also finds
    obfuscated addresses (see `_deobfuscate_email_markers`) by re-
    running this same regex over a de-obfuscated copy of `text` and
    unioning the results -- both passes share the identical filtering
    below, so an obfuscated junk address is rejected exactly like a
    plain one would be.
    """
    seen: List[str] = []
    seen_set = set()
    candidates = _EMAIL_RE.findall(text) + _EMAIL_RE.findall(_deobfuscate_email_markers(text))
    for match in candidates:
        candidate = _filter_email_candidate(match)
        if candidate and candidate not in seen_set:
            seen_set.add(candidate)
            seen.append(candidate)
    return seen


def extract_mailto_emails(mailto_hrefs: List[str]) -> List[str]:
    """Emails found in `mailto:` href attributes -- see
    `collection/site_collector.py`'s `_find_mailto_emails` for where
    these come from (the href value itself, already stripped of the
    `mailto:` scheme and any `?subject=`/`?body=` query string).

    Exists because a real, common pattern defeats `extract_emails`
    entirely: a page renders "Click Here" or an icon as the visible
    link text specifically to deter naive text-scraping, while the
    real address sits untouched in the href for any actual browser (or
    person) to use. `extract_emails` only ever sees rendered text, so
    it is structurally blind to this -- found via a real UK supplier
    site (truckmasters.co.uk) whose contact page's visible text says
    only "Email: Click Here" but whose href is
    `mailto:mail@truckmasters.co.uk`.

    Same junk-filtering discipline as `extract_emails`
    (`_filter_email_candidate`), plus a shape check against the same
    email regex -- an href value isn't guaranteed to be a syntactically
    valid address just because it starts with `mailto:`."""
    seen: List[str] = []
    seen_set = set()
    for href_value in mailto_hrefs:
        if not _EMAIL_RE.fullmatch(href_value.strip()):
            continue
        candidate = _filter_email_candidate(href_value)
        if candidate and candidate not in seen_set:
            seen_set.add(candidate)
            seen.append(candidate)
    return seen


def extract_phone_numbers(text: str, default_region: Optional[str] = None) -> List[str]:
    """Every valid phone number `phonenumbers` can find in `text`,
    formatted E.164, deduplicated, in first-seen order.

    `default_region` (an ISO 3166-1 alpha-2 code, e.g. "CN") lets
    `phonenumbers.PhoneNumberMatcher` recognise a locally-formatted
    number with no country code — pass the supplier's own country
    when known. Without it, only numbers already written with an
    explicit `+countrycode` prefix are found, which is still the
    common case for a "Contact Us" page aimed at international buyers.
    Never raises on malformed input; `phonenumbers` itself is
    defensive about this, matching every other extractor in this
    codebase's "never abort a batch on one bad record" discipline.
    """
    seen: List[str] = []
    seen_set = set()
    try:
        matches = phonenumbers.PhoneNumberMatcher(text, default_region)
        for match in matches:
            formatted = phonenumbers.format_number(
                match.number, phonenumbers.PhoneNumberFormat.E164
            )
            if formatted not in seen_set:
                seen_set.add(formatted)
                seen.append(formatted)
    except Exception:
        # Defensive only -- phonenumbers is not known to raise on
        # arbitrary text, but a third-party parsing library failing on
        # one page's malformed content must never abort a batch run.
        return seen
    return seen


# How far back (characters) from a matched number's start position to
# scan for a WhatsApp/WeChat/fax label -- see _classify_phone_type.
_TYPE_CONTEXT_WINDOW = 40
_WHATSAPP_KEYWORDS = ("whatsapp",)
_WECHAT_KEYWORDS = ("wechat", "weixin", "微信")
_FAX_KEYWORDS = ("fax", "传真")


def _classify_phone_type(parsed_number: "phonenumbers.PhoneNumber", context_text: str) -> str:
    """'mobile' | 'landline' | 'whatsapp' | 'wechat' | 'fax' | 'other'.

    `context_text` is a short window of page text immediately
    preceding the matched number -- checked for a WhatsApp/WeChat/fax
    label, which OVERRIDES phonenumbers' own mobile/landline
    classification: a labelled use is a more specific signal than the
    number's format alone (a WhatsApp number IS ordinarily a mobile
    number; the label is what makes it actionable for the "reaches a
    salesperson directly" purpose the label exists to signal).
    Everything else falls back to `phonenumbers.number_type()`, which
    classifies mobile vs. landline for free -- no LLM call, matching
    this module's whole reason for existing (see module docstring)."""
    haystack = context_text.lower()
    if any(kw in haystack for kw in _FAX_KEYWORDS):
        return "fax"
    if any(kw in haystack for kw in _WHATSAPP_KEYWORDS):
        return "whatsapp"
    if any(kw in haystack for kw in _WECHAT_KEYWORDS):
        return "wechat"
    return _number_type_to_phone_type(parsed_number)


def _number_type_to_phone_type(parsed_number: "phonenumbers.PhoneNumber") -> str:
    """The format-only half of `_classify_phone_type` (no context text
    to check a whatsapp/wechat/fax label against) -- shared with
    `extract_tel_phones`, which parses an isolated `tel:` href with no
    surrounding page text to draw a label from."""
    number_type = phonenumbers.number_type(parsed_number)
    if number_type == phonenumbers.PhoneNumberType.MOBILE:
        return "mobile"
    if number_type == phonenumbers.PhoneNumberType.FIXED_LINE:
        return "landline"
    if number_type == phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE:
        return "mobile"  # ambiguous by format alone -- mobile is the more useful default
    return "other"


def extract_typed_phone_numbers(text: str, default_region: Optional[str] = None) -> List[PhoneFinding]:
    """Like `extract_phone_numbers`, but classifies each number's type
    (see `_classify_phone_type`) instead of returning a flat list of
    bare numbers -- `extract_phone_numbers` itself is untouched, so
    every existing caller of that function sees no change in
    behaviour. Deduplicated by (number, type) pair, in first-seen
    order -- the SAME number found again under a DIFFERENT type (e.g.
    also explicitly labelled WhatsApp elsewhere on the page) is kept
    as a second, legitimate finding, not merged away.

    Found via a real calibration run: 6 of 20 suppliers had a mobile
    number displayed on their site that the old (still-existing,
    untouched) `extract_phone_numbers`/`enrich_contact_details` path
    silently discarded -- only the first number found ever made it
    into storage. This is the fix: every number is kept, with enough
    context (a type label) to tell them apart.
    """
    findings: List[PhoneFinding] = []
    seen: set = set()
    try:
        matches = phonenumbers.PhoneNumberMatcher(text, default_region)
        previous_end = 0
        for match in matches:
            formatted = phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.E164)
            # Bounded by the previous match's end, not just a fixed
            # lookback -- otherwise a label like "WhatsApp:" can bleed
            # into a SECOND, closely-following number's context window
            # ("WhatsApp: +...111, WeChat: +...222" would otherwise
            # misclassify the WeChat number as WhatsApp too, since
            # "whatsapp" is still within a naive fixed-size window).
            context_start = max(0, match.start - _TYPE_CONTEXT_WINDOW, previous_end)
            context = text[context_start:match.start]
            phone_type = _classify_phone_type(match.number, context)
            key = (formatted, phone_type)
            if key not in seen:
                seen.add(key)
                findings.append(PhoneFinding(number=formatted, phone_type=phone_type))
            previous_end = match.end
    except Exception:
        # Same defensive discipline as extract_phone_numbers above.
        return findings
    return findings


def extract_tel_phones(tel_hrefs: List[str], default_region: Optional[str] = None) -> List[PhoneFinding]:
    """Phone numbers found in `tel:` href attributes -- see
    `collection/site_collector.py`'s `_find_tel_phones` for where these
    come from (the href value itself, already stripped of the `tel:`
    scheme). Parsed individually via `phonenumbers.parse` rather than
    `PhoneNumberMatcher`: a `tel:` value is already a single isolated
    candidate, not free text to scan for matches within, and there is
    no surrounding context to classify whatsapp/wechat/fax from (see
    `_number_type_to_phone_type`, the context-free half of
    `_classify_phone_type`).

    Same `default_region` behaviour as `extract_phone_numbers` -- a
    national-format `tel:` value (no `+countrycode`) needs the hint to
    parse at all. Never raises, same defensive discipline as every
    other function here."""
    findings: List[PhoneFinding] = []
    seen: set = set()
    for href_value in tel_hrefs:
        try:
            parsed = phonenumbers.parse(href_value, default_region)
            if not phonenumbers.is_valid_number(parsed):
                continue
            formatted = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        except Exception:
            continue
        phone_type = _number_type_to_phone_type(parsed)
        key = (formatted, phone_type)
        if key not in seen:
            seen.add(key)
            findings.append(PhoneFinding(number=formatted, phone_type=phone_type))
    return findings


def country_name_to_region_code(country_name: Optional[str]) -> Optional[str]:
    """Best-effort ISO 3166-1 alpha-2 lookup for `extract_phone_numbers`'s
    `default_region` — e.g. "China" -> "CN". Returns `None` on any
    failure to resolve (unrecognised name, `None` input) rather than
    raising; a missing region hint just means phone extraction falls
    back to explicit `+countrycode`-prefixed numbers only, which is
    still useful, not a hard failure.
    """
    if not country_name:
        return None
    try:
        import pycountry

        match = pycountry.countries.search_fuzzy(country_name)
        return match[0].alpha_2 if match else None
    except (LookupError, ImportError):
        return None


def extract_contact_details(
    pages: List[object], *, default_region: Optional[str] = None
) -> List[ContactFindings]:
    """Convenience wrapper over multiple
    `scrapers.own_website_scraper.OwnWebsitePage`-shaped objects
    (anything with `.url` and `.text`) — mirrors
    `CapabilityExtractor.extract_from_pages`'s exact shape so both can
    be called over the identical already-fetched page list in the
    pipeline stage.

    A page contributes a finding if it has an email, a phone number,
    OR a contact form (`page.has_contact_form`, set by
    `OwnWebsiteScraper` at fetch time) — the third case matters because
    a real, common pattern is a site with *no* visible email or phone
    at all, only a form. Without surfacing that page's URL, the honest
    output for that supplier would be "no contact info," when the
    accurate answer is "no email, but here's exactly where to reach
    them."

    A page that `parking_page_reason` flags as a server-default/
    parking/holding page contributes NOTHING -- skipped before any
    regex runs, same "nothing on a junk page is trusted" discipline
    name/address extraction already apply. Found via a real
    calibration run: a bare nginx default page's own boilerplate text
    produced a syntactically-valid-looking fake email.

    Also merges in `mailto:`/`tel:` href-attribute findings
    (`extract_mailto_emails`/`extract_tel_phones`) when `page` carries
    `mailto_emails`/`tel_phones` lists (via `getattr`, defaulting to
    `[]` -- `scrapers.own_website_scraper.OwnWebsitePage` doesn't set
    these, so it's unaffected). A page whose visible text obfuscates
    its contact details behind "Click Here"-style link text but
    exposes the real address/number in the href is otherwise invisible
    to the text-only regex scan above -- see `extract_mailto_emails`'s
    own docstring for a real example.
    """
    findings = []
    for page in pages:
        if parking_page_reason(page.text):
            continue
        emails = extract_emails(page.text)
        for email in extract_mailto_emails(getattr(page, "mailto_emails", [])):
            if email not in emails:
                emails.append(email)

        phones = extract_phone_numbers(page.text, default_region=default_region)
        typed_phones = extract_typed_phone_numbers(page.text, default_region=default_region)
        typed_seen = {(t.number, t.phone_type) for t in typed_phones}
        for typed in extract_tel_phones(getattr(page, "tel_phones", []), default_region=default_region):
            if typed.number not in phones:
                phones.append(typed.number)
            key = (typed.number, typed.phone_type)
            if key not in typed_seen:
                typed_seen.add(key)
                typed_phones.append(typed)

        has_form = getattr(page, "has_contact_form", False)
        if emails or phones or has_form:
            findings.append(ContactFindings(
                emails=emails, phone_numbers=phones, source_url=page.url, has_contact_form=has_form,
                typed_phone_numbers=typed_phones,
            ))
    return findings


def best_contact_method(findings: List[ContactFindings]) -> dict:
    """Collapse multi-page findings into the single best way to reach
    a supplier, for display: an email if any page had one, otherwise
    a phone number, otherwise the URL of a page with a contact form,
    otherwise nothing. Returns
    `{'method': 'email'|'phone'|'contact_form'|None, 'value': str|None}`
    -- `value` is an email address, a phone number, or a page URL
    depending on `method`, so a caller can display it without needing
    to know which case fired.
    """
    for finding in findings:
        if finding.emails:
            return {"method": "email", "value": finding.emails[0]}
    for finding in findings:
        if finding.phone_numbers:
            return {"method": "phone", "value": finding.phone_numbers[0]}
    for finding in findings:
        if finding.has_contact_form:
            return {"method": "contact_form", "value": finding.source_url}
    return {"method": None, "value": None}
    return findings
