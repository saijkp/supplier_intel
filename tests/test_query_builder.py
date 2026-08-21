"""
tests/test_query_builder.py

Tests for discovery/query_builder.py -- pure string building, no LLM,
no network.
"""

from __future__ import annotations

from discovery.query_builder import build_queries


class TestBuildQueries:

    def test_returns_one_query_per_template(self):
        queries = build_queries("trailer axle")
        assert len(queries) == 4

    def test_first_three_queries_are_exact_phrase_quoted(self):
        queries = build_queries("trailer axle")
        for query in queries[:3]:
            assert '"trailer axle"' in query

    def test_last_query_is_an_unquoted_fallback(self):
        """Real bug this guards against: an exact-phrase search for a
        less common product phrasing (e.g. "wheel bearing units") can
        return almost nothing, since most real listings use slightly
        different wording. The unquoted variant is last so it's only
        reached once the precise, quoted variants didn't already find
        enough (see discover()'s own early-stop-once-max_candidates
        loop)."""
        queries = build_queries("trailer axle")
        assert queries[-1] == "trailer axle manufacturer"
        assert '"trailer axle"' not in queries[-1]

    def test_country_is_appended_when_given(self):
        queries = build_queries("trailer axle", country="China")
        for query in queries:
            assert query.endswith("China")

    def test_no_country_suffix_when_omitted(self):
        queries = build_queries("trailer axle")
        for query in queries:
            assert "China" not in query

    def test_category_does_not_break_query_building(self):
        """category is accepted for the CLI/API surface and
        discovery_runs.category, not yet folded into the query text --
        this should not raise or produce a malformed query."""
        queries = build_queries("trailer axle", category="Axles & Suspension")
        assert len(queries) == 4

    def test_queries_include_manufacturer_supplier_and_factory_variants(self):
        queries = build_queries("trailer axle")
        joined = " ".join(queries)
        assert "manufacturer" in joined
        assert "supplier" in joined
        assert "factory" in joined


class TestApplicationAndKeySpecifications:

    def test_application_adds_one_extra_query(self):
        queries = build_queries("winch", application="off-road trailer recovery")
        assert len(queries) == 5
        assert any("off-road trailer recovery" in q for q in queries)

    def test_key_specifications_add_one_extra_query(self):
        queries = build_queries("winch", key_specifications=["12V", "5000lb capacity"])
        assert len(queries) == 5
        assert any("12V" in q and "5000lb capacity" in q for q in queries)

    def test_application_and_key_specifications_together_add_two_extra_queries(self):
        queries = build_queries(
            "winch", application="off-road trailer recovery", key_specifications=["12V"],
        )
        assert len(queries) == 6

    def test_country_still_appended_to_the_new_variants(self):
        queries = build_queries("winch", country="China", application="off-road trailer recovery")
        for query in queries:
            assert query.endswith("China")

    def test_neither_given_matches_original_four_query_behaviour(self):
        queries = build_queries("winch")
        assert len(queries) == 4


class TestExtraRoleWords:
    """Real bug this ordering guards against: discover()'s own
    candidate-collection loop stops as soon as max_candidates raw
    candidates accumulate, processing queries in this function's
    returned order. If role-word queries were appended last (as they
    used to be), a call that explicitly asked for them could still have
    them never run at all, silently starved out by the base templates
    filling the budget first -- confirmed live on a real Material
    Handling discovery run where manufacturer-framed queries alone
    dominated the budget while dealer/distributor-framed queries (the
    actually useful ones for that category) never even executed."""

    def test_adds_one_query_per_role_word(self):
        queries = build_queries("forklift", extra_role_words=["dealer", "distributor"])
        assert len(queries) == 6  # 4 base + 2 role-word

    def test_role_word_queries_come_first(self):
        queries = build_queries("forklift", extra_role_words=["dealer", "distributor"])
        assert '"forklift" dealer' == queries[0]
        assert '"forklift" distributor' == queries[1]

    def test_base_templates_still_present_after_role_words(self):
        queries = build_queries("forklift", extra_role_words=["dealer"])
        joined = " ".join(queries[1:])
        assert "manufacturer" in joined
        assert "supplier" in joined
        assert "factory" in joined

    def test_country_appended_to_role_word_queries_too(self):
        queries = build_queries("forklift", country="United Kingdom", extra_role_words=["dealer"])
        assert queries[0].endswith("United Kingdom")

    def test_none_has_no_effect_on_default_output(self):
        with_none = build_queries("trailer axle", extra_role_words=None)
        without_arg = build_queries("trailer axle")
        assert with_none == without_arg

    def test_empty_list_has_no_effect_on_default_output(self):
        with_empty = build_queries("trailer axle", extra_role_words=[])
        without_arg = build_queries("trailer axle")
        assert with_empty == without_arg
