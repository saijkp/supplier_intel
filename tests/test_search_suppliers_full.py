"""
tests/test_search_suppliers_full.py

Tests for storage.repository.SupplierRepository.search_suppliers_full
-- the single query combining product text, multiple required
capabilities (AND semantics), and manufacturer verification.
"""

from __future__ import annotations

import pytest

from storage.database import initialise_schema


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    initialise_schema(path)
    return path


@pytest.fixture()
def repo(db_path):
    from storage.repository import SupplierRepository
    return SupplierRepository(db_path=db_path)


def _make_supplier(repo, **overrides):
    name = overrides.get("canonical_name", "Acme Trailer Parts")
    data = {
        "canonical_name": name, "country": "China",
        "domain": overrides.pop("domain", None) or f"{name.lower().replace(' ', '-')}.example.com",
        "product_keywords": ["wheel bearings", "hub assemblies"],
        "composite_score": 70,
    }
    data.update(overrides)
    supplier_id = repo.create_golden_record(data)
    if "is_manufacturer" in overrides:
        repo.update_supplier_fields(supplier_id, {"is_manufacturer": overrides["is_manufacturer"]})
    if "composite_score" in overrides:
        # composite_score lives in SCORE_FIELDS, a separate whitelist
        # from SUPPLIER_WRITABLE_FIELDS -- update_supplier_fields
        # silently drops it (the same class of bug caught earlier for
        # capability_extracted_at); update_scores is the real setter.
        repo.update_scores(supplier_id, {"composite_score": overrides["composite_score"]})
    return supplier_id


def _add_capability(repo, supplier_id, canonical_term, category="process", relationship="in_house", confidence=0.9):
    repo.add_capability_finding(supplier_id, {
        "reported_term": canonical_term, "canonical_term": canonical_term, "category": category,
        "relationship": relationship, "confidence": confidence,
        "evidence": f"we operate {canonical_term}", "source_url": "https://example.com",
    })


class TestProductTextOnly:

    def test_matches_on_product_keywords(self, repo):
        supplier_id = _make_supplier(repo)
        results = repo.search_suppliers_full(product_query="wheel bearings")
        assert any(r["id"] == supplier_id for r in results)

    def test_no_match_returns_empty(self, repo):
        _make_supplier(repo)
        assert repo.search_suppliers_full(product_query="injection moulded toolboxes") == []

    def test_matched_capabilities_is_empty_list_when_no_capabilities_required(self, repo):
        _make_supplier(repo)
        results = repo.search_suppliers_full(product_query="wheel bearings")
        assert results[0]["matched_capabilities"] == []


class TestRequiredCapabilitiesAreAndNotOr:

    def test_supplier_with_only_one_of_two_required_is_excluded(self, repo):
        supplier_id = _make_supplier(repo, canonical_name="Partial Co")
        _add_capability(repo, supplier_id, "rotational moulding")
        # missing "sub-assembly"

        results = repo.search_suppliers_full(
            required_capabilities=["rotational moulding", "sub-assembly"]
        )
        assert results == []

    def test_supplier_with_both_required_capabilities_matches(self, repo):
        supplier_id = _make_supplier(repo, canonical_name="Full Co")
        _add_capability(repo, supplier_id, "rotational moulding")
        _add_capability(repo, supplier_id, "sub-assembly", category="capability")

        results = repo.search_suppliers_full(
            required_capabilities=["rotational moulding", "sub-assembly"]
        )
        assert len(results) == 1
        assert results[0]["id"] == supplier_id

    def test_matched_capabilities_are_attached_with_evidence(self, repo):
        supplier_id = _make_supplier(repo, canonical_name="Full Co")
        _add_capability(repo, supplier_id, "rotational moulding")

        results = repo.search_suppliers_full(required_capabilities=["rotational moulding"])
        matched = results[0]["matched_capabilities"]
        assert len(matched) == 1
        assert matched[0]["canonical_term"] == "rotational moulding"
        assert matched[0]["evidence"] == "we operate rotational moulding"

    def test_unrecognised_capability_term_raises_not_silently_empties(self, repo):
        _make_supplier(repo)
        with pytest.raises(ValueError, match="not a recognised capability"):
            repo.search_suppliers_full(required_capabilities=["hydroforming"])

    def test_min_confidence_excludes_weak_evidence(self, repo):
        supplier_id = _make_supplier(repo, canonical_name="Weak Co")
        _add_capability(repo, supplier_id, "rotational moulding", confidence=0.3)

        assert repo.search_suppliers_full(
            required_capabilities=["rotational moulding"], min_capability_confidence=0.5
        ) == []
        assert len(repo.search_suppliers_full(
            required_capabilities=["rotational moulding"], min_capability_confidence=0.2
        )) == 1

    def test_subcontracted_still_counts_toward_the_and_requirement(self, repo):
        """A rotomoulder who subcontracts assembly is still a
        legitimate match -- see find_suppliers_by_capability's own
        precedent for this."""
        supplier_id = _make_supplier(repo, canonical_name="Roto Co")
        _add_capability(repo, supplier_id, "rotational moulding", relationship="in_house")
        _add_capability(repo, supplier_id, "sub-assembly", relationship="subcontracted")

        results = repo.search_suppliers_full(
            required_capabilities=["rotational moulding", "sub-assembly"]
        )
        assert len(results) == 1


class TestManufacturersOnly:

    def test_excludes_non_manufacturers_when_flagged(self, repo):
        _make_supplier(repo, canonical_name="Trader Co", is_manufacturer=False)
        results = repo.search_suppliers_full(manufacturers_only=True)
        assert results == []

    def test_includes_manufacturers_when_flagged(self, repo):
        supplier_id = _make_supplier(repo, canonical_name="Real Factory", is_manufacturer=True)
        results = repo.search_suppliers_full(manufacturers_only=True)
        assert any(r["id"] == supplier_id for r in results)

    def test_flag_off_includes_everyone(self, repo):
        _make_supplier(repo, canonical_name="Trader Co", is_manufacturer=False)
        results = repo.search_suppliers_full(manufacturers_only=False)
        assert len(results) == 1


