"""
verification/uscc_validator.py

Validates China's Unified Social Credit Code (统一社会信用代码, USCC) —
the 18-character registry ID every Chinese legal entity has held since
the GB 32100-2015 standard was introduced. Used to sanity-check a USCC
before spending a Qichacha API call on it, and to catch scraped/typo'd
codes before they ever reach storage.

Structure (18 characters total):
  - Position 1:     registration authority type code
  - Position 2:     entity category code
  - Positions 3-8:  administrative division code (6 digits)
  - Positions 9-17: organisation code (9 chars)
  - Position 18:    check digit, computed from positions 1-17

Character set excludes the letters I, O, S, V, Z (visually confusable
with digits), leaving 31 valid characters: 0-9 plus 21 letters.
"""

from __future__ import annotations

import re
from typing import Optional

# The 31-character alphabet used by USCC codes, in check-digit weight order.
CHARSET = "0123456789ABCDEFGHJKLMNPQRTUWXY"

# Standard GB 32100-2015 position weights for the first 17 characters.
WEIGHTS = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)

_FORMAT_RE = re.compile(rf"^[{CHARSET}]{{18}}$")

_CHAR_VALUES = {c: i for i, c in enumerate(CHARSET)}


def has_valid_format(uscc: Optional[str]) -> bool:
    """Length and character-set check only — does not verify the check
    digit. Useful as a cheap pre-filter before the more expensive
    checksum computation, and for flagging obviously-garbled scrape
    output early."""
    if not uscc:
        return False
    return bool(_FORMAT_RE.match(uscc.strip().upper()))


def compute_check_character(prefix17: str) -> Optional[str]:
    """Compute the expected 18th character for a 17-character prefix.
    Returns None if the prefix contains characters outside CHARSET."""
    try:
        total = sum(_CHAR_VALUES[c] * w for c, w in zip(prefix17, WEIGHTS))
    except KeyError:
        return None
    remainder = total % 31
    check_value = (31 - remainder) % 31
    return CHARSET[check_value]


def has_valid_checksum(uscc: str) -> bool:
    """Assumes `uscc` has already passed has_valid_format(); recomputes
    the check digit from positions 1-17 and compares to position 18."""
    prefix, actual_check = uscc[:17], uscc[17]
    expected_check = compute_check_character(prefix)
    return expected_check is not None and expected_check == actual_check


def is_valid_uscc(uscc: Optional[str]) -> bool:
    """Full validation: correct length/charset AND correct check digit.
    This is what verification/qichacha.py calls before spending an API
    request on a USCC — a code that fails this can't possibly be real,
    so there's no point looking it up."""
    if not uscc:
        return False
    candidate = uscc.strip().upper()
    return has_valid_format(candidate) and has_valid_checksum(candidate)


def region_code(uscc: str) -> Optional[str]:
    """Return the 6-digit administrative division code (positions 3-8),
    or None if the input isn't valid-format. Useful for a quick
    'does this claimed China-based supplier's USCC region look plausible'
    sanity check."""
    if not has_valid_format(uscc):
        return None
    return uscc.strip().upper()[2:8]
