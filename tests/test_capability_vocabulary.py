"""
tests/test_capability_vocabulary.py

Tests for verification.capability_vocabulary: alias mapping, exact-
match-only discipline, and the vocabulary's own internal consistency.
"""

from __future__ import annotations

import pytest

from verification.capability_vocabulary import (
    CATEGORY_PROCESS,
    CATEGORY_STANDARD,
    VOCABULARY,
    map_to_canonical,
    normalise_term,
)


class TestVocabularyStructure:

    def test_canonical_terms_are_unique(self):
        canonicals = [t.canonical for t in VOCABULARY]
        assert len(canonicals) == len(set(canonicals))

    def test_alias_index_builds_without_error(self):
        # _build_alias_index() runs at import time (ALIAS_INDEX) — if any
        # alias were contested between two canonical terms, importing
        # this module would already have raised. Re-importing here and
        # checking every alias resolves is the assertion.
        for term in VOCABULARY:
            for alias in term.aliases:
                assert map_to_canonical(alias) is not None


class TestMapping:

    @pytest.mark.parametrize(
        "surface_form,expected",
        [
            ("Rotomoulding", "rotational moulding"),
            ("roto-moulding", "rotational moulding"),
            ("ROTATIONAL MOLDING", "rotational moulding"),
            ("  injection   molding  ", "injection moulding"),
            ("Hot Dip Galvanizing", "hot dip galvanising"),
            ("E-Mark", "e-mark approval"),
            ("ISO9001", "iso 9001"),
        ],
    )
    def test_real_world_spelling_variants_collapse(self, surface_form, expected):
        result = map_to_canonical(surface_form)
        assert result is not None
        assert result.canonical == expected

    def test_canonical_terms_map_to_themselves(self):
        for term in VOCABULARY:
            assert map_to_canonical(term.canonical).canonical == term.canonical

    def test_unknown_term_maps_to_none(self):
        assert map_to_canonical("hydroforming") is None
        assert map_to_canonical("") is None

    def test_substring_containment_does_not_match(self):
        """The whole point of exact-match-only: a sentence denying a
        capability contains the capability's own name as a substring."""
        assert map_to_canonical("we do not offer injection moulding") is None
        assert map_to_canonical("injection moulding machines for sale") is None

    def test_category_is_reported_correctly(self):
        assert map_to_canonical("rotomoulding").category == CATEGORY_PROCESS
        assert map_to_canonical("iso 9001").category == CATEGORY_STANDARD


class TestNormalisation:

    def test_pure_and_repeatable(self):
        for value in ("Roto-Moulding", "  ISO 9001  ", "e.mark"):
            assert normalise_term(value) == normalise_term(value)

    def test_punctuation_variants_normalise_identically(self):
        assert normalise_term("Co., Ltd.") == normalise_term("Co Ltd")