class TestCombinedFilters:

    def test_product_plus_capability_plus_manufacturer_all_apply_together(self, repo):
        good = _make_supplier(
            repo, canonical_name="Good Match",
            product_keywords=["wheel bearings"], is_manufacturer=True,
        )
        _add_capability(repo, good, "iso 9001", category="standard")

        wrong_product = _make_supplier(
            repo, canonical_name="Wrong Product",
            product_keywords=["led lighting"], is_manufacturer=True,
        )
        _add_capability(repo, wrong_product, "iso 9001", category="standard")

        not_manufacturer = _make_supplier(
            repo, canonical_name="Not Manufacturer",
            product_keywords=["wheel bearings"], is_manufacturer=False,
        )
        _add_capability(repo, not_manufacturer, "iso 9001", category="standard")

        results = repo.search_suppliers_full(
            product_query="wheel bearings", required_capabilities=["iso 9001"],
            manufacturers_only=True,
        )
        assert [r["id"] for r in results] == [good]

    def test_min_score_applies_alongside_other_filters(self, repo):
        low = _make_supplier(repo, canonical_name="Low Score", composite_score=20)
        high = _make_supplier(repo, canonical_name="High Score", composite_score=90)
        results = repo.search_suppliers_full(product_query="wheel bearings", min_score=50)
        ids = {r["id"] for r in results}
        assert high in ids
        assert low not in ids

    def test_no_filters_at_all_returns_everything_sorted_by_score(self, repo):
        low = _make_supplier(repo, canonical_name="Low", composite_score=10)
        high = _make_supplier(repo, canonical_name="High", composite_score=90)
        results = repo.search_suppliers_full()
        assert [r["id"] for r in results] == [high, low]

    def test_limit_is_respected(self, repo):
        for i in range(5):
            _make_supplier(repo, canonical_name=f"Co {i}")
        results = repo.search_suppliers_full(limit=2)
        assert len(results) == 2


class TestCountryFilter:

    def test_exact_country_match(self, repo):
        uk = _make_supplier(repo, canonical_name="UK Co", country="United Kingdom")
        _make_supplier(repo, canonical_name="China Co", country="China")
        results = repo.search_suppliers_full(country="United Kingdom")
        assert [r["id"] for r in results] == [uk]

    def test_match_is_case_insensitive(self, repo):
        uk = _make_supplier(repo, canonical_name="UK Co", country="United Kingdom")
        results = repo.search_suppliers_full(country="united kingdom")
        assert [r["id"] for r in results] == [uk]

    def test_deliberately_not_fuzzy_uk_does_not_match_ukraine(self, repo):
        """The whole point of exact matching here: a loose match risks
        'UK' silently matching 'Ukraine', which would be a genuinely
        dangerous false positive in a procurement filter."""
        _make_supplier(repo, canonical_name="Ukraine Co", country="Ukraine")
        results = repo.search_suppliers_full(country="UK")
        assert results == []

    def test_combines_with_product_and_capability_filters(self, repo):
        good = _make_supplier(
            repo, canonical_name="Good UK Co", country="United Kingdom",
            product_keywords=["wheel hub"],
        )
        _add_capability(repo, good, "iso 9001", category="standard")

        wrong_country = _make_supplier(
            repo, canonical_name="China Co", country="China", product_keywords=["wheel hub"],
        )
        _add_capability(repo, wrong_country, "iso 9001", category="standard")

        results = repo.search_suppliers_full(
            product_query="wheel hub", required_capabilities=["iso 9001"], country="United Kingdom",
        )
        assert [r["id"] for r in results] == [good]

    def test_no_country_filter_includes_everyone(self, repo):
        _make_supplier(repo, canonical_name="UK Co", country="United Kingdom")
        _make_supplier(repo, canonical_name="China Co", country="China")
        assert len(repo.search_suppliers_full()) == 2


class TestExcludesFlaggedSuppliers:
    """A human-flagged supplier (flagged=1, e.g. ruled out as a
    broker/network rather than a single factory) must never resurface
    in a procurement search result, regardless of which other filters
    are used -- see storage/repository.py's SUPPLIER_WRITABLE_FIELDS
    flagged/flag_reason and verification/scorer.py's _recommend(),
    which already treats flagged=1 as an automatic 'avoid'."""

    def test_flagged_supplier_is_excluded_from_plain_product_search(self, repo):
        good = _make_supplier(repo, canonical_name="Good Co")
        flagged = _make_supplier(repo, canonical_name="Flagged Co")
        repo.update_supplier_fields(flagged, {"flagged": True, "flag_reason": "broker, not a single factory"})

        results = repo.search_suppliers_full(product_query="wheel bearings")

        assert [r["id"] for r in results] == [good]

    def test_flagged_supplier_is_excluded_even_with_no_other_filters(self, repo):
        good = _make_supplier(repo, canonical_name="Good Co")
        flagged = _make_supplier(repo, canonical_name="Flagged Co")
        repo.update_supplier_fields(flagged, {"flagged": True, "flag_reason": "equipment maker, not a parts manufacturer"})

        results = repo.search_suppliers_full()

        assert [r["id"] for r in results] == [good]

    def test_unflagged_suppliers_are_unaffected(self, repo):
        a = _make_supplier(repo, canonical_name="Co A")
        b = _make_supplier(repo, canonical_name="Co B")
        results = repo.search_suppliers_full()
        assert {r["id"] for r in results} == {a, b}
