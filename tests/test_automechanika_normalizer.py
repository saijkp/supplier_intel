"""
tests/test_automechanika_normalizer.py

Tests for normalizers.automechanika_normalizer.AutomechanikaNormaliser.
The sample rows below are copied verbatim from real rows in the actual
uploaded workbook (Core Suppliers and Extended - Review sheets), not
synthetic data -- this is deliberately testing against the real
column format, not an assumed one.
"""

from __future__ import annotations

from normalizers.automechanika_normalizer import AutomechanikaNormaliser


def _row(**overrides):
    base = {
        "name": "Forged Chassis Limited",
        "tier": "Core",
        "final_score": 17,
        "hall_stand": "5.1, C20",
        "website": "http://www.forgedchassis.com",
        "address": "Forged Chassis Limited, Drumgoose, A75 V002 Castleblayney, Ireland",
        "matched_product_groups": (
            "Axle suspensions; Spring seats / plates; Suspension components (chassis); "
            "Cast parts (chassis); Rear-axle suspensions; Rear axles"
        ),
        "matched_text_keywords": "chassis",
        "description": "Forged Chassis Limited is a specialist manufacturer...",
        "url": "https://automechanika.messefrankfurt.com/frankfurt/en/exhibitor-search.detail.html/forged-chassis-limited.html",
    }
    base.update(overrides)
    return base


class TestRealRowFromCoreSuppliers:
    """This exact row is copied verbatim from the real uploaded file."""

    def test_name_maps_to_canonical_name(self):
        result = AutomechanikaNormaliser().normalise(_row())
        assert result["canonical_name"] == "Forged Chassis Limited"

    def test_website_becomes_a_clean_domain(self):
        result = AutomechanikaNormaliser().normalise(_row())
        assert result["domain"] == "forgedchassis.com"

    def test_country_is_parsed_from_the_address(self):
        result = AutomechanikaNormaliser().normalise(_row())
        assert result["country"] == "Ireland"

    def test_full_address_is_preserved(self):
        result = AutomechanikaNormaliser().normalise(_row())
        assert "Castleblayney" in result["address"]

    def test_product_groups_are_split_into_a_keyword_list(self):
        result = AutomechanikaNormaliser().normalise(_row())
        assert "Axle suspensions" in result["product_keywords"]
        assert "Rear axles" in result["product_keywords"]
        assert len(result["product_keywords"]) == 6

    def test_description_becomes_notes(self):
        result = AutomechanikaNormaliser().normalise(_row())
        assert "specialist manufacturer" in result["notes"]


class TestRealRowsAcrossDifferentCountries:
    """Each of these addresses is copied from a real row, chosen to
    prove the 'country is the last comma segment' rule actually holds
    across the file's real variety -- including a postal code
    embedded in the same segment as the country (the India case)."""

    def test_italy(self):
        row = _row(address="Errevi S.p.A., Via Eugenio Curiel 11, 10024 Moncalieri Torino, Italy")
        assert AutomechanikaNormaliser()._parse_country(row["address"]) == "Italy"

    def test_belgium(self):
        row = _row(address="VDL Weweler-Colaert N.V., Beneluxlaan 1-3, 8970 Poperinge, Belgium")
        assert AutomechanikaNormaliser()._parse_country(row["address"]) == "Belgium"

    def test_india_with_postal_code_and_state_in_parentheses(self):
        row = _row(address="Hindostan Expo, C - 246, Phase VIII, Focal Point, Ludhiana (Punjab) 141010, India")
        assert AutomechanikaNormaliser()._parse_country(row["address"]) == "India"

    def test_germany(self):
        row = _row(address="SAF-HOLLAND GmbH, Hauptstr. 26, 63856 Bessenbach, Germany")
        assert AutomechanikaNormaliser()._parse_country(row["address"]) == "Germany"

    def test_taiwan(self):
        row = _row(
            address="Hwang Yu Automobile Parts Co. Ltd, 1F, No. 17, Aly. Ln. 1, Sec. 4, "
                    "Chongqing N. Rd., Shilin Dist., Taipei 111061, Taiwan"
        )
        assert AutomechanikaNormaliser()._parse_country(row["address"]) == "Taiwan"

    def test_bosnia_and_herzegovina_two_word_country_name(self):
        row = _row(address="'Pobjeda' d.d. Tesanj, Poslovna zona Bukva 3, 74260 Bukva, Bosnia and Herzegovina")
        assert AutomechanikaNormaliser()._parse_country(row["address"]) == "Bosnia and Herzegovina"


class TestMissingData:
    """47 of the 322 real Core Suppliers rows have no website -- this
    must still produce a valid, importable candidate."""

    def test_missing_website_is_omitted_not_a_broken_domain(self):
        result = AutomechanikaNormaliser().normalise(_row(website=None))
        assert "domain" not in result
        assert result["canonical_name"] == "Forged Chassis Limited"

    def test_missing_website_still_produces_an_importable_candidate(self):
        result = AutomechanikaNormaliser().normalise(_row(website=None))
        assert result.get("canonical_name")

    def test_missing_product_groups_is_omitted_not_an_empty_list_key_error(self):
        result = AutomechanikaNormaliser().normalise(_row(matched_product_groups=None))
        assert "product_keywords" not in result

    def test_missing_description_is_omitted(self):
        result = AutomechanikaNormaliser().normalise(_row(description=None))
        assert "notes" not in result

    def test_empty_address_produces_no_country_or_address_fields(self):
        result = AutomechanikaNormaliser().normalise(_row(address=""))
        assert "country" not in result
        assert "address" not in result

    def test_completely_empty_row_still_returns_a_dict_with_canonical_name_key(self):
        """BaseNormalizer's own contract: canonical_name must always
        be present, even as an empty string, so a caller can check for
        it explicitly rather than hitting a KeyError."""
        result = AutomechanikaNormaliser().normalise({})
        assert "canonical_name" in result
        assert result["canonical_name"] == ""


class TestSourceName:

    def test_source_name_is_set(self):
        assert AutomechanikaNormaliser.source_name == "automechanika_2026"
