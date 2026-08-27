"""
tests/test_fabtech_exhibitor_normalizer.py

Tests for normalizers.fabtech_exhibitor_normalizer.FabtechExhibitorNormalizer.
The sample rows below reflect real fields extracted from real FABTECH
exhibitor profile pages during this module's own development (Intermark
Steel, FARO CREAFORM), not synthetic data -- same discipline as
tests/test_linde_dealer_normalizer.py.
"""

from __future__ import annotations

from normalizers.fabtech_exhibitor_normalizer import FabtechExhibitorNormalizer


def _exhibitor(**overrides):
    base = {
        "name": "Intermark Steel",
        "booth_id": "517394",
        "booth_number": "A3345",
        "website": "http://intermarksteel.com",
        "address": "650 S 500 W Ste 101\nSalt Lake City, UT 84101-2378\nUnited States",
        "phone": "435-637-4435",
        "pavilion": "Forming & Fabricating",
    }
    base.update(overrides)
    return base


class TestRealExhibitorRow:

    def test_name_maps_to_canonical_name(self):
        result = FabtechExhibitorNormalizer().normalise(_exhibitor())
        assert result["canonical_name"] == "Intermark Steel"

    def test_website_becomes_a_clean_domain(self):
        result = FabtechExhibitorNormalizer().normalise(_exhibitor())
        assert result["domain"] == "intermarksteel.com"

    def test_address_lines_are_rejoined_with_commas(self):
        result = FabtechExhibitorNormalizer().normalise(_exhibitor())
        assert result["address"] == "650 S 500 W Ste 101, Salt Lake City, UT 84101-2378, United States"

    def test_country_is_the_last_address_line(self):
        result = FabtechExhibitorNormalizer().normalise(_exhibitor())
        assert result["country"] == "United States"

    def test_city_is_parsed_from_the_second_to_last_line(self):
        result = FabtechExhibitorNormalizer().normalise(_exhibitor())
        assert result["city"] == "Salt Lake City"

    def test_phone_maps_to_primary_phone_unmodified(self):
        result = FabtechExhibitorNormalizer().normalise(_exhibitor())
        assert result["primary_phone"] == "435-637-4435"

    def test_discovery_source_is_stamped(self):
        result = FabtechExhibitorNormalizer().normalise(_exhibitor())
        assert result["discovery_source"] == "trade-show-exhibitor-fabtech"

    def test_booth_number_and_pavilion_are_not_mapped_to_any_field(self):
        """Only name/address/phone/website -- booth_number/pavilion have
        no SUPPLIER_WRITABLE_FIELDS column, kept only in raw_source_data,
        same precedent as automechanika_normalizer's hall_stand."""
        result = FabtechExhibitorNormalizer().normalise(_exhibitor())
        assert "booth_number" not in result
        assert "pavilion" not in result

    def test_canadian_address_real_sample(self):
        """Real profile data: FARO CREAFORM."""
        result = FabtechExhibitorNormalizer().normalise(_exhibitor(
            name="FARO CREAFORM", website="http://www.creaform3d.com",
            address="4700 rue de la Pascaline\nLévis, QC G6W 0L9\nCanada",
        ))
        assert result["country"] == "Canada"
        assert result["city"] == "Lévis"
        assert result["domain"] == "creaform3d.com"


class TestMissingFields:

    def test_missing_website_produces_no_domain(self):
        result = FabtechExhibitorNormalizer().normalise(_exhibitor(website=""))
        assert "domain" not in result
        assert result["canonical_name"] == "Intermark Steel"

    def test_two_line_address_has_country_but_no_city(self):
        """Street + country only, no separate city line -- must never
        misattribute the street itself as the city."""
        result = FabtechExhibitorNormalizer().normalise(_exhibitor(
            address="123 Main St\nUnited States",
        ))
        assert result["country"] == "United States"
        assert "city" not in result

    def test_single_line_address_produces_no_country(self):
        """No delimiters to safely split on -- don't guess a country
        from a run-on blob, same discipline as automechanika_normalizer's
        own no-comma guard."""
        result = FabtechExhibitorNormalizer().normalise(_exhibitor(address="Some Street Only"))
        assert "country" not in result
        assert "city" not in result
        assert result["address"] == "Some Street Only"

    def test_completely_missing_address_produces_no_address_fields(self):
        result = FabtechExhibitorNormalizer().normalise(_exhibitor(address=""))
        assert "address" not in result
        assert "country" not in result
        assert "city" not in result

    def test_missing_phone_produces_no_primary_phone(self):
        result = FabtechExhibitorNormalizer().normalise(_exhibitor(phone=""))
        assert "primary_phone" not in result

    def test_missing_name_still_returns_a_result_with_empty_canonical_name(self):
        result = FabtechExhibitorNormalizer().normalise(_exhibitor(name=""))
        assert result["canonical_name"] == ""
