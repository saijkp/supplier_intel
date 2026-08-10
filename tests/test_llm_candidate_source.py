"""
tests/test_llm_candidate_source.py

Tests for discovery/llm_candidate_source.py -- the LLM-proposed
candidate-generation path alongside SerpAPI in discovery_service.py.
Fakes the LLM client entirely (no network/API key needed); nothing
here touches CandidateValidator or SupplierMatcher -- those are covered
in tests/test_discovery_candidate_validator.py and
tests/test_discovery_service.py respectively, since this module's own
job stops at "produce a list of Candidate objects," never storage.
"""

from __future__ import annotations

import pytest

from discovery.llm_candidate_source import LLMCandidateSource


class FakeLLMClient:
    """Returns `responses` in call order (one per complete_json() call),
    or a single fixed `response` for every call. `raise_on_call_index`
    injects an exception on a specific 0-indexed call, for
    fault-isolation tests -- mirrors FakeCandidateValidator's
    `raise_for_domain` convention elsewhere in this test suite."""

    def __init__(self, response=None, responses=None, raise_on_call_index=None):
        self._response = response
        self._responses = responses
        self._raise_on_call_index = raise_on_call_index
        self.calls = []

    def complete_json(self, system_prompt, user_prompt, **kwargs):
        index = len(self.calls)
        self.calls.append((system_prompt, user_prompt))
        if self._raise_on_call_index is not None and index == self._raise_on_call_index:
            raise RuntimeError("llm exploded")
        if self._responses is not None:
            return self._responses[index] if index < len(self._responses) else None
        return self._response


def _item(company_name="Acme Trailer Co", website="https://acmetrailer.com", **overrides):
    item = {
        "company_name": company_name, "website": website,
        "country": "China", "city": "Ningbo", "why_relevant": "makes trailer axles",
    }
    item.update(overrides)
    return item


class TestFindCandidatesHappyPath:

    def test_valid_response_produces_a_candidate(self):
        llm = FakeLLMClient(response=[_item()])
        source = LLMCandidateSource(llm_client=llm)

        candidates, stats = source.find_candidates("trailer axle")

        assert len(candidates) == 1
        c = candidates[0]
        assert c.title == "Acme Trailer Co"
        assert c.link == "https://acmetrailer.com"
        assert c.snippet == "makes trailer axles"
        assert c.domain == "acmetrailer.com"

    def test_stats_track_the_funnel(self):
        llm = FakeLLMClient(response=[_item()])
        source = LLMCandidateSource(llm_client=llm)

        candidates, stats = source.find_candidates("trailer axle")

        assert stats.raw_generated >= 1
        assert stats.deduplicated == len(candidates)
        assert stats.dropped_incomplete == 0
        assert stats.dropped_unusable_domain == 0


class TestFindCandidatesFiltering:

    # These isolate a single prompt variation (via `responses=` with only
    # the first call populated -- FakeLLMClient falls back to None for
    # any later call) so the counts aren't entangled with
    # _build_prompt_variations's own multi-variation behaviour, which
    # TestPromptVariations/TestFindCandidatesDeduplication cover
    # separately.

    def test_missing_company_name_is_dropped(self):
        llm = FakeLLMClient(responses=[[_item(company_name=None), _item(company_name="   ")]])
        source = LLMCandidateSource(llm_client=llm)

        candidates, stats = source.find_candidates("trailer axle")

        assert candidates == []
        assert stats.dropped_incomplete == 2

    def test_missing_website_is_dropped(self):
        llm = FakeLLMClient(responses=[[_item(website=None), _item(website="")]])
        source = LLMCandidateSource(llm_client=llm)

        candidates, stats = source.find_candidates("trailer axle")

        assert candidates == []
        assert stats.dropped_incomplete == 2

    def test_non_dict_items_are_skipped_silently(self):
        llm = FakeLLMClient(responses=[["not a dict", 42, None, _item()]])
        source = LLMCandidateSource(llm_client=llm)

        candidates, stats = source.find_candidates("trailer axle")

        assert len(candidates) == 1
        assert stats.raw_generated == 1  # only the real dict counted

    def test_platform_domain_is_filtered_out(self):
        llm = FakeLLMClient(responses=[[_item(website="https://acme.en.alibaba.com")]])
        source = LLMCandidateSource(llm_client=llm)

        candidates, stats = source.find_candidates("trailer axle")

        assert candidates == []
        assert stats.dropped_unusable_domain == 1

    def test_bare_domain_without_scheme_is_accepted(self):
        """The model won't always return a fully-qualified URL --
        extract_domain already handles a bare domain string."""
        llm = FakeLLMClient(response=[_item(website="acmetrailer.com")])
        source = LLMCandidateSource(llm_client=llm)

        candidates, _ = source.find_candidates("trailer axle")

        assert candidates[0].domain == "acmetrailer.com"


