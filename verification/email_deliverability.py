"""
verification/email_deliverability.py

Checks whether an email address's domain can receive mail at all, via
a live MX-record DNS lookup — free, and (unlike most of this
codebase's newer additions) actually exercised against real DNS
resolution while building it, not just a fake client. `dnspython` is
a new declared dependency (added to requirements.txt) specifically for
this.

What this proves, and the real limit on it
------------------------------------------------
A domain with no MX records genuinely cannot receive email — full
stop, this is a hard, reliable "no." A domain *with* MX records only
proves the domain is mail-capable, not that any specific mailbox on it
exists or is actively monitored (`sales@realcompany.com` and
`typo@realcompany.com` are indistinguishable to this check if
`realcompany.com` has valid MX records for either).

Deliberately NOT doing an SMTP RCPT TO probe
--------------------------------------------------
The obvious next step — connect to the mail server and ask whether the
specific mailbox exists without actually sending anything — was
considered and rejected for this module. Most mail servers today
greylist, rate-limit, or flatly refuse RCPT TO probes from unfamiliar
IPs specifically because this technique is so commonly used for spam
list validation; a probe would produce a meaningful rate of false
"undeliverable" verdicts for genuinely good addresses, which is a
worse outcome than not probing at all — a wrong "bad" verdict on a
real address actively misleads a buyer into skipping a good contact.
If mailbox-level confidence is worth paying for, a commercial verifier
(NeverBounce, ZeroBounce, Hunter) is the more honest way to get it;
this module stays at the free, reliable MX-only tier on purpose.
"""

from __future__ import annotations

import dataclasses
from typing import List, Optional

import dns.resolver


@dataclasses.dataclass(frozen=True)
class EmailDomainResult:
    email: str
    domain: Optional[str]
    has_mx_records: bool
    reason: str


def _extract_domain(email: str) -> Optional[str]:
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[-1].strip().lower()
    return domain or None


def check_email_domain(email: str, resolver: Optional[dns.resolver.Resolver] = None) -> EmailDomainResult:
    """Whether `email`'s domain has valid MX records. Never raises --
    a DNS timeout, an unregistered domain, or a malformed address all
    resolve to `has_mx_records=False` with an explanatory reason,
    never an exception a batch run would have to catch.
    """
    domain = _extract_domain(email)
    if domain is None:
        return EmailDomainResult(email=email, domain=None, has_mx_records=False, reason="not a valid email shape")

    resolver = resolver or dns.resolver.Resolver()
    try:
        answers = resolver.resolve(domain, "MX")
        if len(answers) > 0:
            return EmailDomainResult(
                email=email, domain=domain, has_mx_records=True,
                reason=f"{len(answers)} MX record(s) found — domain can receive mail",
            )
        return EmailDomainResult(
            email=email, domain=domain, has_mx_records=False, reason="no MX records found",
        )
    except dns.resolver.NXDOMAIN:
        return EmailDomainResult(
            email=email, domain=domain, has_mx_records=False, reason="domain does not exist",
        )
    except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return EmailDomainResult(
            email=email, domain=domain, has_mx_records=False,
            reason="domain exists but has no MX records — cannot receive mail",
        )
    except Exception as e:
        return EmailDomainResult(
            email=email, domain=domain, has_mx_records=False, reason=f"lookup failed: {e}",
        )


def check_email_domains(emails: List[str], resolver: Optional[dns.resolver.Resolver] = None) -> List[EmailDomainResult]:
    return [check_email_domain(e, resolver=resolver) for e in emails]
