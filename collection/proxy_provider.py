"""
collection/proxy_provider.py

Pluggable rotating-proxy interface for collection/site_collector.py.
Webshare is the only provider actually implemented -- chosen over
Bright Data/Oxylabs/Decodo/IPRoyal (all named in the original brief)
because it has a genuine free tier to build/test against and the
cheapest paid tier of the five, given this project's demonstrated real
budget sensitivity (both Apify and SerpAPI free/low tiers were
exhausted in the same session this redesign was scoped in). The other
four are documented extension points -- same ProxyProvider interface,
NotImplementedError until someone actually builds one, so a caller that
mistakenly selects one fails loudly rather than silently collecting
with no proxy at all.

Webshare needs no SDK: its proxies are plain HTTP-proxy basic auth,
directly consumable by Playwright's own `proxy=` launch option.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from config.settings import (
    COLLECTION_PROXY_PROVIDER,
    WEBSHARE_PROXY_ENDPOINT,
    WEBSHARE_PROXY_PASSWORD,
    WEBSHARE_PROXY_USERNAME,
)

logger = logging.getLogger(__name__)


class ProxyProvider(ABC):

    @abstractmethod
    def is_configured(self) -> bool:
        """Whether this provider has everything it needs (credentials,
        etc.) to actually be used. NoProxyProvider is always
        configured; the unimplemented stubs are never configured."""

    @abstractmethod
    def get_proxy_config(self) -> Optional[Dict[str, Any]]:
        """Returns a dict shaped for Playwright's `proxy=` launch
        option (`{"server", "username", "password"}`), or None for "no
        proxy." Never raises for NoProxyProvider or WebshareProxyProvider
        (an unconfigured Webshare provider just returns None, same as
        NoProxyProvider) -- the unimplemented stub providers are the
        only ones that raise, and only if actually selected."""


class NoProxyProvider(ProxyProvider):
    """Direct connection -- no proxy. The default
    (COLLECTION_PROXY_PROVIDER='none'), and what site_collector.py
    falls back to if a configured provider turns out not to have real
    credentials set."""

    def is_configured(self) -> bool:
        return True

    def get_proxy_config(self) -> Optional[Dict[str, Any]]:
        return None


class WebshareProxyProvider(ProxyProvider):

    def __init__(
        self, username: Optional[str] = None, password: Optional[str] = None, endpoint: Optional[str] = None,
    ):
        self.username = username if username is not None else WEBSHARE_PROXY_USERNAME
        self.password = password if password is not None else WEBSHARE_PROXY_PASSWORD
        self.endpoint = endpoint if endpoint is not None else WEBSHARE_PROXY_ENDPOINT

    def is_configured(self) -> bool:
        return bool(self.username and self.password)

    def get_proxy_config(self) -> Optional[Dict[str, Any]]:
        if not self.is_configured():
            return None
        return {
            "server": f"http://{self.endpoint}",
            "username": self.username,
            "password": self.password,
        }


class _UnimplementedProxyProvider(ProxyProvider):
    """Base for the four providers named in the original brief that
    aren't built yet. Subclasses only need to set _PROVIDER_NAME."""

    _PROVIDER_NAME = "unimplemented"

    def is_configured(self) -> bool:
        return False

    def get_proxy_config(self) -> Optional[Dict[str, Any]]:
        raise NotImplementedError(
            f"{self._PROVIDER_NAME} is a documented extension point, not implemented yet -- "
            f"implement it against the ProxyProvider interface (see WebshareProxyProvider in "
            f"this module for the pattern) before selecting it via COLLECTION_PROXY_PROVIDER."
        )


class BrightDataProxyProvider(_UnimplementedProxyProvider):
    _PROVIDER_NAME = "Bright Data"


class OxylabsProxyProvider(_UnimplementedProxyProvider):
    _PROVIDER_NAME = "Oxylabs"


class DecodoProxyProvider(_UnimplementedProxyProvider):
    _PROVIDER_NAME = "Decodo"


class IPRoyalProxyProvider(_UnimplementedProxyProvider):
    _PROVIDER_NAME = "IPRoyal"


_PROVIDERS: Dict[str, type] = {
    "none": NoProxyProvider,
    "webshare": WebshareProxyProvider,
    "brightdata": BrightDataProxyProvider,
    "oxylabs": OxylabsProxyProvider,
    "decodo": DecodoProxyProvider,
    "iproyal": IPRoyalProxyProvider,
}


def select_proxy_provider(name: Optional[str] = None) -> ProxyProvider:
    """`name` defaults to config.settings.COLLECTION_PROXY_PROVIDER.
    An unknown name falls back to NoProxyProvider with a logged
    warning rather than raising -- a misconfigured/misspelled provider
    name shouldn't take down Collection Service, just mean it collects
    without a proxy."""
    key = (name or COLLECTION_PROXY_PROVIDER or "none").strip().lower()
    provider_cls = _PROVIDERS.get(key)
    if provider_cls is None:
        logger.warning("Unknown proxy provider %r, falling back to no proxy", key)
        return NoProxyProvider()
    return provider_cls()
