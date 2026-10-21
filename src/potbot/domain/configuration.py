"""Validated configuration snapshots; credential values are never accepted."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json, content_hash

LAYERS = ("installation", "deployment", "workspace", "repository", "task")
DEFAULTS: dict[str, object] = {
    "policy_versions": {
        "search": "bounded-depth-first-v1",
        "reuse": "reuse-discovery-v1",
        "verification": "v1",
    },
    "reasoning_models": ["microsoft/Phi-4-mini-instruct", "google/gemma-3-4b-it"],
    "coding_models": [
        "Qwen/Qwen2.5-Coder-7B-Instruct",
        "google/codegemma-7b-it",
        "deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct",
    ],
    "allowed_tools": [],
    "source_access": [],
    "physical_inference_concurrency": 1,
    "max_packet_tokens": 2048,
    "verification_policy": "v1",
    "embedding_profile": "qwen3-embedding-0.6b-1024-v1",
    "secret_refs": {},
}
_REF = re.compile(r"secret://[a-z][a-z0-9_-]*/[A-Za-z0-9_./-]+(?:#[A-Za-z0-9_.-]+)?\Z")
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z")


def validate_secret_refs(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise InvalidInputError("secret_refs must map names to secret:// references")
    for name, reference in value.items():
        if (
            not isinstance(name, str)
            or not _NAME.fullmatch(name)
            or not isinstance(reference, str)
            or not _REF.fullmatch(reference)
            or ".." in reference
        ):
            raise InvalidInputError("secret_refs must contain references, never credential values")
    return dict(value)


def _validate(settings: dict[str, Any]) -> None:
    if set(settings) != set(DEFAULTS):
        raise InvalidInputError("unknown or missing configuration setting")
    for name in ("physical_inference_concurrency", "max_packet_tokens"):
        if type(settings[name]) is not int or not 1 <= settings[name] <= 1_000_000:
            raise InvalidInputError(f"{name} must be a bounded positive integer")
    for name in ("reasoning_models", "coding_models", "allowed_tools", "source_access"):
        values = settings[name]
        if (
            not isinstance(values, list)
            or len(values) > 256
            or any(not isinstance(v, str) or not v.strip() for v in values)
            or len(set(values)) != len(values)
        ):
            raise InvalidInputError(f"{name} must contain distinct nonempty names")
    for name in ("verification_policy", "embedding_profile"):
        if not isinstance(settings[name], str) or not _NAME.fullmatch(settings[name]):
            raise InvalidInputError(f"{name} must be a versioned policy name")
    policies = settings["policy_versions"]
    if (
        not isinstance(policies, dict)
        or not {"search", "reuse", "verification"} <= set(policies)
        or any(
            not isinstance(k, str)
            or not _NAME.fullmatch(k)
            or not isinstance(v, str)
            or not _NAME.fullmatch(v)
            for k, v in policies.items()
        )
    ):
        raise InvalidInputError("policy_versions must identify all baseline policies")
    validate_secret_refs(settings["secret_refs"])


@dataclass(frozen=True, slots=True)
class ResolvedConfiguration:
    """Serialized content prevents callers mutating a nested accepted snapshot."""

    content_json: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise InvalidInputError("unsupported configuration schema")
        try:
            value = json.loads(self.content_json)
        except (TypeError, ValueError) as error:
            raise InvalidInputError("invalid configuration JSON") from error
        if not isinstance(value, dict):
            raise InvalidInputError("configuration must be an object")
        _validate(value)
        object.__setattr__(self, "content_json", canonical_json(value))

    @property
    def settings(self) -> dict[str, Any]:
        return dict(json.loads(self.content_json))

    @property
    def fingerprint(self) -> str:
        return content_hash({"schema_version": self.schema_version, "settings": self.settings})


def resolve_configuration(
    layers: Mapping[str, Mapping[str, object]] | None = None,
    *,
    forbidden_overrides: frozenset[str] = frozenset(),
) -> ResolvedConfiguration:
    """Merge known layers, from installation through task, then freeze the result.

    Map settings merge by key. Lists replace as a whole. A forbidden override
    names a top-level setting and locks its value after installation defaults.
    """
    layers = layers or {}
    if set(layers) - set(LAYERS) or forbidden_overrides - set(DEFAULTS):
        raise InvalidInputError("unknown configuration layer or protected setting")
    settings = dict(json.loads(canonical_json(DEFAULTS)))
    for layer in LAYERS:
        overrides = layers.get(layer, {})
        if not isinstance(overrides, Mapping) or set(overrides) - set(DEFAULTS):
            raise InvalidInputError("unknown configuration setting")
        for name, value in overrides.items():
            if layer != "installation" and name in forbidden_overrides and value != settings[name]:
                raise InvalidInputError("a protected configuration setting cannot be overridden")
            if name in {"policy_versions", "secret_refs"} and isinstance(value, Mapping):
                settings[name] = {**settings[name], **value}
            else:
                settings[name] = value
    return ResolvedConfiguration(canonical_json(settings))
