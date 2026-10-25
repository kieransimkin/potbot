"""Scoped model directory with explicit metadata discovery and bounded memory caching."""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import canonical_json
from potbot.models.hosted_policy import HostedPolicy
from potbot.models.profiles import default_model_profiles
from potbot.models.providers import (
    PROVIDERS,
    CatalogModel,
    HostedProviderClient,
    ProviderAPIError,
    ProviderSelection,
    default_protocol,
    is_known_non_text_model,
)
from potbot.models.providers.contracts import provider_name
from potbot.persistence.database import Database
from potbot.service_credentials import ServiceCredentials
from potbot.tools.secrets import CaptureRedactor

_LABELS = {
    "anthropic": "Claude (Anthropic)",
    "openai": "OpenAI",
    "xai": "Grok (xAI)",
    "gemini": "Gemini (Google)",
    "evolink": "EvoLink.ai",
}


@dataclass(frozen=True, slots=True)
class _CachedCatalog:
    models: tuple[CatalogModel, ...]
    discovered_at: str
    expires_at: float
    size_bytes: int


class HostedModelDirectory:
    """Read-only directory callbacks for authenticated HTTP and CLI adapters.

    Discovery uses metadata GETs only and grants no generation authority. The
    process-local cache is scoped by workspace/provider, has TTL/LRU/byte bounds,
    and never substitutes for a fresh database membership check.
    """

    def __init__(
        self,
        database: Database,
        policy: HostedPolicy,
        credentials: ServiceCredentials,
        *,
        cache_ttl_seconds: float = 300,
        max_cache_entries: int = 128,
        max_cache_bytes: int = 32 * 1024 * 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            type(cache_ttl_seconds) not in (int, float)
            or not math.isfinite(cache_ttl_seconds)
            or not 0 < cache_ttl_seconds <= 3600
            or type(max_cache_entries) is not int
            or not 1 <= max_cache_entries <= 512
            or type(max_cache_bytes) is not int
            or not 1 <= max_cache_bytes <= 128 * 1024 * 1024
        ):
            raise InvalidInputError("Model catalogue cache bounds are invalid.")
        self.database = database
        self.policy = policy
        self.credentials = credentials
        self._rules = {(rule.provider, rule.model): rule for rule in policy.rules}
        self._ttl = cache_ttl_seconds
        self._max_entries = max_cache_entries
        self._max_bytes = max_cache_bytes
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple[UUID, str], _CachedCatalog] = OrderedDict()
        self._cache_bytes = 0

    def _validate(self, context: WorkspaceContext) -> None:
        # Database.transaction checks both the restricted login binding and
        # current workspace membership even when all results are cached.
        with self.database.transaction(context):
            pass

    def _prune(self) -> None:
        now = self._clock()
        for key in [key for key, value in self._cache.items() if value.expires_at <= now]:
            self._cache_bytes -= self._cache.pop(key).size_bytes

    def invalidate_provider(self, provider: str) -> None:
        """Discard local provider metadata after an OS-vault key change."""
        provider_name(provider)
        with self._lock:
            for key in [key for key in self._cache if key[1] == provider]:
                self._cache_bytes -= self._cache.pop(key).size_bytes

    def _snapshot(self, workspace_id: UUID) -> dict[str, _CachedCatalog]:
        with self._lock:
            self._prune()
            result = {
                provider: value
                for (workspace, provider), value in self._cache.items()
                if workspace == workspace_id
            }
            for provider in result:
                self._cache.move_to_end((workspace_id, provider))
            return result

    def _save(
        self,
        workspace_id: UUID,
        provider: str,
        models: tuple[CatalogModel, ...],
        discovered_at: str,
        size_bytes: int,
    ) -> bool:
        with self._lock:
            self._prune()
            key = (workspace_id, provider)
            previous = self._cache.pop(key, None)
            if previous is not None:
                self._cache_bytes -= previous.size_bytes
            if size_bytes > self._max_bytes:
                # Discovery still returns its complete result, without keeping
                # a partial catalogue or exceeding the memory cache budget.
                return False
            while self._cache and (
                len(self._cache) >= self._max_entries
                or self._cache_bytes + size_bytes > self._max_bytes
            ):
                _, oldest = self._cache.popitem(last=False)
                self._cache_bytes -= oldest.size_bytes
            self._cache[key] = _CachedCatalog(
                models, discovered_at, self._clock() + self._ttl, size_bytes
            )
            self._cache_bytes += size_bytes
            return True

    def _model_view(self, model: CatalogModel, *, configured: bool, source: str) -> dict[str, Any]:
        value = model.to_dict()
        rule = self._rules.get((model.provider, model.model)) or self._rules.get(
            (model.provider, "*")
        )
        eligible = model.text_generation is not False and not is_known_non_text_model(model.model)
        protocol: str | None = None
        if eligible:
            protocol = ProviderSelection(
                model.provider, model.model, rule.protocol if rule else None
            ).protocol
        value.update(
            configured=configured,
            policy_allowed=rule is not None,
            roles=list(rule.roles) if rule else [],
            protocol=protocol,
            generation_enabled=bool(
                configured and self.policy.allow_prompt_disclosure and rule and eligible
            ),
            source=source,
        )
        if not eligible:
            value["text_generation"] = False
            value["capability_verification_required"] = False
        return value

    def catalog(self, context: WorkspaceContext) -> dict[str, Any]:
        """Return static profiles, operator selections and cached metadata; no HTTP."""
        self._validate(context)
        snapshot = self._snapshot(context.workspace_id)
        providers: list[dict[str, Any]] = []
        models: list[dict[str, Any]] = []
        for provider in PROVIDERS:
            configured = self.credentials.configured(provider)
            rules = [rule for rule in self.policy.rules if rule.provider == provider]
            cached = snapshot.get(provider)
            providers.append(
                {
                    "id": provider,
                    "label": _LABELS[provider],
                    "configured": configured,
                    "disclosure_allowed": self.policy.allow_prompt_disclosure,
                    "generation_enabled": bool(
                        configured and self.policy.allow_prompt_disclosure and rules
                    ),
                    "rule_count": len(rules),
                    "catalog_cached": cached is not None,
                    "catalog_model_count": len(cached.models) if cached else 0,
                    "discovered_at": cached.discovered_at if cached else None,
                    "discovery_available": configured,
                    "protocol_selection": "model_family" if provider == "evolink" else "provider",
                }
            )
            records = {model.model: model for model in cached.models} if cached else {}
            discovered = set(records)
            for rule in rules:
                if rule.model != "*" and rule.model not in records:
                    non_text = is_known_non_text_model(rule.model)
                    records[rule.model] = CatalogModel(
                        provider,
                        rule.model,
                        rule.model,
                        False if non_text else None,
                        ()
                        if non_text
                        else (rule.protocol or default_protocol(provider, rule.model),),
                    )
            models.extend(
                self._model_view(
                    model,
                    configured=configured,
                    source="provider_catalog" if model.model in discovered else "operator_policy",
                )
                for model in sorted(records.values(), key=lambda model: model.model)
            )
        return {
            "local_models": [profile.to_dict() for profile in default_model_profiles()],
            "providers": providers,
            "models": models,
        }

    def discover(self, context: WorkspaceContext, provider: str) -> dict[str, Any]:
        """Refresh one provider's complete bounded catalogue without generating text."""
        self._validate(context)
        provider_name(provider)
        expected_ref = "secret://providers/" + provider
        used_secrets: list[str] = []

        def resolve(reference: str) -> str:
            if reference != expected_ref:
                raise ScopeDeniedError("The catalogue credential is unavailable.")
            secret = self.credentials.resolve(reference)
            used_secrets.append(secret)
            return secret

        client = HostedProviderClient(resolve, authorize=self.policy.authorize)
        try:
            models = tuple(client.list_models(provider))
        finally:
            client.close()
        self._validate(context)
        public_models = [model.to_dict() for model in models]
        if any(model.provider != provider for model in models) or (
            CaptureRedactor(tuple(used_secrets)).redact_metadata(public_models) != public_models
        ):
            raise ProviderAPIError("invalid_model_catalog", dispatched=True)
        serialized = canonical_json(public_models).encode()
        discovered_at = datetime.now(UTC).isoformat()
        cached = self._save(context.workspace_id, provider, models, discovered_at, len(serialized))
        return {
            "provider": provider,
            "models": [
                self._model_view(model, configured=True, source="provider_catalog")
                for model in models
            ],
            "qualified": False,
            "cached": cached,
            "discovered_at": discovered_at,
        }
