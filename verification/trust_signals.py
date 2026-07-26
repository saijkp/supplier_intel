"""
verification/trust_signals.py

Free, zero-new-dependency verification checks that add real signal
without a paid API call. Two things, each with an honestly-stated
ceiling on what it actually proves:

1. Phone number validity — is this a real, correctly-structured
   number for its claimed country, per Google's own libphonenumber
   numbering-plan data (via the `phonenumbers` package, already a
   declared dependency, unused until now). This proves the number is
   *plausible*, not that it's *reachable* — a valid-shaped number that
   was disconnected years ago still passes. True reachability needs a
   live carrier lookup (e.g. Twilio Lookup), a paid API this module
   deliberately does not call.

2. E-mark number format sanity-checking — catches an obviously
   malformed or fabricated E-mark claim ("E-MARK CERTIFIED!!!" with no
   number at all). This is NOT verification against the issuing
   authority: the UNECE's DETA type-approval database exists but
   access is restricted to contracting-party governments,
   manufacturers, and designated technical services — there is no
   public API a third-party sourcing tool can query. A well-formed but
   fraudulent E-mark number will pass this check; only a genuinely
   malformed one is caught. Reported as `format_plausible`,
   deliberately never as `verified`, so this distinction can't get
   lost downstream.

   The actual pattern match is delegated to
   `verification.cert_checker.validate_e_mark_format` rather than
   kept as a second, independently-maintained regex here — two
   definitions of the same real-world format drifting apart quietly
   is exactly the kind of bug that erodes trust in a "verified" badge
   without anyone noticing until it's wrong. This module's only
   addition on top of that existing function is the honest "not
   verified against any registry" context a plain boolean can't carry.
"""

from __future__ import annotations

import dataclasses
from typing import List, Optional

import phonenumbers

from verification.cert_checker import validate_e_mark_format


@dataclasses.dataclass(frozen=True)
class PhoneValidityResult:
    number: str
    plausible: bool
    reason: str


@dataclasses.dataclass(frozen=True)
class EmarkFormatResult:
    reported_value: str
    format_plausible: bool
    reason: str


def check_phone_validity(number: str, default_region: Optional[str] = None) -> PhoneValidityResult:
    """Whether `number` is a structurally valid phone number per its
    country's own numbering plan. See module docstring for exactly
    what this does and doesn't prove -- "plausible," never "reachable."
    """
    if not number or not number.strip():
        return PhoneValidityResult(number=number, plausible=False, reason="empty")
    try:
        parsed = phonenumbers.parse(number, default_region)
    except phonenumbers.NumberParseException as e:
        return PhoneValidityResult(number=number, plausible=False, reason=f"could not parse: {e}")
    if phonenumbers.is_valid_number(parsed):
        return PhoneValidityResult(number=number, plausible=True, reason="valid per numbering plan")
    return PhoneValidityResult(
        number=number, plausible=False, reason="not a valid number for its claimed country"
    )


def check_phone_numbers(numbers: List[str], default_region: Optional[str] = None) -> List[PhoneValidityResult]:
    return [check_phone_validity(n, default_region=default_region) for n in numbers]


def check_emark_format(value: str) -> EmarkFormatResult:
    """See module docstring: this checks the *shape* of an E-mark
    claim, never whether it's genuine. `format_plausible=False` is a
    real red flag (an obviously fabricated or garbled claim);
    `format_plausible=True` means only that it looks like a real E-mark
    number would look, nothing more.
    """
    if not value or not value.strip():
        return EmarkFormatResult(reported_value=value, format_plausible=False, reason="empty")
    if validate_e_mark_format(value):
        return EmarkFormatResult(
            reported_value=value, format_plausible=True,
            reason="matches expected E-mark number shape (not verified against any registry -- see module docstring)",
        )
    return EmarkFormatResult(
        reported_value=value, format_plausible=False,
        reason="does not match the expected E-mark number shape (e.g. 'e1 10R-05 12345') -- "
               "worth a closer look before trusting this claim",
    )


def check_emark_numbers(values: List[str]) -> List[EmarkFormatResult]:
    return [check_emark_format(v) for v in values]
