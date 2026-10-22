"""Operator-owned disclosure and rate ceilings for paid hosted inference.

This policy contains no credentials. Provider endpoints are fixed by the adapter;
job payloads cannot supply an endpoint, credential reference or pricing rule.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import content_hash
from potbot.models.providers import ProviderRequest, ProviderSelection, ProviderUsage
from potbot.models.providers.contracts import ALLOWED_PROTOCOLS, PROVIDERS, model_name


def _integer(value: object, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvalidInputError(f"{name} is outside its supported bound.")
    return value


@dataclass(frozen=True, slots=True)
class RateRule:
    provider: str
    model: str
    input_microunits_per_million: int
    output_microunits_per_million: int
    roles: tuple[str, ...] = ("reasoning", "coding")
    protocol: str | None = None

    @classmethod
    def from_dict(cls, value: object) -> RateRule:
        required = {
            "provider",
            "model",
            "input_microunits_per_million",
            "output_microunits_per_million",
        }
        if (
            not isinstance(value, dict)
            or not required <= value.keys()
            or set(value) - required - {"roles", "protocol"}
        ):
            raise InvalidInputError("Provider rate rule has unsupported fields.")
        provider = value["provider"]
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise InvalidInputError("Provider rate rule identifies an unknown provider.")
        model = value["model"]
        if model != "*":
            model = model_name(model)
        roles = value.get("roles", ["reasoning", "coding"])
        if (
            not isinstance(roles, list)
            or not roles
            or any(
                not isinstance(role, str) or role not in {"reasoning", "coding"} for role in roles
            )
            or len(set(roles)) != len(roles)
        ):
            raise InvalidInputError("Provider roles must be distinct reasoning/coding identities.")
        protocol = value.get("protocol")
        if protocol is not None and (
            not isinstance(protocol, str) or protocol not in ALLOWED_PROTOCOLS[provider]
        ):
            raise InvalidInputError("The provider rate rule selects an unsupported protocol.")
        return cls(
            provider,
            model,
            _integer(value["input_microunits_per_million"], "Input rate", 0, 10**12),
            _integer(value["output_microunits_per_million"], "Output rate", 0, 10**12),
            tuple(roles),
            protocol,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "input_microunits_per_million": self.input_microunits_per_million,
            "output_microunits_per_million": self.output_microunits_per_million,
            "roles": list(self.roles),
            "protocol": self.protocol,
        }

    def charge(
        self, input_tokens: int, output_tokens: int, *, total_tokens: int | None = None
    ) -> int:
        # Unclassified provider-reported tokens use the more expensive ceiling.
        # Cache discounts are deliberately not assumed; operators must configure
        # rates covering any cache-write premiums and other per-token charges.
        extra = max(0, (total_tokens or 0) - input_tokens - output_tokens)
        numerator = (
            input_tokens * self.input_microunits_per_million
            + output_tokens * self.output_microunits_per_million
            + extra * max(self.input_microunits_per_million, self.output_microunits_per_million)
        )
        return _integer((numerator + 999999) // 1000000, "Calculated cost", 0, 2**53 - 1)


@dataclass(frozen=True, slots=True)
class HostedPolicy:
    allow_prompt_disclosure: bool = False
    rules: tuple[RateRule, ...] = ()
    max_prompt_bytes: int = 262144
    max_output_tokens: int = 8192
    max_timeout_seconds: int = 300
    max_cost_microunits: int = 1000000

    @classmethod
    def from_dict(cls, value: object) -> HostedPolicy:
        allowed = {
            "schema_version",
            "allow_prompt_disclosure",
            "rules",
            "max_prompt_bytes",
            "max_output_tokens",
            "max_timeout_seconds",
            "max_cost_microunits",
        }
        if (
            not isinstance(value, dict)
            or set(value) != allowed
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
        ):
            raise InvalidInputError("Hosted policy fields do not match schema version 1.")
        if type(value["allow_prompt_disclosure"]) is not bool:
            raise InvalidInputError("Disclosure permission must be explicit.")
        raw_rules = value["rules"]
        if not isinstance(raw_rules, list) or len(raw_rules) > 10000:
            raise InvalidInputError("Hosted policy requires a bounded rule list.")
        rules = tuple(RateRule.from_dict(rule) for rule in raw_rules)
        if len({(r.provider, r.model) for r in rules}) != len(rules):
            raise InvalidInputError("Provider/model rules must be distinct.")
        return cls(
            value["allow_prompt_disclosure"],
            rules,
            _integer(value["max_prompt_bytes"], "Prompt bytes", 1, 1048576),
            _integer(value["max_output_tokens"], "Output tokens", 1, 1048576),
            _integer(value["max_timeout_seconds"], "Timeout", 1, 3600),
            _integer(value["max_cost_microunits"], "Cost allowance", 0, 2**53 - 1),
        )

    @classmethod
    def read(cls, path: Path) -> HostedPolicy:
        try:
            with path.open("rb") as stream:
                raw = stream.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError
            return cls.from_dict(json.loads(raw))
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            raise InvalidInputError(
                "The hosted provider policy is unreadable or invalid."
            ) from None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "allow_prompt_disclosure": self.allow_prompt_disclosure,
            "rules": [rule.to_dict() for rule in self.rules],
            "max_prompt_bytes": self.max_prompt_bytes,
            "max_output_tokens": self.max_output_tokens,
            "max_timeout_seconds": self.max_timeout_seconds,
            "max_cost_microunits": self.max_cost_microunits,
        }

    @property
    def fingerprint(self) -> str:
        return content_hash(self.to_dict())

    def rule(self, provider: str, model: str, role: str | None = None) -> RateRule:
        matches = [r for r in self.rules if r.provider == provider and r.model == model]
        if not matches:
            matches = [r for r in self.rules if r.provider == provider and r.model == "*"]
        if not matches or (role is not None and role not in matches[0].roles):
            raise ScopeDeniedError(
                "This provider/model/role is not enabled by the operator policy."
            )
        return matches[0]

    def authorize(self, request: ProviderRequest) -> None:
        if not self.allow_prompt_disclosure:
            raise ScopeDeniedError("Hosted prompt disclosure is disabled by the operator policy.")
        rule = self.rule(request.selection.provider, request.selection.model)
        expected = ProviderSelection(
            request.selection.provider, request.selection.model, rule.protocol
        )
        if request.selection.protocol != expected.protocol:
            raise ScopeDeniedError("The hosted protocol does not match the operator policy.")
        if (
            len((request.prompt + request.system).encode()) > self.max_prompt_bytes
            or request.max_output_tokens > self.max_output_tokens
            or request.timeout_seconds > self.max_timeout_seconds
        ):
            raise ScopeDeniedError("The hosted request exceeds the operator policy limits.")

    def request(
        self, selection: dict[str, str], value: dict[str, Any]
    ) -> tuple[ProviderRequest, int, RateRule]:
        if (
            not isinstance(selection, dict)
            or set(selection) != {"provider", "model", "role"}
            or any(not isinstance(item, str) for item in selection.values())
            or not isinstance(value, dict)
        ):
            raise InvalidInputError(
                "Hosted generation requires a typed selection and input object."
            )
        fields = {
            "prompt",
            "system",
            "max_output_tokens",
            "timeout_seconds",
            "temperature",
            "top_p",
            "seed",
            "input_token_allowance",
        }
        if set(value) - fields or "prompt" not in value:
            raise InvalidInputError("Hosted generation input has unsupported fields.")
        rule = self.rule(selection["provider"], selection["model"], selection["role"])
        request = ProviderRequest(
            ProviderSelection(selection["provider"], selection["model"], rule.protocol),
            prompt=value["prompt"],
            system=value.get("system", ""),
            max_output_tokens=value.get("max_output_tokens", 2048),
            timeout_seconds=value.get("timeout_seconds", 60),
            temperature=value.get("temperature"),
            top_p=value.get("top_p"),
            seed=value.get("seed"),
        )
        self.authorize(request)
        # Provider tokenizers and hidden template overhead are unavailable here.
        # This conservative byte-based admission estimate is labelled as such;
        # reported overruns remain charged and exhaust the task when necessary.
        estimate = len((request.prompt + request.system).encode()) + 1024
        allowance = _integer(
            value.get("input_token_allowance", estimate),
            "Input token allowance",
            estimate,
            16 * 1024 * 1024,
        )
        if rule.charge(allowance, request.max_output_tokens) > self.max_cost_microunits:
            raise ScopeDeniedError(
                "The hosted request exceeds the configured per-call cost ceiling."
            )
        return request, allowance, rule

    @staticmethod
    def usage_amounts(usage: ProviderUsage, rule: RateRule, elapsed_ms: int) -> dict[str, int]:
        return {
            "calls": 1,
            "tokens": usage.total_tokens,
            "inference_ms": elapsed_ms,
            "money_microunits": rule.charge(
                usage.input_tokens, usage.output_tokens, total_tokens=usage.total_tokens
            ),
        }
