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


@dataclasses.dataclass(frozen=True)
class ContactFindings:
    emails: List[str]
    phone_numbers: List[str]
    source_url: str
    has_contact_form: bool = False


def extract_emails(text: str) -> List[str]:
    """Every plausible, non-junk email address in `text`, deduplicated
    and lowercased, in first-seen order. See the module docstring for
    why `_IMAGE_EXTENSION_TLDS` matters here specifically.
    """
    seen: List[str] = []
    seen_set = set()
    for match in _EMAIL_RE.findall(text):
        candidate = match.lower().strip(".")
        local_part, _, domain = candidate.partition("@")
        if not domain:
            continue
        tld = domain.rsplit(".", 1)[-1]
        if tld in _IMAGE_EXTENSION_TLDS:
            continue
        if local_part in _JUNK_LOCAL_PARTS:
            continue
        if domain in _JUNK_DOMAINS:
            continue
        if candidate not in seen_set:
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
    """
    findings = []
    for page in pages:
        emails = extract_emails(page.text)
        phones = extract_phone_numbers(page.text, default_region=default_region)
        has_form = getattr(page, "has_contact_form", False)
        if emails or phones or has_form:
            findings.append(ContactFindings(
                emails=emails, phone_numbers=phones, source_url=page.url, has_contact_form=has_form,
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
