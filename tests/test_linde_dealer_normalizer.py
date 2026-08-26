"""
tests/test_linde_dealer_normalizer.py

Tests for normalizers.linde_dealer_normalizer.LindeDealerNormalizer.
The sample row below is copied verbatim from a real entry in Linde's
own live Dealer-Finder-App-Data.json, not synthetic data -- same
discipline as tests/test_automechanika_normalizer.py.
"""

from __future__ import annotations

from normalizers.linde_dealer_normalizer import LindeDealerNormalizer, _iso_country_name


def _dealer(**overrides):
    base = {
        "city": "Buenos Aires",
        "cityRanges": [],
        "continent": "sa",
        "country": "ar",
        "fax": "+54 11 4717-6221",
        "latitude": "-34.501684",
        "logo": "https://www.linde-mh.com/media/Global-Content/07_Company-Logos/agpruden_com_ar.jpg",
        "longitude": "-58.526985",
        "mail": "ventas@agpruden.com",
        "name": "A.G. Pruden & Cia. S.A.",
        "notes": "",
        "phone": "+54 11 4733-2500",
        "street": "Av. Hipolito Yrigoyen 2441 | Martinez, Argentina",
        "typeCode": "linde",
        "website": "https://www.agpruden.com",
        "zip": "B1640HFW",
    }
    base.update(overrides)
    return base


class TestRealDealerRow:

    def test_name_maps_to_canonical_name(self):
        result = LindeDealerNormalizer().normalise(_dealer())
        assert result["canonical_name"] == "A.G. Pruden & Cia. S.A."

    def test_website_becomes_a_clean_domain(self):
        result = LindeDealerNormalizer().normalise(_dealer())
        assert result["domain"] == "agpruden.com"

    def test_country_code_converts_to_full_name(self):
        result = LindeDealerNormalizer().normalise(_dealer())
        assert result["country"] == "Argentina"

    def test_phone_maps_to_primary_phone_unmodified(self):
        result = LindeDealerNormalizer().normalise(_dealer())
        assert result["primary_phone"] == "+54 11 4733-2500"

    def test_email_maps_to_primary_email_unmodified(self):
        result = LindeDealerNormalizer().normalise(_dealer())
        assert result["primary_email"] == "ventas@agpruden.com"

    def test_address_combines_street_city_zip(self):
        result = LindeDealerNormalizer().normalise(_dealer())
        assert result["address"] == "Av. Hipolito Yrigoyen 2441 | Martinez, Argentina, Buenos Aires, B1640HFW"

    def test_city_is_also_stored_separately(self):
        result = LindeDealerNormalizer().normalise(_dealer())
        assert result["city"] == "Buenos Aires"

    def test_discovery_source_is_stamped(self):
        result = LindeDealerNormalizer().normalise(_dealer())
        assert result["discovery_source"] == "linde-oem-dealer-network"

    def test_fax_is_not_mapped_to_any_field(self):
        """Only name/address/phone/website/email -- fax has no
        SUPPLIER_WRITABLE_FIELDS column and was never asked for."""
        result = LindeDealerNormalizer().normalise(_dealer())
        assert "fax" not in result


class TestMissingFields:

    def test_missing_website_produces_no_domain(self):
        result = LindeDealerNormalizer().normalise(_dealer(website=""))
        assert "domain" not in result
        assert result["canonical_name"] == "A.G. Pruden & Cia. S.A."

    def test_missing_country_code_produces_no_country(self):
        result = LindeDealerNormalizer().normalise(_dealer(country=""))
        assert "country" not in result

    def test_unrecognised_country_code_produces_no_country(self):
        result = LindeDealerNormalizer().normalise(_dealer(country="zz"))
        assert "country" not in result

    def test_missing_street_and_zip_still_uses_city(self):
        result = LindeDealerNormalizer().normalise(_dealer(street="", zip=""))
        assert result["address"] == "Buenos Aires"

    def test_completely_missing_address_fields_produce_no_address(self):
        result = LindeDealerNormalizer().normalise(_dealer(street="", city="", zip=""))
        assert "address" not in result
        assert "city" not in result

    def test_missing_name_still_returns_a_result_with_empty_canonical_name(self):
        """BaseNormalizer's own contract: canonical_name always present,
        even empty, never a KeyError for a caller checking it."""
        result = LindeDealerNormalizer().normalise(_dealer(name=""))
        assert result["canonical_name"] == ""


class TestIsoCountryName:

    def test_lowercase_code_converts(self):
        assert _iso_country_name("ar") == "Argentina"

    def test_uppercase_code_converts(self):
        assert _iso_country_name("DE") == "Germany"

    def test_gb_converts_to_united_kingdom(self):
        assert _iso_country_name("gb") == "United Kingdom"

    def test_unrecognised_code_returns_empty_string(self):
        assert _iso_country_name("zz") == ""

    def test_none_returns_empty_string(self):
        assert _iso_country_name(None) == ""

    def test_empty_string_returns_empty_string(self):
        assert _iso_country_name("") == ""
