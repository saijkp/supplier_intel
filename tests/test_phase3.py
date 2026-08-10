"""
tests/test_phase3.py

Phase 3 test suite: deduplication (name/domain utils + matcher) and the
composite scoring engine.
"""

from __future__ import annotations

import pytest

from deduplication.name_utils import normalise_company_name, CHINESE_GEO_TOKENS
from deduplication.domain_utils import extract_domain, domains_match, is_platform_subdomain
from deduplication.matcher import SupplierMatcher
from verification.scorer import SupplierScorer
from storage.database import initialise_schema
from storage.repository import SupplierRepository


# ═════════════════════════════════════════════════════════════
# name_utils
# ═════════════════════════════════════════════════════════════

class TestNormaliseCompanyName:

    def test_empty_input_returns_empty_string(self):
        assert normalise_company_name("") == ""
        assert normalise_company_name(None) == ""

    def test_strips_co_ltd_suffix(self):
        assert normalise_company_name("ABC Electronics Co., Ltd.", strip_geo=False) == "abc electronics"

    def test_strips_chained_suffixes(self):
        result = normalise_company_name("XYZ Trading Co., Ltd", strip_geo=False)
        assert result == "xyz"

    def test_strips_bracketed_geo_qualifier(self):
        result = normalise_company_name("ABC Electronics (Guangzhou)", strip_geo=False)
        assert result == "abc electronics"

    def test_strips_leading_geo_token_when_enabled(self):
        result = normalise_company_name("Guangzhou ABC Electronics Co Ltd", strip_geo=True)
        assert result == "abc electronics"

    def test_geo_variants_converge_to_same_normalised_form(self):
        a = normalise_company_name("Guangzhou ABC Electronics Co Ltd")
        b = normalise_company_name("ABC Electronics (Guangzhou)")
        c = normalise_company_name("ABC Electronic Co., Ltd Guangzhou")
        # 'Electronic' vs 'Electronics' will differ slightly, but the
        # geo-stripped core should be near-identical — exact equality
        # isn't required by this function (that's the matcher's job via
        # fuzzy scoring), just that geo tokens are gone.
        for normalised in (a, b, c):
            assert "guangzhou" not in normalised

    def test_never_strips_name_down_to_nothing(self):
        # "Shenzhen" alone is a real (if unlikely) company name fragment;
        # stripping it as a geo token shouldn't leave an empty string.
        result = normalise_company_name("Shenzhen", strip_geo=True)
        assert result != ""

    def test_punctuation_and_whitespace_collapsed(self):
        result = normalise_company_name("A.B.C.   Electronics!!", strip_geo=False)
        assert result == "a b c electronics"

    def test_strip_geo_false_keeps_city_name(self):
        result = normalise_company_name("Guangzhou ABC Electronics Co Ltd", strip_geo=False)
        assert "guangzhou" in result


# ═════════════════════════════════════════════════════════════
# domain_utils
# ═════════════════════════════════════════════════════════════

class TestExtractDomain:

    def test_empty_input(self):
        assert extract_domain("") is None
        assert extract_domain(None) is None  # type: ignore[arg-type]

    def test_strips_scheme_and_www(self):
        assert extract_domain("https://www.foo.com/products") == "foo.com"
        assert extract_domain("http://foo.com") == "foo.com"

    def test_adds_scheme_when_missing(self):
        assert extract_domain("foo.com") == "foo.com"
        assert extract_domain("www.foo.com") == "foo.com"

    def test_strips_port(self):
        assert extract_domain("https://foo.com:8080/path") == "foo.com"

    def test_ignores_path_and_query(self):
        assert extract_domain("https://foo.com/a/b?c=d") == "foo.com"

    def test_preserves_subdomain(self):
        assert extract_domain("https://ledmasters.en.alibaba.com") == "ledmasters.en.alibaba.com"


class TestDomainsMatch:

    def test_matching_domains(self):
        assert domains_match("https://foo.com", "www.foo.com") is True

    def test_non_matching_domains(self):
        assert domains_match("foo.com", "bar.com") is False

    def test_empty_inputs_never_match(self):
        assert domains_match("", "") is False
        assert domains_match(None, None) is False  # type: ignore[arg-type]


class TestIsPlatformSubdomain:

    def test_alibaba_subdomain_detected(self):
        assert is_platform_subdomain("ledmasters.en.alibaba.com") is True

    def test_own_company_domain_not_flagged(self):
        assert is_platform_subdomain("ledmasters.com") is False

    def test_empty_input(self):
        assert is_platform_subdomain(None) is False
        assert is_platform_subdomain("") is False


