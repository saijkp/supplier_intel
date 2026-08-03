"""
tests/test_proxy_provider.py

Tests for collection/proxy_provider.py -- the pluggable rotating-proxy
interface. WebshareProxyProvider is the only one actually implemented
(see that module's own docstring for why); BrightData/Oxylabs/Decodo/
IPRoyal are documented stubs this file also verifies fail loudly rather
than silently, if selected.
"""

from __future__ import annotations

import pytest

from collection.proxy_provider import (
    BrightDataProxyProvider,
    DecodoProxyProvider,
    IPRoyalProxyProvider,
    NoProxyProvider,
    OxylabsProxyProvider,
    ProxyProvider,
    WebshareProxyProvider,
    select_proxy_provider,
)


class TestNoProxyProvider:

    def test_always_configured(self):
        assert NoProxyProvider().is_configured() is True

    def test_returns_none_for_proxy_config(self):
        assert NoProxyProvider().get_proxy_config() is None


class TestWebshareProxyProvider:

    def test_configured_when_username_and_password_given(self):
        provider = WebshareProxyProvider(username="user", password="pass")
        assert provider.is_configured() is True

    def test_not_configured_without_credentials(self):
        provider = WebshareProxyProvider(username=None, password=None)
        assert provider.is_configured() is False

    def test_get_proxy_config_shape_matches_playwright_launch_option(self):
        provider = WebshareProxyProvider(username="user", password="pass", endpoint="p.webshare.io:80")
        config = provider.get_proxy_config()
        assert config == {"server": "http://p.webshare.io:80", "username": "user", "password": "pass"}

    def test_unconfigured_provider_returns_none_not_raise(self):
        provider = WebshareProxyProvider(username=None, password=None)
        assert provider.get_proxy_config() is None  # must not raise


class TestUnimplementedProxyProviders:

    @pytest.mark.parametrize("provider_cls", [
        BrightDataProxyProvider, OxylabsProxyProvider, DecodoProxyProvider, IPRoyalProxyProvider,
    ])
    def test_not_configured(self, provider_cls):
        assert provider_cls().is_configured() is False

    @pytest.mark.parametrize("provider_cls", [
        BrightDataProxyProvider, OxylabsProxyProvider, DecodoProxyProvider, IPRoyalProxyProvider,
    ])
    def test_get_proxy_config_raises_not_implemented(self, provider_cls):
        """A caller that mistakenly selects one of these must fail
        loudly, not silently fall through to collecting with no proxy
        at all."""
        with pytest.raises(NotImplementedError):
            provider_cls().get_proxy_config()

    def test_all_are_real_proxy_provider_subclasses(self):
        for cls in (BrightDataProxyProvider, OxylabsProxyProvider, DecodoProxyProvider, IPRoyalProxyProvider):
            assert issubclass(cls, ProxyProvider)


class TestSelectProxyProvider:

    def test_explicit_none_selects_no_proxy(self):
        assert isinstance(select_proxy_provider("none"), NoProxyProvider)

    def test_explicit_webshare_selects_webshare(self):
        assert isinstance(select_proxy_provider("webshare"), WebshareProxyProvider)

    def test_case_insensitive(self):
        assert isinstance(select_proxy_provider("WEBSHARE"), WebshareProxyProvider)

    def test_unknown_name_falls_back_to_no_proxy(self):
        assert isinstance(select_proxy_provider("totally-unknown-provider"), NoProxyProvider)

    def test_defaults_to_config_setting_when_name_omitted(self, monkeypatch):
        import collection.proxy_provider as pp_module

        monkeypatch.setattr(pp_module, "COLLECTION_PROXY_PROVIDER", "webshare")
        assert isinstance(select_proxy_provider(), WebshareProxyProvider)
