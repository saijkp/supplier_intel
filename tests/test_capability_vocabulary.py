"""
tests/test_capability_vocabulary.py

Tests for verification.capability_vocabulary: alias mapping, exact-
match-only discipline, and the vocabulary's own internal consistency.
"""

from __future__ import annotations

import pytest

from verification.capability_vocabulary import (
    CATEGORY_LOGISTICS,
    CATEGORY_MARKET_PRESENCE,
    CATEGORY_OEM_READINESS,
    CATEGORY_PROCESS,
    CATEGORY_STANDARD,
    VOCABULARY,
    map_to_canonical,
    normalise_term,
)
from verification.capability_vocabulary import _build_alias_index as build_alias_index


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


class TestCommercialIntelligenceExtension:
    """Tests for the v9 additions: logistics, market_presence, and
    oem_readiness categories."""

    @pytest.mark.parametrize(
        "surface_form,expected_canonical",
        [
            ("DDP", "ddp shipping"),
            ("delivered duty paid", "ddp shipping"),
            ("FOB only", "fob shipping"),
            ("UK stock", "uk warehouse"),
            ("customs brokerage", "customs expertise"),
        ],
    )
    def test_logistics_terms_map_correctly(self, surface_form, expected_canonical):
        result = map_to_canonical(surface_form)
        assert result is not None
        assert result.canonical == expected_canonical
        assert result.category == CATEGORY_LOGISTICS

    @pytest.mark.parametrize(
        "surface_form,expected_canonical",
        [
            ("UK customers", "serves uk market"),
            ("supplying europe", "serves europe market"),
            ("US customers", "serves north america market"),
            ("australian customers", "serves australia market"),
            ("tier 1 supplier", "oem supplier"),
        ],
    )
    def test_market_presence_terms_map_correctly(self, surface_form, expected_canonical):
        result = map_to_canonical(surface_form)
        assert result is not None
        assert result.canonical == expected_canonical
        assert result.category == CATEGORY_MARKET_PRESENCE

    @pytest.mark.parametrize(
        "surface_form,expected_canonical",
        [
            ("PPAP", "ppap capability"),
            ("production part approval process", "ppap capability"),
            ("CAD support", "cad engineering support"),
            ("full traceability", "traceability system"),
        ],
    )
    def test_oem_readiness_terms_map_correctly(self, surface_form, expected_canonical):
        result = map_to_canonical(surface_form)
        assert result is not None
        assert result.canonical == expected_canonical
        assert result.category == CATEGORY_OEM_READINESS

    def test_ce_marking_added_alongside_existing_standards(self):
        result = map_to_canonical("CE marked")
        assert result is not None
        assert result.canonical == "ce marking"
        assert result.category == CATEGORY_STANDARD

    def test_new_terms_do_not_collide_with_existing_vocabulary(self):
        """Re-running the alias-index build (import-time in the real
        module) would already raise on a collision -- this just
        re-asserts that guarantee explicitly for the new terms."""
        index = build_alias_index()
        assert len(index) >= len(VOCABULARY)