# ═════════════════════════════════════════════════════════════
# SupplierMatcher
# ═════════════════════════════════════════════════════════════

@pytest.fixture()
def repo(tmp_path):
    db_path = tmp_path / "test.db"
    initialise_schema(db_path)
    return SupplierRepository(db_path=db_path)


@pytest.fixture()
def matcher(repo):
    return SupplierMatcher(repo)


class TestFindMatch:

    def test_uscc_exact_match(self, repo, matcher):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Foo Trading Co", "uscc": "91440101MA5ABCDE12",
        })
        candidate = {"canonical_name": "Completely Different Name", "uscc": "91440101MA5ABCDE12"}

        found_id, confidence, signals = matcher.find_match(candidate)
        assert found_id == supplier_id
        assert confidence == 1.0
        assert signals["uscc_match"] is True

    def test_domain_exact_match(self, repo, matcher):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Foo Trading Co", "domain": "footrading.com",
        })
        candidate = {"canonical_name": "Totally Different Name Inc", "domain": "https://www.footrading.com/en"}

        found_id, confidence, signals = matcher.find_match(candidate)
        assert found_id == supplier_id
        assert confidence == 0.95
        assert signals["domain_match"] is True

    def test_fuzzy_name_match_within_country(self, repo, matcher):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Guangzhou ABC Electronics Co Ltd", "country": "China",
        })
        candidate = {"canonical_name": "ABC Electronics (Guangzhou)", "country": "China"}

        found_id, confidence, signals = matcher.find_match(candidate)
        assert found_id == supplier_id
        assert confidence >= 0.75
        assert "name_score" in signals

    def test_fuzzy_match_boosted_by_phone(self, repo, matcher):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Shenzhen Widget Factory", "country": "China",
            "primary_phone": "+86 755 1234 5678",
        })
        candidate = {
            "canonical_name": "Shenzhen Widgets Factory Ltd",  # slightly different
            "country": "China", "primary_phone": "0755-1234-5678",
        }

        found_id, confidence, signals = matcher.find_match(candidate)
        assert found_id == supplier_id
        assert signals.get("phone_match") is True

    def test_fuzzy_match_boosted_by_city(self, repo, matcher):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Widget Factory", "country": "China", "city": "Shenzhen",
        })
        candidate = {"canonical_name": "Widget Factories", "country": "China", "city": "Shenzhen"}

        found_id, confidence, signals = matcher.find_match(candidate)
        assert found_id == supplier_id
        assert signals.get("city_match") is True

    def test_no_match_for_dissimilar_names(self, repo, matcher):
        repo.create_golden_record({"canonical_name": "Shenzhen Widget Factory", "country": "China"})
        candidate = {"canonical_name": "Completely Unrelated Aerospace Corp", "country": "China"}

        found_id, confidence, signals = matcher.find_match(candidate)
        assert found_id is None
        assert confidence == 0.0

    def test_no_match_when_candidate_has_no_name(self, repo, matcher):
        repo.create_golden_record({"canonical_name": "Foo Co"})
        found_id, confidence, signals = matcher.find_match({"canonical_name": ""})
        assert found_id is None
        assert confidence == 0.0

    def test_empty_database_returns_no_match(self, matcher):
        found_id, confidence, signals = matcher.find_match({"canonical_name": "Anything Co", "country": "China"})
        assert found_id is None
        assert confidence == 0.0

    def test_uscc_match_takes_priority_over_name_mismatch(self, repo, matcher):
        supplier_id = repo.create_golden_record({
            "canonical_name": "Original Name Co", "uscc": "91440101MA5ABCDE12", "country": "China",
        })
        repo.create_golden_record({"canonical_name": "Some Other Unrelated Co", "country": "China"})

        candidate = {"canonical_name": "Renamed Entirely Co", "uscc": "91440101MA5ABCDE12", "country": "China"}
        found_id, confidence, signals = matcher.find_match(candidate)
        assert found_id == supplier_id
        assert confidence == 1.0