class TestFindCandidatesDeduplication:

    def test_same_domain_across_variations_is_deduplicated(self):
        """English and Mandarin variations both surfacing the same real
        company must not produce two candidates -- this is the whole
        point of running several variations and deduplicating."""
        llm = FakeLLMClient(response=[_item(website="https://acmetrailer.com")])
        source = LLMCandidateSource(llm_client=llm)

        candidates, stats = source.find_candidates("trailer axle", country="China")

        assert len(llm.calls) > 1  # more than one variation actually ran
        assert len(candidates) == 1
        assert stats.raw_generated > 1  # counted once per variation, before dedup
        assert stats.deduplicated == 1

    def test_www_prefix_and_bare_domain_dedupe_to_the_same_candidate(self):
        llm = FakeLLMClient(responses=[
            [_item(website="https://www.acmetrailer.com")],
            [_item(website="acmetrailer.com")],
        ])
        source = LLMCandidateSource(llm_client=llm)

        candidates, stats = source.find_candidates("trailer axle")

        assert len(candidates) == 1
        assert stats.deduplicated == 1


class TestFindCandidatesMaxCandidates:

    def test_stops_once_max_candidates_reached(self):
        llm = FakeLLMClient(response=[
            _item(company_name=f"Company {i}", website=f"https://company{i}.example.com")
            for i in range(10)
        ])
        source = LLMCandidateSource(llm_client=llm)

        candidates, _ = source.find_candidates("trailer axle", max_candidates=3)

        assert len(candidates) == 3


class TestFindCandidatesFaultIsolation:

    def test_non_list_response_is_skipped_not_raised(self):
        llm = FakeLLMClient(responses=[{"not": "a list"}, [_item()]])
        source = LLMCandidateSource(llm_client=llm)

        candidates, _ = source.find_candidates("trailer axle", country="China")  # must not raise

        assert len(candidates) == 1

    def test_none_response_is_skipped_not_raised(self):
        llm = FakeLLMClient(response=None)
        source = LLMCandidateSource(llm_client=llm)

        candidates, stats = source.find_candidates("trailer axle")  # must not raise

        assert candidates == []
        assert stats.raw_generated == 0

    def test_one_variation_raising_does_not_abort_the_others(self):
        # Call index 0 raises; every later call returns a real candidate.
        llm = FakeLLMClient(response=[_item()], raise_on_call_index=0)
        source = LLMCandidateSource(llm_client=llm)

        candidates, _ = source.find_candidates("trailer axle")  # must not raise

        assert len(llm.calls) > 1  # more than one variation actually ran
        assert len(candidates) == 1  # every variation after the exploding one still ran


class TestPromptVariations:

    def test_runs_more_than_one_variation(self):
        source = LLMCandidateSource(llm_client=FakeLLMClient(response=[]))
        variations = source._build_prompt_variations("trailer axle", country=None)
        assert len(variations) >= 2

    def test_includes_a_mandarin_variation(self):
        source = LLMCandidateSource(llm_client=FakeLLMClient(response=[]))
        variations = source._build_prompt_variations("trailer axle", country=None)
        assert any(any(ord(ch) > 0x2E80 for ch in v) for v in variations)  # CJK range

    def test_country_adds_additional_variations(self):
        source = LLMCandidateSource(llm_client=FakeLLMClient(response=[]))
        without_country = source._build_prompt_variations("trailer axle", country=None)
        with_country = source._build_prompt_variations("trailer axle", country="China")
        assert len(with_country) > len(without_country)
        assert any("China" in v for v in with_country)
