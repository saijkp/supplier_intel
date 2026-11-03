"""Furnishing retarget (IK Eng): domain switch, BOM categories, vocabulary.

The platform was built for trailer components; SOURCING_DOMAIN now
defaults to furnishing, with kitchens and wardrobes as the priority
categories. These tests pin that default and check the trailer domain
is still selectable.
"""

from __future__ import annotations

import pytest

from config import settings
from config.settings import domain_config, get_search_terms
from reports.coverage import (
    FURNISHING_BOM_CATEGORIES,
    TRAILER_BOM_CATEGORIES,
    _haystack,
    _matches,
    categories_for,
)
from verification.capability_vocabulary import map_to_canonical
from verification.scorer import SupplierScorer


def test_default_domain_is_furnishing():
    assert settings.SOURCING_DOMAIN == "furnishing"
    assert settings.SWEEP_SEARCH_TERMS == settings.FURNISHING_SEARCH_TERMS
    assert settings.ACTIVE_HS_CODES == settings.FURNISHING_HS_CODES


def test_kitchens_and_wardrobes_lead_the_sweep():
    first = " ".join(settings.SWEEP_SEARCH_TERMS[:10]).lower()
    assert "kitchen" in first
    assert any("wardrobe" in t.lower() for t in settings.SWEEP_SEARCH_TERMS[:20])


def test_trailer_domain_still_selectable():
    assert domain_config("trailer")["search_terms"] == settings.TRAILER_COMPONENT_SEARCH_TERMS
    assert categories_for("trailer") == TRAILER_BOM_CATEGORIES
    assert any("bulb" in t.lower() for t in get_search_terms(level="component", domain="trailer"))


def test_unknown_domain_rejected():
    with pytest.raises((ValueError, KeyError)):
        domain_config("aerospace")


def test_priority_categories_are_tier_a():
    tier_a = {c.key for c in FURNISHING_BOM_CATEGORIES if c.tier == "A"}
    assert {"kitchen_cabinets", "wardrobes", "worktops", "hinges_runners"} <= tier_a
    assert categories_for() == FURNISHING_BOM_CATEGORIES


def _cat(key):
    return next(c for c in FURNISHING_BOM_CATEGORIES if c.key == key)


def test_kitchen_cabinet_category_excludes_electrical_cabinets():
    cat = _cat("kitchen_cabinets")
    assert _matches(_haystack({"product_keywords": ["kitchen cabinets", "flat pack kitchen"]}), cat)
    assert not _matches(_haystack({"product_keywords": ["electrical distribution cabinet"]}), cat)


def test_furnishing_supplier_gets_product_fit():
    scorer = SupplierScorer()
    furnishing = scorer.score({"product_keywords": ["kitchen cabinets", "sliding wardrobe", "soft close hinges"]})
    trailer = scorer.score({"product_keywords": ["trailer axle", "coupling"]})
    assert furnishing["product_fit_score"] > trailer["product_fit_score"]


@pytest.mark.parametrize("text,expected", [
    ("FSC chain of custody", "fsc certified"),
    ("BS 5852", "uk furniture fire safety"),
    ("CNC nesting", "cnc panel processing"),
    ("PUR edge banding", "edge banding"),
    ("CARB P2", "low formaldehyde emission"),
    ("WRAS approved", "wras approval"),
])
def test_furnishing_vocabulary(text, expected):
    term = map_to_canonical(text)
    assert term is not None and term.canonical == expected