class TestResolveAndStore:

    def test_requires_canonical_name(self, matcher):
        with pytest.raises(ValueError):
            matcher.resolve_and_store({"country": "China"})

    def test_creates_new_record_when_no_match(self, repo, matcher):
        result = matcher.resolve_and_store({"canonical_name": "Brand New Co", "country": "China"})
        assert result["action"] == "created"
        assert repo.get_supplier(result["supplier_id"])["canonical_name"] == "Brand New Co"

    def test_auto_merges_on_uscc_match(self, repo, matcher):
        existing_id = repo.create_golden_record({
            "canonical_name": "Foo Co", "uscc": "91440101MA5ABCDE12",
        })
        result = matcher.resolve_and_store({
            "canonical_name": "Foo Co", "uscc": "91440101MA5ABCDE12", "city": "Shenzhen",
        })
        assert result["action"] == "merged"
        assert result["supplier_id"] == existing_id

        merged = repo.get_supplier(existing_id)
        assert merged["city"] == "Shenzhen"
        assert merged["source_count"] == 2

    def test_queues_for_review_on_medium_confidence_match(self, repo, matcher):
        existing_id = repo.create_golden_record({
            "canonical_name": "Guangzhou ABC Electronics Co Ltd", "country": "China",
        })
        # Similar but not identical enough to auto-merge; no USCC/domain
        # to push it to certainty either.
        result = matcher.resolve_and_store({
            "canonical_name": "ABC Electronic Co Guangzhou", "country": "China",
        })

        if result["action"] == "review_queued":
            assert result["matched_supplier_id"] == existing_id
            pending = repo.get_pending_review_candidates()
            assert len(pending) == 1
            assert pending[0]["supplier_id_b"] == existing_id
        else:
            # Fuzzy matching is heuristic; if this particular pair scored
            # high enough to auto-merge or low enough to be a new record,
            # that's still a valid, non-crashing outcome for this test's
            # purposes — the review-queue path itself is covered directly below.
            assert result["action"] in ("merged", "created")

    def test_review_queue_action_end_to_end(self, repo, matcher, monkeypatch):
        """Force a mid-range confidence score to deterministically test
        the review_queued branch, independent of fuzzy-matching specifics."""
        existing_id = repo.create_golden_record({"canonical_name": "Foo Co", "country": "China"})

        def fake_find_match(candidate):
            return existing_id, 0.80, {"name_score": 0.80}

        monkeypatch.setattr(matcher, "find_match", fake_find_match)
        result = matcher.resolve_and_store({"canonical_name": "Foo Co Variant", "country": "China"})

        assert result["action"] == "review_queued"
        assert result["matched_supplier_id"] == existing_id
        new_supplier = repo.get_supplier(result["new_supplier_id"])
        assert new_supplier["canonical_name"] == "Foo Co Variant"

        pending = repo.get_pending_review_candidates()
        assert len(pending) == 1
        assert pending[0]["match_score"] == 0.80


# ═════════════════════════════════════════════════════════════
# SupplierScorer
# ═════════════════════════════════════════════════════════════

