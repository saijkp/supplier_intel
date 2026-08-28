"""
tests/test_discovery_candidate_validator.py

Tests for discovery/candidate_validator.py -- the one LLM call in
Discovery Service, and the concrete anti-hallucination gate: a
candidate is only validated if the LLM's extracted name (read from a
real fetched page) corroborates the ORIGINAL search result, AND the
page text actually mentions the searched product term. Uses fakes for
both the LLM client and the website fetcher -- no real network/API key.
"""

from __future__ import annotations

from types import SimpleNamespace

from discovery.candidate_extractor import Candidate
from discovery.candidate_validator import (
    CandidateValidator,
    _core_product_term,
    _countries_plausibly_match,
    _distinctive_tokens,
    _mentions_product_term,
    _shares_distinctive_token,
)


class FakeLLMClient:
    def __init__(self, response=None):
        self._response = response
        self.calls = []

    def complete_json(self, system_prompt, user_prompt, **kwargs):
        self.calls.append((system_prompt, user_prompt))
        return self._response


class FakeWebsiteFetcher:
    def __init__(self, success=True, pages=None, error=None):
        self._success = success
        self._pages = pages if pages is not None else [SimpleNamespace(text="")]
        self._error = error
        self.calls = []

    def fetch(self, domain):
        self.calls.append(domain)
        return SimpleNamespace(success=self._success, pages=self._pages, error=self._error)


class ExplodingWebsiteFetcher:
    def fetch(self, domain):
        raise RuntimeError("network down")


def _candidate(title="Acme Trailer Co", snippet="Leading manufacturer of trailer axles"):
    return Candidate(title=title, link="https://acmetrailer.com/", snippet=snippet, domain="acmetrailer.com")


