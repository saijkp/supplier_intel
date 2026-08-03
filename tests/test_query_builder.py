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
        assert len(queries) == 3

    def test_product_is_quoted_in_every_query(self):
        queries = build_queries("trailer axle")
        for query in queries:
            assert '"trailer axle"' in query

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
        assert len(queries) == 3

    def test_queries_include_manufacturer_supplier_and_factory_variants(self):
        queries = build_queries("trailer axle")
        joined = " ".join(queries)
        assert "manufacturer" in joined
        assert "supplier" in joined
        assert "factory" in joined