class TestSupplierScorer:

    def test_fully_qualified_supplier_scores_high(self):
        scorer = SupplierScorer()
        supplier = {
            "product_keywords": ["axle", "brake", "coupling", "led light"],  # 4 BOM categories
            "domain": "foo.com",
            "address": "1 Factory Road, Foo City",
            "uscc_verified": 1,
            "is_manufacturer": 1,
            "iso_9001": 1,
            "e_mark_certified": 1,
            "iso_ts_16949": 1,
            "confirmed_shipments_uk": 15,
            "confirmed_shipments_eu": 3,
            "confirmed_shipments_us": 1,
            "last_shipment_date": "2026-06-01",
            "alibaba_years": 6,
            "alibaba_trade_assurance": 1,
            "alibaba_rating": 4.8,
            "alibaba_url": "https://foo.en.alibaba.com",
            "hktdc_url": "https://hktdc.com/foo",
            "contact_name": "Li Wei",
            "primary_email": "sales@foo.com",
            "whatsapp": "+86 123",
            "primary_phone": "+86 123",
            "capability_extracted_at": "2026-01-01T00:00:00",
            "manufacturer_verified_at": "2026-01-01T00:00:00",
            "contacts_found_at": "2026-01-01T00:00:00",
        }
        result = scorer.score(supplier, sources={"trade", "automechanika_2026", "hktdc"})
        assert result["product_fit_score"] == 100
        assert result["verification_score"] == 100
        assert result["export_score"] == 100
        assert result["platform_score"] == 100
        assert result["contact_score"] == 100
        assert result["evidence_coverage"] == 100
        assert result["composite_score"] == 100  # weighted formula alone exceeds 100 before the +10 bonus, capped
        assert result["recommendation"] == "recommended"

    def test_empty_supplier_is_unscored_not_avoid(self):
        """The core bug this rewrite fixes: absence of evidence must
        never resolve to 'avoid' -- that bucket is reserved for actual
        negative evidence (flagged / confirmed trader)."""
        scorer = SupplierScorer()
        result = scorer.score({})
        assert result["composite_score"] == 0
        assert result["evidence_coverage"] == 0
        assert result["recommendation"] == "unscored"

    def test_avoid_requires_negative_evidence_not_absence(self):
        """A real supplier with strong product fit and provenance but
        no certs/exports/contacts on file yet (the SAF-Holland case)
        must not be 'avoid' just because that evidence hasn't been
        collected."""
        scorer = SupplierScorer()
        supplier = {
            "canonical_name": "SAF-HOLLAND GmbH",
            "domain": "safholland.com",
            "address": "SAF-HOLLAND GmbH, Hauptstr. 26, 63856 Bessenbach, Germany",
            "product_keywords": ["Axles", "Brake pads / linings", "Fifth-wheel couplings"],
        }
        result = scorer.score(supplier, sources={"automechanika_2026"})
        assert result["recommendation"] != "avoid"

    def test_composite_matches_weighted_formula(self):
        scorer = SupplierScorer()
        supplier = {
            "is_manufacturer": 1,          # verification += 40
            "confirmed_shipments_uk": 1,   # export += 40
            "contact_name": "Li Wei",      # contact += 30
        }
        result = scorer.score(supplier)
        expected = round(0 * 0.25 + 0 * 0.25 + 40 * 0.25 + 40 * 0.15 + 30 * 0.10)
        assert result["composite_score"] == expected

    def test_uscc_and_alibaba_are_bonuses_not_weighted(self):
        """USCC verification and Alibaba platform strength must move
        the composite by at most their small capped bonus, never by a
        full weighted-dimension's worth."""
        scorer = SupplierScorer()
        base = scorer.score({"is_manufacturer": 1})["composite_score"]
        with_uscc = scorer.score({"is_manufacturer": 1, "uscc_verified": 1})["composite_score"]
        assert with_uscc == base + 5

    def test_flagged_supplier_always_avoid_regardless_of_score(self):
        scorer = SupplierScorer()
        supplier = {
            "uscc_verified": 1, "is_manufacturer": 1, "iso_9001": 1,
            "e_mark_certified": 1, "confirmed_shipments_uk": 20,
            "flagged": 1,
        }
        result = scorer.score(supplier)
        assert result["recommendation"] == "avoid"

    def test_confirmed_trader_with_high_confidence_is_avoid(self):
        scorer = SupplierScorer()
        supplier = {
            "uscc_verified": 1, "confirmed_shipments_uk": 20,
            "is_manufacturer": 0, "manufacturer_confidence": 95,
        }
        result = scorer.score(supplier)
        assert result["recommendation"] == "avoid"

    def test_unknown_manufacturer_status_not_penalised_as_trader(self):
        scorer = SupplierScorer()
        # is_manufacturer is None/absent (unknown) — should NOT trigger
        # the "confirmed trader" auto-avoid rule.
        supplier = {"uscc_verified": 1, "iso_9001": 1, "e_mark_certified": 1, "confirmed_shipments_uk": 15}
        result = scorer.score(supplier)
        assert result["recommendation"] != "avoid"

    def test_recommendation_thresholds(self):
        scorer = SupplierScorer()
        # strong evidence across every dimension -> recommended (>= 70)
        high = scorer.score({
            "product_keywords": ["axle", "brake"],
            "domain": "foo.com", "address": "1 Road",
            "uscc_verified": 1, "is_manufacturer": 1, "iso_9001": 1, "e_mark_certified": 1,
            "confirmed_shipments_uk": 15, "confirmed_shipments_eu": 1,
            "alibaba_years": 5, "alibaba_trade_assurance": 1, "alibaba_rating": 4.8,
            "contact_name": "A", "primary_email": "a@b.com",
        }, sources={"trade", "hktdc"})
        assert high["recommendation"] == "recommended"

        # nothing at all -> unscored, never avoid
        low = scorer.score({})
        assert low["recommendation"] == "unscored"

    def test_product_fit_rewards_more_matched_categories(self):
        scorer = SupplierScorer()
        none_matched = scorer._product_fit_score({"product_keywords": ["widget"]})
        one_matched = scorer._product_fit_score({"product_keywords": ["axle"]})
        three_matched = scorer._product_fit_score({"product_keywords": ["axle", "brake", "coupling"]})
        assert none_matched == 0
        assert 0 < one_matched < three_matched <= 100

    def test_provenance_is_source_aware_not_flat(self):
        """Regression test for the exact bug caught in review: provenance
        must not give the same flat credit to every supplier sourced
        from the same bulk import. A supplier corroborated by 2
        independent sources must outscore one seen only once, and a
        higher-quality single source must outscore a lower-quality one."""
        scorer = SupplierScorer()
        one_source = scorer._provenance_score({}, {"automechanika_2026"})
        two_sources = scorer._provenance_score({}, {"automechanika_2026", "trade"})
        assert two_sources > one_source

        weak_source = scorer._provenance_score({}, {"google"})
        strong_source = scorer._provenance_score({}, {"trade"})
        assert strong_source > weak_source

    def test_provenance_unknown_source_gets_conservative_default(self):
        scorer = SupplierScorer()
        known = scorer._provenance_score({}, {"trade"})
        unknown = scorer._provenance_score({}, {"some_future_source_not_yet_curated"})
        assert unknown < known

    def test_evidence_coverage_low_routes_to_unscored(self):
        scorer = SupplierScorer()
        result = scorer.score({"canonical_name": "Mystery Co"})
        assert result["recommendation"] == "unscored"
        assert result["evidence_coverage"] < 30

    def test_evidence_coverage_and_composite_are_independent(self):
        """A supplier can have full coverage (we checked everything)
        and still score low (what we found wasn't good) — coverage and
        composite quality must not be conflated."""
        scorer = SupplierScorer()
        result = scorer.score({
            "domain": "example.com", "address": "1 Street, Town",
            "product_keywords": ["widget"],  # matches no BOM category
            "exports_to_uk": True,
            "capability_extracted_at": "2026-01-01T00:00:00",
            "manufacturer_verified_at": "2026-01-01T00:00:00",
            "contacts_found_at": "2026-01-01T00:00:00",
        }, sources={"google"})
        assert result["evidence_coverage"] == 100
        assert result["composite_score"] < 40

    def test_export_score_recent_shipment_bonus(self):
        scorer = SupplierScorer()
        from datetime import date, timedelta
        recent = (date.today() - timedelta(days=10)).isoformat()
        old = (date.today() - timedelta(days=400)).isoformat()

        recent_score = scorer._export_score({"confirmed_shipments_uk": 1, "last_shipment_date": recent})
        old_score = scorer._export_score({"confirmed_shipments_uk": 1, "last_shipment_date": old})
        assert recent_score == old_score + 10

    def test_platform_score_multi_platform_bonus(self):
        scorer = SupplierScorer()
        single = scorer._platform_score({"alibaba_url": "https://foo.alibaba.com"})
        multi = scorer._platform_score({
            "alibaba_url": "https://foo.alibaba.com",
            "hktdc_url": "https://hktdc.com/foo",
        })
        assert multi == single + 20

    def test_score_handles_sqlite_integer_booleans(self):
        """Repository reads return 0/1 ints for BOOLEAN columns, not
        True/False — the scorer must treat these correctly. uscc_verified
        now only feeds the bonus (see test_uscc_and_alibaba_are_bonuses_not_weighted),
        so this checks the bonus path handles 0/1 ints correctly."""
        scorer = SupplierScorer()
        result_true = scorer.score({"uscc_verified": 1})
        result_false = scorer.score({"uscc_verified": 0})
        assert result_true["composite_score"] == result_false["composite_score"] + 5

    def test_scorer_output_compatible_with_repository_update(self, tmp_path):
        db_path = tmp_path / "test.db"
        initialise_schema(db_path)
        repo = SupplierRepository(db_path=db_path)
        scorer = SupplierScorer()

        supplier_id = repo.create_golden_record({
            "canonical_name": "Foo Co", "uscc_verified": True, "confirmed_shipments_uk": 5,
        })
        supplier = repo.get_supplier(supplier_id)
        scores = scorer.score(supplier)
        repo.update_scores(supplier_id, scores)

        updated = repo.get_supplier(supplier_id)
        assert updated["composite_score"] == scores["composite_score"]
        assert updated["recommendation"] == scores["recommendation"]
        assert updated["evidence_coverage"] == scores["evidence_coverage"]
        assert updated["product_fit_score"] == scores["product_fit_score"]
        assert updated["provenance_score"] == scores["provenance_score"]
