"""
tests/test_email_deliverability.py

Tests for verification.email_deliverability. Two tiers deliberately:
a handful of tests run against real, live DNS (this environment's
sandbox genuinely allows DNS resolution even though most HTTP is
blocked, confirmed before building this module) for real proof the
integration works; the rest use a fake resolver for deterministic
edge cases that can't depend on a specific domain's DNS state staying
the same forever.
"""

from __future__ import annotations

import dns.resolver
import pytest

from verification.email_deliverability import check_email_domain, check_email_domains


class FakeMXRecord:
    def __init__(self, exchange):
        self.exchange = exchange


class FakeResolver:
    def __init__(self, records=None, raise_error=None):
        self._records = records if records is not None else []
        self._raise_error = raise_error
        self.queries = []

    def resolve(self, name, rdtype):
        self.queries.append((name, rdtype))
        if self._raise_error:
            raise self._raise_error
        return self._records


class TestRealLiveDnsLookup:
    """Genuinely exercised against the internet's real DNS, not a
    fake -- these are the actual proof this module works, not just
    that its own mocked logic is internally consistent."""

    def test_a_domain_with_well_known_mx_records_passes(self):
        result = check_email_domain("someone@gmail.com")
        assert result.has_mx_records is True
        assert result.domain == "gmail.com"

    def test_a_domain_that_does_not_exist_fails(self):
        result = check_email_domain("someone@this-domain-genuinely-does-not-exist-anywhere-12345.com")
        assert result.has_mx_records is False


class TestMalformedInput:

    def test_no_at_symbol_is_rejected_without_a_lookup(self):
        result = check_email_domain("not-an-email-at-all")
        assert result.has_mx_records is False
        assert result.domain is None
        assert result.reason == "not a valid email shape"

    def test_empty_string_is_rejected(self):
        result = check_email_domain("")
        assert result.has_mx_records is False


class TestWithFakeResolver:

    def test_domain_with_mx_records_is_deliverable(self):
        resolver = FakeResolver(records=[FakeMXRecord("mail.acme.example.com")])
        result = check_email_domain("sales@acme.example.com", resolver=resolver)
        assert result.has_mx_records is True
        assert "1 MX record" in result.reason

    def test_domain_with_zero_records_is_not_deliverable(self):
        resolver = FakeResolver(records=[])
        result = check_email_domain("sales@acme.example.com", resolver=resolver)
        assert result.has_mx_records is False

    def test_nxdomain_is_reported_distinctly(self):
        resolver = FakeResolver(raise_error=dns.resolver.NXDOMAIN())
        result = check_email_domain("sales@totally-fake-domain.example", resolver=resolver)
        assert result.has_mx_records is False
        assert result.reason == "domain does not exist"

    def test_no_answer_is_reported_distinctly_from_nxdomain(self):
        """A domain that exists (e.g. has A records for a website) but
        has no MX records is a different, common case -- worth its own
        message rather than folding into the same 'does not exist'
        reason."""
        resolver = FakeResolver(raise_error=dns.resolver.NoAnswer())
        result = check_email_domain("sales@website-only-no-email.example", resolver=resolver)
        assert result.has_mx_records is False
        assert "no MX records" in result.reason

    def test_unexpected_exception_does_not_propagate(self):
        resolver = FakeResolver(raise_error=RuntimeError("resolver library exploded"))
        result = check_email_domain("sales@acme.example.com", resolver=resolver)
        assert result.has_mx_records is False
        assert "lookup failed" in result.reason

    def test_domain_is_extracted_and_lowercased(self):
        resolver = FakeResolver(records=[FakeMXRecord("mail.acme.com")])
        result = check_email_domain("Sales@ACME.COM", resolver=resolver)
        assert result.domain == "acme.com"


class TestBulkCheck:

    def test_preserves_order_across_mixed_results(self):
        resolver = FakeResolver(records=[FakeMXRecord("mail.acme.com")])
        results = check_email_domains(["sales@acme.com", "not-an-email"], resolver=resolver)
        assert results[0].has_mx_records is True
        assert results[1].has_mx_records is False

    def test_empty_list_returns_empty(self):
        assert check_email_domains([]) == []
