"""
tests/test_discovery_candidate_validator.py

Tests for discovery/candidate_validator.py -- the one LLM call in
Discovery Service, and the concrete anti-hallucination gate: a
candidate is only validated if the LLM's extracted name (read from a
real fetched page) corroborates the ORIGINAL search result, AND the
page text actually mentions the searched product term. Uses fakes for
both the LLM client and the website fetcher -- no real network/API key.
"""

from __future__ import annotations

from types import SimpleNamespace

from discovery.candidate_extractor import Candidate
from discovery.candidate_validator import CandidateValidator


class FakeLLMClient:
    def __init__(self, response=None):
        self._response = response

    def complete_json(self, system_prompt, user_prompt, **kwargs):
        return self._response


class FakeWebsiteFetcher:
    def __init__(self, success=True, pages=None, error=None):
        self._success = success
        self._pages = pages if pages is not None else [SimpleNamespace(text="")]
        self._error = error
        self.calls = []

    def fetch(self, domain):
        self.calls.append(domain)
        return SimpleNamespace(success=self._success, pages=self._pages, error=self._error)


class ExplodingWebsiteFetcher:
    def fetch(self, domain):
        raise RuntimeError("network down")


def _candidate(title="Acme Trailer Co", snippet="Leading manufacturer of trailer axles"):
    return Candidate(title=title, link="https://acmetrailer.com/", snippet=snippet, domain="acmetrailer.com")


class TestCandidateValidator:

    def test_fully_corroborated_candidate_is_validated(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Welcome to Acme Trailer Co, manufacturer of trailer axle assemblies since 1998.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": "United Kingdom"})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is True
        assert result.extracted_name == "Acme Trailer Co"
        assert result.extracted_country == "United Kingdom"

    def test_fetch_failure_is_not_validated(self):
        fetcher = FakeWebsiteFetcher(success=False, error="404 not found")
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co"})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is False
        assert "could not fetch" in result.reason

    def test_fetcher_raising_does_not_propagate(self):
        validator = CandidateValidator(website_fetcher=ExplodingWebsiteFetcher(), llm_client=FakeLLMClient())
        result = validator.validate(_candidate(), "trailer axle")  # must not raise
        assert result.validated is False

    def test_empty_page_text_is_not_validated(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(text="")])
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=FakeLLMClient())
        result = validator.validate(_candidate(), "trailer axle")
        assert result.validated is False
        assert "no readable text" in result.reason

    def test_llm_returning_none_is_not_validated(self):
        """LLMClient itself already returns None on any failure --
        CandidateValidator must degrade gracefully, never crash."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(text="Some real page text about axles.")])
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=FakeLLMClient(response=None))
        result = validator.validate(_candidate(), "trailer axle")
        assert result.validated is False
        assert "invalid JSON" in result.reason

    def test_llm_returning_non_dict_is_not_validated(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(text="Some real page text.")])
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=FakeLLMClient(response=["not", "a", "dict"]))
        result = validator.validate(_candidate(), "trailer axle")
        assert result.validated is False

    def test_null_company_name_is_not_validated_never_invented(self):
        """The core anti-hallucination behaviour: if the LLM honestly
        reports no name was found in the text, the candidate is
        rejected -- never falls back to guessing from the domain or
        search snippet."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(text="A page with no clear company name stated.")])
        llm = FakeLLMClient(response={"company_name": None, "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is False
        assert result.extracted_name is None
        assert "no company name found" in result.reason

    def test_extracted_name_not_matching_search_result_is_rejected(self):
        """The extracted name must corroborate the ORIGINAL search
        result -- protects against a fetched page being an unrelated
        company that happens to share the candidate domain (e.g. a
        domain that changed hands)."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(text="Welcome to Totally Different Corp.")])
        llm = FakeLLMClient(response={"company_name": "Totally Different Corp", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(title="Acme Trailer Co", snippet="trailer axle manufacturer"), "trailer axle")

        assert result.validated is False
        assert "does not match the original search result" in result.reason

    def test_page_not_mentioning_product_term_is_rejected(self):
        """Deterministic keyword check, not another LLM call -- a
        genuinely matched company whose fetched page happens not to
        mention the specific searched product must not be accepted."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Welcome to Acme Trailer Co, a leading industrial manufacturer.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is False
        assert "does not mention the searched term" in result.reason

    def test_company_name_with_only_whitespace_is_rejected(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(text="Some real text about trailer axle.")])
        llm = FakeLLMClient(response={"company_name": "   ", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)
        result = validator.validate(_candidate(), "trailer axle")
        assert result.validated is False


class TestSelfDeclaredTraderExclusion:
    """The global, country-agnostic trader signal sourcing.
    SourcingAgentService needs -- the codebase's only other trader
    signal (ManufacturerVerifier via Qichacha) only has data for
    China-registered companies. This one is mechanical (a fixed phrase
    list), not another LLM call, checked only after every other gate
    already passed."""

    def test_self_declared_trading_company_is_rejected(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Welcome to Acme Trailer Co. We are a trading company specializing in trailer axle parts.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is False
        assert "trading company" in result.reason

    def test_self_declared_distributor_is_rejected(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Acme Trailer Co -- we are a distributor of trailer axle products across Europe.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is False
        assert "excluded, not a manufacturer" in result.reason

    def test_check_is_case_insensitive(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="ACME TRAILER CO -- WE ARE A TRADING COMPANY dealing in trailer axle parts.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is False

    def test_ordinary_mention_of_trading_does_not_trip_the_filter(self):
        """Precision matters more than recall here -- a genuine
        manufacturer's page mentioning "trading partners" in passing
        must not be excluded just for containing the word "trading"."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Acme Trailer Co manufactures trailer axle assemblies in-house. "
                 "We value our long-standing trading partners across the supply chain.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is True

    def test_genuine_manufacturer_page_is_still_validated(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Acme Trailer Co is a manufacturer of trailer axle assemblies, operating our own factory since 1998.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is True
