"""Hosted model APIs with explicit disclosure admission and provider usage."""

from potbot.models.providers.client import HostedProviderClient
from potbot.models.providers.contracts import (
    CATALOG_ORIGINS,
    DEFAULT_PROTOCOLS,
    ORIGINS,
    PROVIDERS,
    CatalogModel,
    ProviderAPIError,
    ProviderCompleted,
    ProviderEvent,
    ProviderRequest,
    ProviderSelection,
    ProviderTextDelta,
    ProviderUsage,
    default_protocol,
    is_known_non_text_model,
)

__all__ = [
    "CATALOG_ORIGINS",
    "DEFAULT_PROTOCOLS",
    "ORIGINS",
    "PROVIDERS",
    "CatalogModel",
    "HostedProviderClient",
    "ProviderAPIError",
    "ProviderCompleted",
    "ProviderEvent",
    "ProviderRequest",
    "ProviderSelection",
    "ProviderTextDelta",
    "ProviderUsage",
    "default_protocol",
    "is_known_non_text_model",
]