class TestCandidateValidator:

    def test_fully_corroborated_candidate_is_validated(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Welcome to Acme Trailer Co, manufacturer of trailer axle assemblies since 1998.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": "United Kingdom"})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is True
        assert result.extracted_name == "Acme Trailer Co"
        assert result.extracted_country == "United Kingdom"

    def test_fetch_failure_is_not_validated(self):
        fetcher = FakeWebsiteFetcher(success=False, error="404 not found")
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co"})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is False
        assert "could not fetch" in result.reason

    def test_fetcher_raising_does_not_propagate(self):
        validator = CandidateValidator(website_fetcher=ExplodingWebsiteFetcher(), llm_client=FakeLLMClient())
        result = validator.validate(_candidate(), "trailer axle")  # must not raise
        assert result.validated is False

    def test_empty_page_text_is_not_validated(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(text="")])
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=FakeLLMClient())
        result = validator.validate(_candidate(), "trailer axle")
        assert result.validated is False
        assert "no readable text" in result.reason

    def test_llm_returning_none_is_not_validated(self):
        """LLMClient itself already returns None on any failure --
        CandidateValidator must degrade gracefully, never crash."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(text="Some real page text about axles.")])
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=FakeLLMClient(response=None))
        result = validator.validate(_candidate(), "trailer axle")
        assert result.validated is False
        assert "invalid JSON" in result.reason

    def test_llm_returning_non_dict_is_not_validated(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(text="Some real page text.")])
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=FakeLLMClient(response=["not", "a", "dict"]))
        result = validator.validate(_candidate(), "trailer axle")
        assert result.validated is False

    def test_null_company_name_is_not_validated_never_invented(self):
        """The core anti-hallucination behaviour: if the LLM honestly
        reports no name was found in the text, the candidate is
        rejected -- never falls back to guessing from the domain or
        search snippet."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(text="A page with no clear company name stated.")])
        llm = FakeLLMClient(response={"company_name": None, "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is False
        assert result.extracted_name is None
        assert "no company name found" in result.reason

    def test_extracted_name_not_matching_search_result_is_rejected(self):
        """The extracted name must corroborate the ORIGINAL search
        result -- protects against a fetched page being an unrelated
        company that happens to share the candidate domain (e.g. a
        domain that changed hands)."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(text="Welcome to Totally Different Corp.")])
        llm = FakeLLMClient(response={"company_name": "Totally Different Corp", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(title="Acme Trailer Co", snippet="trailer axle manufacturer"), "trailer axle")

        assert result.validated is False
        assert "does not match the original search result" in result.reason

    def test_page_not_mentioning_product_term_is_rejected(self):
        """Deterministic keyword check, not another LLM call -- a
        genuinely matched company whose fetched page happens not to
        mention the specific searched product must not be accepted."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Welcome to Acme Trailer Co, a leading industrial manufacturer.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is False
        assert "does not mention the searched term" in result.reason

    def test_company_name_with_only_whitespace_is_rejected(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(text="Some real text about trailer axle.")])
        llm = FakeLLMClient(response={"company_name": "   ", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)
        result = validator.validate(_candidate(), "trailer axle")
        assert result.validated is False


class TestCoreProductTerm:
    """_core_product_term strips one trailing qualifier word --
    query_builder.py's own templates always append one
    ("{product} manufacturer", "{product} supplier", "{product}
    factory"), but no real company writes its own homepage repeating
    that exact tail."""

    def test_strips_trailing_manufacturer(self):
        assert _core_product_term("injection moulding manufacturer") == "injection moulding"

    def test_strips_trailing_supplier(self):
        assert _core_product_term("injection moulding supplier") == "injection moulding"

    def test_strips_trailing_factory(self):
        assert _core_product_term("injection moulding factory") == "injection moulding"

    def test_unqualified_term_is_unchanged(self):
        assert _core_product_term("injection moulding") == "injection moulding"

    def test_single_word_term_is_unchanged(self):
        """A bare qualifier word alone (e.g. someone searching for
        literally "manufacturer") isn't a qualified multi-word term --
        must not be stripped down to an empty string."""
        assert _core_product_term("manufacturer") == "manufacturer"

    def test_qualifier_only_stripped_from_the_end_not_the_middle(self):
        assert _core_product_term("manufacturer of injection moulding") == "manufacturer of injection moulding"


class TestMentionsProductTerm:
    """_mentions_product_term -- the loosened gate 6 core: core-phrase-
    only, spelling-insensitive for known British/American variants.
    Found via a real "injection moulding manufacturer" discovery run:
    71% of that run's rejections were genuine manufacturers
    (accurateplastics.net, cadrex.com, usainjectionmolding.com, among
    others) rejected purely because their own page said "molding", or
    didn't repeat the word "manufacturer" verbatim."""

    def test_exact_full_phrase_still_matches(self):
        assert _mentions_product_term("We are an injection moulding manufacturer.", "injection moulding manufacturer")

    def test_core_phrase_without_the_qualifier_now_matches(self):
        """The exact real-world case: a real manufacturer's page says
        "injection moulding" but never appends "manufacturer"."""
        assert _mentions_product_term("Custom injection moulding services since 1998.", "injection moulding manufacturer")

    def test_american_spelling_matches_a_british_search_term(self):
        assert _mentions_product_term("Precision injection molding for OEM customers.", "injection moulding manufacturer")

    def test_british_spelling_matches_an_american_search_term(self):
        assert _mentions_product_term("Precision injection moulding for OEM customers.", "injection molding manufacturer")

    def test_unrelated_page_does_not_match(self):
        assert not _mentions_product_term("We manufacture trailer axles and chassis components.", "injection moulding manufacturer")

    def test_still_case_insensitive(self):
        assert _mentions_product_term("INJECTION MOLDING SPECIALISTS SINCE 1998.", "injection moulding manufacturer")

    def test_unqualified_term_with_no_spelling_variant_is_unaffected(self):
        assert _mentions_product_term("We manufacture trailer axle assemblies.", "trailer axle")

    def test_material_handling_synonym_without_equipment_matches(self):
        """Real case: Interroll, a genuine conveyor/warehouse-logistics
        manufacturer, says "material handling" on its own page but
        never attaches "equipment"."""
        assert _mentions_product_term(
            "Interroll is a global leader in material handling solutions for warehouses.",
            "material handling equipment",
        )

    def test_handling_equipment_synonym_without_material_matches(self):
        """Real case: Mercia Lifting Gear, a genuine lifting-equipment
        manufacturer, says "handling equipment" but never attaches
        "material"."""
        assert _mentions_product_term(
            "We supply a wide range of lifting and handling equipment for industrial use.",
            "material handling equipment",
        )

    def test_full_compound_phrase_still_matches(self):
        assert _mentions_product_term(
            "We are a leading material handling equipment manufacturer.",
            "material handling equipment",
        )

    def test_synonym_is_specific_to_the_curated_term_not_global(self):
        """The synonym phrases are keyed to the exact core term --
        "handling equipment" alone must not satisfy an unrelated term
        that happens to share no relationship to material handling."""
        assert not _mentions_product_term(
            "We supply handling equipment for the postal industry.",
            "trailer axle",
        )

    def test_unrelated_page_still_rejected_even_with_synonyms_available(self):
        """Loosening this gate for a curated term must not turn it into
        a rubber stamp -- a genuinely unrelated business still fails."""
        assert not _mentions_product_term(
            "We manufacture trailer axles and chassis components.",
            "material handling equipment",
        )
        assert not _mentions_product_term("We manufacture wheel bearings.", "trailer axle")

    def test_metal_jacks_propstand_prop_vocabulary_matches(self):
        """Real case: Nice Steel (nicesteel.shop) and Baolai
        (baolaisteel.com) both sell "adjustable props"/"steel prop
        jack" but never the compound phrase "metal jacks and
        propstand" verbatim."""
        assert _mentions_product_term(
            "Reliable adjustable props for supporting slabs and beams during construction safely.",
            "metal jacks and propstand",
        )
        assert _mentions_product_term(
            "Steel Prop Jack -- one-stop production and processing for steel structures.",
            "metal jacks and propstand",
        )

    def test_metal_jacks_propstand_bare_jack_vocabulary_matches(self):
        """Real case: ARES Scaffolding and ACE Aluminium Scaffolding
        sell base/levelling/universal jacks and never say "prop"
        anywhere on their site."""
        assert _mentions_product_term(
            "Also known as Levelling Jack. The pipe is of Hot Rolled Steel as per BS-1139 Standard.",
            "metal jacks and propstand",
        )

    def test_metal_jacks_propstand_prop_and_jack_and_pairing_matches(self):
        """A page using both words but not one of the fixed multi-word
        phrases (e.g. neither "prop jack" nor "propstand" verbatim)
        still matches via the ("prop", "jack") AND pair."""
        assert _mentions_product_term(
            "We sell both prop and jack accessories to contractors nationwide.",
            "metal jacks and propstand",
        )

    def test_metal_jacks_propstand_unrelated_page_still_rejected(self):
        assert not _mentions_product_term(
            "We manufacture air suspension solutions for trailers, trucks and buses.",
            "metal jacks and propstand",
        )


class TestValidateEndToEndWithLoosenedTermMatching:

    def test_american_spelling_manufacturer_is_now_validated(self):
        """The concrete real-world fix: a real US injection molder,
        previously rejected outright, now validates."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Accurate Plastics Inc. is a precision injection molding company serving OEM customers.",
        )])
        llm = FakeLLMClient(response={"company_name": "Accurate Plastics Inc.", "country": "United States"})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(
            _candidate(title="Accurate Plastics Inc.", snippet="injection molding manufacturer"),
            "injection moulding manufacturer",
        )

        assert result.validated is True

    def test_core_phrase_without_manufacturer_wording_is_now_validated(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Acme Moulding Co offers custom injection moulding for a range of industries.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Moulding Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(title="Acme Moulding Co", snippet="injection moulding"), "injection moulding manufacturer")

        assert result.validated is True

    def test_genuinely_unrelated_page_is_still_rejected(self):
        """Loosening gate 6 must not turn it into a rubber stamp --
        an unrelated business still fails."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Acme Trailer Co manufactures trailer axles and chassis components.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "injection moulding manufacturer")

        assert result.validated is False
        assert "does not mention the searched term" in result.reason


class TestSelfDeclaredTraderExclusion:
    """The global, country-agnostic trader signal sourcing.
    SourcingAgentService needs -- the codebase's only other trader
    signal (ManufacturerVerifier via Qichacha) only has data for
    China-registered companies. This one is mechanical (a fixed phrase
    list), not another LLM call, checked only after every other gate
    already passed."""

    def test_self_declared_trading_company_is_rejected(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Welcome to Acme Trailer Co. We are a trading company specializing in trailer axle parts.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is False
        assert "trading company" in result.reason

    def test_self_declared_distributor_is_rejected(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Acme Trailer Co -- we are a distributor of trailer axle products across Europe.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is False
        assert "excluded, not a manufacturer" in result.reason

    def test_check_is_case_insensitive(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="ACME TRAILER CO -- WE ARE A TRADING COMPANY dealing in trailer axle parts.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is False

    def test_ordinary_mention_of_trading_does_not_trip_the_filter(self):
        """Precision matters more than recall here -- a genuine
        manufacturer's page mentioning "trading partners" in passing
        must not be excluded just for containing the word "trading"."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Acme Trailer Co manufactures trailer axle assemblies in-house. "
                 "We value our long-standing trading partners across the supply chain.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is True

    def test_genuine_manufacturer_page_is_still_validated(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Acme Trailer Co is a manufacturer of trailer axle assemblies, operating our own factory since 1998.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is True


class TestSoftTraderSignalExclusion:
    """_find_trader_soft_signal -- regex-based soft/indirect trader
    language, added after a real brake-cable-batch failure: two real
    buyer-confirmed FAILs (Auto & Trailer Spares, Towing and Trailers)
    both sailed through _TRADER_SELF_DECLARATION_PHRASES because neither
    ever writes the literal sentence "we are a distributor" -- see
    _TRADER_SOFT_SIGNAL_PATTERNS' own docstring for the real page text
    each pattern was written from."""

    def test_superlative_distributor_self_description_is_rejected(self):
        """Real text from autoandtrailer.com: "expanded to become
        Irelands largest trailer parts distributor"."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Acme Trailer Co was established over 20 years ago and has "
                 "expanded to become the UK's largest trailer axle distributor.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is False
        assert "matched soft signal" in result.reason

    def test_superlative_stockist_self_description_is_rejected(self):
        """Real text from towingandtrailers.com: "One of UK's largest
        stockists of Trailer-Parts"."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Acme Trailer Co - One of the UK's largest stockists of trailer axle parts.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is False

    def test_carries_other_manufacturers_output_is_rejected(self):
        """Real text from towingandtrailers.com: "This range of parts
        covers trailers by most manufacturers"."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Acme Trailer Co stocks a vast range of trailer axle parts "
                 "from leading manufacturers, covering most makes and models.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is False

    def test_authorized_distributor_badge_is_rejected(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Acme Trailer Co is an authorized distributor of premium trailer axle brands.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is False

    def test_trade_account_structural_page_is_rejected(self):
        """Real text from autoandtrailer.com: "Are you a Retailer,
        Wholesaler or Manufacturer? ... apply for a trade account"."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Trailer axle parts online. Are you a Retailer, Wholesaler "
                 "or Manufacturer?\nWhy not apply for a trade account with us? "
                 "Discounted prices across our full range.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is False

    def test_ordinary_mention_of_trading_partners_still_passes(self):
        """Regression: the existing precision-over-recall fixture must
        still pass with the new patterns added."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Acme Trailer Co manufactures trailer axle assemblies in-house. "
                 "We value our long-standing trading partners across the supply chain.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is True

    def test_manufacturer_describing_its_own_supply_is_not_rejected(self):
        """A genuine manufacturer describing itself as a "leading
        supplier" of its OWN output must not be caught -- "supplier" is
        deliberately excluded from the reseller-noun list since a
        manufacturer legitimately uses it for itself."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Acme Trailer Co is the UK's leading supplier of trailer axle assemblies, "
                 "manufactured in-house at our own factory.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is True

    def test_skip_soft_trader_signals_lets_a_real_dealer_through(self):
        """discovery.companies_house_sic_source's own opt-out: a real
        multi-brand dealer (Material Handling's own confirmed-roster
        language, e.g. Multy Lift Forktrucks -- "We stock a wide range
        of new & used forklifts from leading manufacturers") is the
        WANTED supplier type for that category, not a disqualifying
        signal -- skip_soft_trader_signals=True lets it through while
        the hard self-declaration phrases still apply."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Acme Trailer Co stocks a wide range of trailer axle parts "
                 "from leading manufacturers. Available for purchase and hire.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle", skip_soft_trader_signals=True)

        assert result.validated is True

    def test_skip_soft_trader_signals_still_applies_the_hard_phrase_list(self):
        """skip_soft_trader_signals only skips the NEW regex patterns --
        an explicit self-declaration ("we do not manufacture") is still
        rejected regardless."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Acme Trailer Co -- we are a distributor of trailer axle products across Europe.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle", skip_soft_trader_signals=True)

        assert result.validated is False
        assert "excluded, not a manufacturer" in result.reason

    def test_manufacturer_selling_through_distributor_network_is_not_rejected(self):
        """A genuine manufacturer describing its OWN downstream channel
        ("sold through our network of distributors") must not be caught
        -- a bare mention of "distributor" alone is not a self-
        declaration or a soft signal."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Acme Trailer Co manufactures trailer axle assemblies in-house, "
                 "sold through our network of authorized distributors worldwide.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is True


class TestOffDomainRedirectExclusion:
    """gate 3.6 -- a fetch that silently landed on a DIFFERENT real
    company's site (final_url's registered domain doesn't match the
    candidate's own domain) must be rejected, not trusted as if it
    were the candidate's own content. Real finding: duraauto.com's own
    homepage genuinely redirects to durashiloh.com."""

    def test_off_domain_redirect_is_rejected(self):
        duraauto_candidate = Candidate(
            title="Duraauto", link="https://duraauto.com/",
            snippet="trailer axle manufacturer", domain="duraauto.com",
        )
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Welcome to Durashiloh, a leading trailer axle manufacturer.",
            final_url="https://durashiloh.com/",
        )])
        llm = FakeLLMClient(response={"company_name": "Durashiloh", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(duraauto_candidate, "trailer axle")

        assert result.validated is False
        assert "duraauto.com -> durashiloh.com" in result.reason

    def test_same_domain_scheme_upgrade_redirect_still_validates(self):
        """A same-domain http->https or bare->www redirect is normal
        and must not be wrongly rejected -- domains_match() is already
        scheme/www-insensitive."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Welcome to Acme Trailer Co, manufacturer of trailer axle assemblies.",
            final_url="https://www.acmetrailer.com/",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is True

    def test_no_final_url_available_does_not_reject(self):
        """A fetcher/fake that doesn't populate final_url at all (every
        pre-existing test fixture in this file) must degrade to "no
        redirect detected," never a false reject."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Welcome to Acme Trailer Co, manufacturer of trailer axle assemblies.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(_candidate(), "trailer axle")

        assert result.validated is True

    def test_off_domain_redirect_checked_before_the_paid_llm_call(self):
        """Rejected at gate 3.6 must not spend the LLM call at all --
        same "reject cheap, before spending anything" discipline gate
        3.5 already follows."""
        duraauto_candidate = Candidate(
            title="Duraauto", link="https://duraauto.com/", snippet="", domain="duraauto.com",
        )
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Welcome to Durashiloh.", final_url="https://durashiloh.com/",
        )])
        llm = FakeLLMClient(response={"company_name": "Durashiloh", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        validator.validate(duraauto_candidate, "trailer axle")

        assert llm.calls == []


class TestMarketplaceHostExclusion:
    """A supplier whose only web presence is a B2B marketplace
    storefront is a negative manufacturer signal, not a valid company
    website -- checked before any fetch/LLM call (a marketplace page
    routinely contains a real company name and mentions the searched
    product, since it's advertising it, so without this gate such a
    candidate could otherwise sail through every other check)."""

    def _marketplace_candidate(self, domain):
        return Candidate(
            title="Acme Trailer Co", link=f"https://{domain}/", snippet="trailer axle manufacturer", domain=domain,
        )

    def test_goldsupplier_domain_is_rejected(self):
        fetcher = FakeWebsiteFetcher()
        llm = FakeLLMClient()
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(self._marketplace_candidate("acmetrailer.goldsupplier.com"), "trailer axle")

        assert result.validated is False
        assert "marketplace" in result.reason
        assert fetcher.calls == []  # never even attempted to fetch

    def test_alibaba_root_domain_is_rejected(self):
        fetcher = FakeWebsiteFetcher()
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=FakeLLMClient())
        result = validator.validate(self._marketplace_candidate("alibaba.com"), "trailer axle")
        assert result.validated is False
        assert fetcher.calls == []

    def test_en_alibaba_subdomain_is_rejected(self):
        """The exact real-world shape: a company's storefront under
        Alibaba's own en.alibaba.com subdomain, not their own site."""
        fetcher = FakeWebsiteFetcher()
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=FakeLLMClient())
        result = validator.validate(self._marketplace_candidate("acmetrailer.en.alibaba.com"), "trailer axle")
        assert result.validated is False
        assert fetcher.calls == []

    def test_made_in_china_domain_is_rejected(self):
        fetcher = FakeWebsiteFetcher()
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=FakeLLMClient())
        result = validator.validate(self._marketplace_candidate("acmetrailer.made-in-china.com"), "trailer axle")
        assert result.validated is False
        assert fetcher.calls == []

    def test_indiamart_domain_is_rejected(self):
        fetcher = FakeWebsiteFetcher()
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=FakeLLMClient())
        result = validator.validate(self._marketplace_candidate("acmetrailer.indiamart.com"), "trailer axle")
        assert result.validated is False
        assert fetcher.calls == []

    def test_tradeindia_domain_is_rejected(self):
        fetcher = FakeWebsiteFetcher()
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=FakeLLMClient())
        result = validator.validate(self._marketplace_candidate("acmetrailer.tradeindia.com"), "trailer axle")
        assert result.validated is False
        assert fetcher.calls == []

    def test_no_fetch_or_llm_call_is_made_for_a_marketplace_domain(self):
        """Rejected before any real work happens -- saves the HTTP
        fetch and the LLM call, not just the eventual storage."""
        fetcher = FakeWebsiteFetcher()
        llm = FakeLLMClient()
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        validator.validate(self._marketplace_candidate("acmetrailer.alibaba.com"), "trailer axle")

        assert fetcher.calls == []

    def test_a_companys_own_domain_that_merely_contains_a_marketplace_name_is_not_rejected(self):
        """Precision matters -- this checks the REGISTERED domain via
        tldextract, not a raw substring, so a company legitimately
        named e.g. "Alibabatrading Co" at its own domain must not be
        caught by this filter."""
        candidate = Candidate(
            title="Alibabatrading Co", link="https://alibabatrading.com/",
            snippet="trailer axle manufacturer", domain="alibabatrading.com",
        )
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Alibabatrading Co is a manufacturer of trailer axle assemblies.",
        )])
        llm = FakeLLMClient(response={"company_name": "Alibabatrading Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)

        result = validator.validate(candidate, "trailer axle")

        assert fetcher.calls == ["alibabatrading.com"]  # fetch WAS attempted -- not a marketplace host
        assert result.validated is True


class FakeGoogleScraper:
    """Mirrors tests/test_discovery_service.py's own FakeGoogleScraper
    convention -- a fixed result set, recording every query for
    assertion."""

    def __init__(self, results=None, raise_error=None):
        self._results = results if results is not None else []
        self._raise_error = raise_error
        self.queries = []

    def scrape(self, query, max_results=20, **kwargs):
        self.queries.append(query)
        if self._raise_error:
            raise self._raise_error
        return self._results


def _search_result(link, title="", snippet=""):
    return SimpleNamespace(success=True, raw_data={"link": link, "title": title, "snippet": snippet})


class TestRecover:
    """recover() must go through the SAME validate() gate as any other
    candidate -- zero shortcuts, zero special trust. See
    discovery_service.py's own _process_candidate for the caller-side
    rule this exists to serve: only called for a dead/unreachable
    domain, never marketplace/trader/name-mismatch/term-missing."""

    def test_recovers_a_real_top_result(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Welcome to Acme Trailer Co, manufacturer of trailer axle assemblies.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": "UK"})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)
        scraper = FakeGoogleScraper(results=[_search_result("https://acmetrailer.com/", "Acme Trailer Co")])

        result = validator.recover("Acme Trailer Co", "trailer axle", scraper, country="UK")

        assert result is not None
        assert result.validated is True
        assert result.candidate.domain == "acmetrailer.com"
        assert len(scraper.queries) == 1
        assert scraper.queries[0].startswith('"Acme Trailer Co" UK ')
        assert "-site:yell.com" in scraper.queries[0]  # a directory exclusion is actually applied
        assert "-site:alibaba.com" in scraper.queries[0]  # PLATFORM_REGISTERED_DOMAINS reused, not duplicated

    def test_query_omits_country_when_not_given(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(text="Acme Trailer Co trailer axle manufacturer.")])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)
        scraper = FakeGoogleScraper(results=[_search_result("https://acmetrailer.com/", "Acme Trailer Co")])

        validator.recover("Acme Trailer Co", "trailer axle", scraper)

        assert scraper.queries[0].startswith('"Acme Trailer Co" -site:')

    def test_returns_none_when_no_search_results(self):
        fetcher = FakeWebsiteFetcher()
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=FakeLLMClient())
        scraper = FakeGoogleScraper(results=[])

        result = validator.recover("Nonexistent Co", "trailer axle", scraper)

        assert result is None
        assert fetcher.calls == []  # never even attempted a fetch -- nothing to try

    def test_returns_none_when_search_itself_errors(self):
        validator = CandidateValidator(website_fetcher=FakeWebsiteFetcher(), llm_client=FakeLLMClient())
        scraper = FakeGoogleScraper(raise_error=RuntimeError("SerpAPI down"))

        result = validator.recover("Acme Trailer Co", "trailer axle", scraper)  # must not raise

        assert result is None

    def test_tries_a_second_candidate_only_if_the_first_fails(self):
        """A result is only fetched if an earlier one didn't validate,
        not unconditionally -- proven here with exactly 2 results
        available regardless of the default max_candidates cap."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(text="Unrelated content, no company name here.")])
        llm = FakeLLMClient(response=None)  # first candidate: LLM extraction fails -> not validated
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)
        scraper = FakeGoogleScraper(results=[
            _search_result("https://wrongsite.example.com/", "Wrong Site"),
            _search_result("https://acmetrailer.com/", "Acme Trailer Co"),
        ])

        result = validator.recover("Acme Trailer Co", "trailer axle", scraper)

        assert result is None  # both fail with this fetcher/llm combo
        assert fetcher.calls == ["wrongsite.example.com", "acmetrailer.com"]  # both were tried

    def test_default_max_candidates_is_5(self):
        """Raised from an original 2 -- a live pilot found 2 too tight
        to reach a real company site past several listing-site results
        for a common UK business name (see _RECOVERY_EXCLUDED_HOSTS'
        own docstring). Neither discovery_service.py nor batch_service.py
        override this anymore -- both now rely on this default."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(text="Unrelated content, no company name here.")])
        llm = FakeLLMClient(response=None)  # every candidate fails to extract a name -> never validates
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)
        scraper = FakeGoogleScraper(results=[
            _search_result(f"https://site{i}.example.com/", f"Site {i}") for i in range(6)
        ])

        result = validator.recover("Acme Trailer Co", "trailer axle", scraper)

        assert result is None
        assert len(fetcher.calls) == 5  # the default cap, not all 6 available results

    def test_stops_after_first_success_never_tries_a_third(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Welcome to Acme Trailer Co, manufacturer of trailer axle assemblies.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)
        scraper = FakeGoogleScraper(results=[
            _search_result("https://acmetrailer.com/", "Acme Trailer Co"),
            _search_result("https://another.example.com/", "Another Co"),
            _search_result("https://third.example.com/", "Third Co"),
        ])

        result = validator.recover("Acme Trailer Co", "trailer axle", scraper, max_candidates=2)

        assert result is not None
        assert fetcher.calls == ["acmetrailer.com"]  # stopped after the first success

    def test_recovered_candidate_still_fails_a_real_gate_if_it_should(self):
        """The whole point: no special trust. A recovered top result
        that doesn't actually mention the product term is rejected
        exactly like any other candidate would be."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Acme Trailer Co sells garden furniture and patio sets.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)
        scraper = FakeGoogleScraper(results=[_search_result("https://acmetrailer.com/", "Acme Trailer Co")])

        result = validator.recover("Acme Trailer Co", "trailer axle", scraper)

        assert result is None

    def test_rejects_a_self_consistent_but_wrong_company(self):
        """Real case found in a live pilot: recovering "Apadrecoplastics"
        (a dead domain) matched cleanly onto adrecoplastics.co.uk -- the
        real, self-consistent site of an unrelated company, Adreco
        Plastics. validate()'s own gate 5 only checks the extracted name
        against the SAME candidate's SERP snippet, which is trivially
        self-consistent here -- the corroboration check against the
        ORIGINAL company_name must be what catches this."""
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Welcome to Adreco Plastics, a UK injection moulding company.",
        )])
        llm = FakeLLMClient(response={"company_name": "Adreco Plastics", "country": "United Kingdom"})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)
        scraper = FakeGoogleScraper(results=[_search_result("https://adrecoplastics.co.uk/", "Adreco Plastics")])

        result = validator.recover("Apadrecoplastics", "injection moulding", scraper)

        assert result is None

    def test_accepts_a_genuine_near_miss_with_shared_distinctive_word(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Beta Bearing Ltd manufactures trailer axle bearings.",
        )])
        llm = FakeLLMClient(response={"company_name": "Beta Bearing Ltd", "country": None})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)
        scraper = FakeGoogleScraper(results=[_search_result("https://betabearing.example.com/", "Beta Bearing Ltd")])

        result = validator.recover("Beta Bearings Ltd", "trailer axle", scraper)

        assert result is not None
        assert result.validated is True

    def test_existing_country_mismatch_rejects_even_with_name_match(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Acme Trailer Co, based in China, manufactures trailer axles.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": "China"})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)
        scraper = FakeGoogleScraper(results=[_search_result("https://acmetrailer.com/", "Acme Trailer Co")])

        result = validator.recover("Acme Trailer Co", "trailer axle", scraper, existing_country="United Kingdom")

        assert result is None

    def test_existing_country_match_accepts(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Acme Trailer Co, based in the UK, manufactures trailer axles.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": "United Kingdom"})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)
        scraper = FakeGoogleScraper(results=[_search_result("https://acmetrailer.com/", "Acme Trailer Co")])

        result = validator.recover("Acme Trailer Co", "trailer axle", scraper, existing_country="United Kingdom")

        assert result is not None

    def test_no_existing_country_given_does_not_block(self):
        fetcher = FakeWebsiteFetcher(pages=[SimpleNamespace(
            text="Acme Trailer Co, based in China, manufactures trailer axles.",
        )])
        llm = FakeLLMClient(response={"company_name": "Acme Trailer Co", "country": "China"})
        validator = CandidateValidator(website_fetcher=fetcher, llm_client=llm)
        scraper = FakeGoogleScraper(results=[_search_result("https://acmetrailer.com/", "Acme Trailer Co")])

        result = validator.recover("Acme Trailer Co", "trailer axle", scraper)  # no existing_country

        assert result is not None


class TestDistinctiveTokens:

    def test_shares_a_common_significant_word(self):
        assert _shares_distinctive_token("Beta Bearings Ltd", "Beta Bearing Ltd") is True

    def test_no_shared_word_rejects(self):
        assert _shares_distinctive_token("Apadrecoplastics", "Adreco Plastics") is False

    def test_identical_names_share_tokens(self):
        assert _shares_distinctive_token("Murray Plastics", "Murray Plastics") is True

    def test_nothing_distinctive_on_either_side_does_not_block(self):
        """Short/generic-only names (e.g. below the length-4 floor, or
        entirely corporate-suffix words) leave nothing to compare --
        that's insufficient signal for a rejection, not evidence of one."""
        assert _shares_distinctive_token("ABC Ltd", "Co Inc") is True

    def test_distinctive_tokens_strips_generic_corporate_words(self):
        assert _distinctive_tokens("Acme Trailer Co Ltd") == {"acme", "trailer"}

    def test_shared_industry_vocabulary_word_alone_does_not_count(self):
        """Real second false match found live: recovering "Ability
        Handling" (a dead domain) matched onto the real site of "Grant
        Handling" -- a completely unrelated company -- because both
        names include "Handling", which this check treated as proof of
        identity before it was added to _GENERIC_NAME_WORDS alongside
        the legal suffixes. Same failure MODE as the original
        Apadrecoplastics/Adreco Plastics case: a word common enough
        within one product category to appear in many unrelated
        companies' names proves nothing about identity."""
        assert _shares_distinctive_token("Ability Handling", "Grant Handling") is False

    def test_distinctive_tokens_strips_industry_vocabulary_too(self):
        assert _distinctive_tokens("Global Material Handling") == {"global"}
        assert _distinctive_tokens("Mitsubishi Forklift Trucks UK") == {"mitsubishi"}

    def test_genuine_company_name_overlap_still_passes_alongside_industry_words(self):
        """The stoplist only strips the generic word -- a genuinely
        shared, distinctive company-name word right next to it still
        counts, same as it always has for corporate suffixes."""
        assert _shares_distinctive_token("Acme Forklift Trucks", "Acme Forklifts Ltd") is True


class TestCountriesPlausiblyMatch:

    def test_exact_match(self):
        assert _countries_plausibly_match("China", "China") is True

    def test_uk_synonyms_match(self):
        assert _countries_plausibly_match("England", "United Kingdom") is True

    def test_different_countries_do_not_match(self):
        assert _countries_plausibly_match("United Kingdom", "China") is False

    def test_either_side_empty_does_not_block(self):
        assert _countries_plausibly_match("", "China") is True
        assert _countries_plausibly_match("United Kingdom", "") is True
