"""
deduplication/domain_utils.py

Domain extraction and comparison utilities shared by the dedup matcher
and any normalizer that needs to turn a raw URL into a comparable
domain string.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

import tldextract

# Companies list their storefront on these platforms under a subdomain
# of the platform itself (e.g. ledmasters.en.alibaba.com). Two different
# companies can each have a *.alibaba.com subdomain, so matching on the
# platform's registered domain alone would be meaningless — only an
# exact full-domain match should count, and a platform subdomain should
# never be treated as a supplier's "own" verified company website.
PLATFORM_REGISTERED_DOMAINS = {
    "alibaba.com",
    "indiamart.com",
    "hktdc.com",
    "made-in-china.com",
    "globalsources.com",
    "goldsupplier.com",
    "tradeindia.com",
}


def extract_domain(url: str) -> Optional[str]:
    """Normalise a URL or bare-domain string down to a comparable domain:
    lowercase, no scheme, no leading 'www.', no path/query/port. Returns
    None for empty or unparseable input rather than raising."""
    if not url:
        return None
    text = url.strip()
    if not text:
        return None
    try:
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", text):
            text = "https://" + text
        netloc = urlparse(text).netloc.lower()
        netloc = re.sub(r"^www\.", "", netloc)
        netloc = netloc.split(":")[0]  # drop a port if present
        return netloc or None
    except Exception:
        return None


def looks_like_url(text: Optional[str]) -> bool:
    """True if `text` looks like a website URL or bare domain rather than
    a free-text company name -- deliberately simple (no internal
    whitespace, contains a '.'), not a full URL validator. Needed
    because `extract_domain` is NOT safe for this classification: it
    force-prepends a scheme and returns a truthy (if meaningless) netloc
    string for ordinary multi-word input, e.g. `extract_domain("Acme
    Trailer Co")` returns `"acme trailer co"`, not `None`. Known
    tradeoff: a one-word company name containing a literal period (rare)
    would be misrouted -- accepted, matches this codebase's general
    "simple regex over LLM call" discipline."""
    if not text:
        return False
    text = text.strip()
    if not text or " " in text:
        return False
    return "." in text


def is_platform_subdomain(domain: Optional[str]) -> bool:
    """True if `domain` is a subdomain of a known B2B platform (Alibaba,
    IndiaMART, HKTDC, etc.) rather than a supplier's own company website.
    Useful for deciding whether a 'domain' field is trustworthy enough
    to use for exact-match deduplication."""
    if not domain:
        return False
    extracted = tldextract.extract(domain)
    if not extracted.domain or not extracted.suffix:
        return False
    registered_domain = f"{extracted.domain}.{extracted.suffix}".lower()
    return registered_domain in PLATFORM_REGISTERED_DOMAINS


def domains_match(a: Optional[str], b: Optional[str]) -> bool:
    """Normalise both inputs and compare. Two empty/unparseable domains
    never count as a match."""
    domain_a, domain_b = extract_domain(a or ""), extract_domain(b or "")
    return bool(domain_a) and domain_a == domain_b
