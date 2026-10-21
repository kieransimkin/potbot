"""Immutable deployment identity separated from mutable operational health."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.persistence.database import Database

DEFAULT_MODELS: tuple[tuple[str, str], ...] = (
    ("microsoft/Phi-4-mini-instruct", "reasoning"),
    ("google/gemma-3-4b-it", "reasoning"),
    ("Qwen/Qwen2.5-Coder-7B-Instruct", "coding"),
    ("google/codegemma-7b-it", "coding"),
    ("deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct", "coding"),
)


@dataclass(frozen=True, slots=True)
class DeploymentSpec:
    canonical_model: str
    weights_digest: str
    backend: str
    backend_version: str
    quantization: str
    tokenizer_revision: str
    chat_template_digest: str
    output_adapter: str
    tool_protocol: str
    usable_context_tokens: int
    max_output_tokens: int
    prompt_versions: tuple[tuple[str, str], ...]
    roles: tuple[str, ...]
    response_schemas: tuple[str, ...]
    resource_profile_json: str = (
        '{"memory_bytes":0,"load_ms":0,"device":"unspecified","measurement":"unknown"}'
    )
    cancellation: str = "unsupported"
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or type(self.schema_version) is not int:
            raise InvalidInputError("unsupported deployment schema")
        for name in (
            "canonical_model",
            "backend",
            "backend_version",
            "quantization",
            "tokenizer_revision",
            "output_adapter",
            "tool_protocol",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise InvalidInputError(f"{name} is required for an exact deployment")
        for name in ("weights_digest", "chat_template_digest"):
            if not isinstance(getattr(self, name), str) or not re.fullmatch(
                r"[0-9a-f]{64}", getattr(self, name)
            ):
                raise InvalidInputError(f"{name} must be a SHA256 digest")
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", self.tokenizer_revision):
            raise InvalidInputError("tokenizer revision must be an immutable commit or digest")
        if (
            any(
                type(v) is not int or v <= 0
                for v in (self.usable_context_tokens, self.max_output_tokens)
            )
            or self.max_output_tokens > self.usable_context_tokens
        ):
            raise InvalidInputError("deployment token limits are inconsistent")
        if (
            not isinstance(self.roles, tuple)
            or not self.roles
            or any(not isinstance(role, str) for role in self.roles)
            or set(self.roles) - {"reasoning", "coding"}
            or len(set(self.roles)) != len(self.roles)
        ):
            raise InvalidInputError("deployment roles must be explicit and unique")
        if (
            not isinstance(self.response_schemas, tuple)
            or not self.response_schemas
            or any(not isinstance(v, str) or not v.strip() for v in self.response_schemas)
        ):
            raise InvalidInputError("deployment response schemas are required")
        if (
            not isinstance(self.prompt_versions, tuple)
            or not self.prompt_versions
            or any(
                not isinstance(v, tuple)
                or len(v) != 2
                or any(not isinstance(x, str) or not x.strip() for x in v)
                for v in self.prompt_versions
            )
            or len(dict(self.prompt_versions)) != len(self.prompt_versions)
        ):
            raise InvalidInputError("prompt versions are required")
        if self.cancellation not in {"unsupported", "cooperative", "process_termination"}:
            raise InvalidInputError("unknown cancellation behavior")
        try:
            resource = json.loads(self.resource_profile_json)
        except (TypeError, ValueError) as error:
            raise InvalidInputError("invalid resource profile") from error
        if (
            not isinstance(resource, dict)
            or set(resource) != {"memory_bytes", "load_ms", "device", "measurement"}
            or any(
                type(resource[v]) is not int or resource[v] < 0 for v in ("memory_bytes", "load_ms")
            )
            or resource["measurement"] not in {"unknown", "estimated", "measured"}
            or not isinstance(resource["device"], str)
        ):
            raise InvalidInputError("invalid resource profile")
        object.__setattr__(self, "resource_profile_json", canonical_json(resource))
        object.__setattr__(self, "prompt_versions", tuple(sorted(self.prompt_versions)))
        object.__setattr__(self, "roles", tuple(sorted(self.roles)))
        object.__setattr__(self, "response_schemas", tuple(sorted(self.response_schemas)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "canonical_model": self.canonical_model,
            "weights_digest": self.weights_digest,
            "backend": self.backend,
            "backend_version": self.backend_version,
            "quantization": self.quantization,
            "tokenizer_revision": self.tokenizer_revision,
            "chat_template_digest": self.chat_template_digest,
            "output_adapter": self.output_adapter,
            "tool_protocol": self.tool_protocol,
            "usable_context_tokens": self.usable_context_tokens,
            "max_output_tokens": self.max_output_tokens,
            "prompt_versions": dict(self.prompt_versions),
            "roles": list(self.roles),
            "response_schemas": list(self.response_schemas),
            "resource_profile": json.loads(self.resource_profile_json),
            "cancellation": self.cancellation,
        }

    @property
    def fingerprint(self) -> str:
        return content_hash(self.to_dict())


class ModelRegistry:
    def __init__(self, database: Database) -> None:
        self.database = database

    def seed_defaults(self, context: WorkspaceContext) -> list[dict[str, Any]]:
        with self.database.transaction(context) as conn:
            for canonical, role in DEFAULT_MODELS:
                conn.execute(
                    "INSERT INTO model_catalog (workspace_id,id,canonical_model,role) VALUES (%s,%s,%s,%s) ON CONFLICT (workspace_id,canonical_model) DO NOTHING",
                    (context.workspace_id, uuid5(NAMESPACE_URL, canonical), canonical, role),
                )
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM model_catalog WHERE workspace_id=%s ORDER BY canonical_model",
                    (context.workspace_id,),
                ).fetchall()
            ]

    def register(self, context: WorkspaceContext, spec: DeploymentSpec) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        with self.database.transaction(context) as conn:
            conn.execute(
                "INSERT INTO model_deployments (workspace_id,id,canonical_model,content_hash,content) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (workspace_id,content_hash) DO NOTHING",
                (
                    context.workspace_id,
                    uuid4(),
                    spec.canonical_model,
                    spec.fingerprint,
                    Jsonb(spec.to_dict()),
                ),
            )
            row = conn.execute(
                "SELECT * FROM model_deployments WHERE workspace_id=%s AND content_hash=%s",
                (context.workspace_id, spec.fingerprint),
            ).fetchone()
            return dict(row)

    def inspect(self, context: WorkspaceContext, deployment_id: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            row = conn.execute(
                "SELECT d.*,COALESCE(h.health,'unknown') AS health,COALESCE(q.qualified,false) AS qualified FROM model_deployments d LEFT JOIN deployment_health h ON (h.workspace_id,h.deployment_id)=(d.workspace_id,d.id) LEFT JOIN deployment_qualifications q ON (q.workspace_id,q.deployment_id)=(d.workspace_id,d.id) WHERE d.workspace_id=%s AND d.id=%s",
                (context.workspace_id, deployment_id),
            ).fetchone()
            if row is None:
                raise ScopeDeniedError("record is unavailable")
            return dict(row)

    def report_health(self, context: WorkspaceContext, deployment_id: UUID, health: str) -> None:
        if health not in {"unknown", "healthy", "unavailable", "degraded"}:
            raise InvalidInputError("unknown deployment health")
        with self.database.transaction(context) as conn:
            if (
                conn.execute(
                    "SELECT id FROM model_deployments WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, deployment_id),
                ).fetchone()
                is None
            ):
                raise ScopeDeniedError("record is unavailable")
            conn.execute(
                "INSERT INTO deployment_health (workspace_id,id,deployment_id,health) VALUES (%s,%s,%s,%s) ON CONFLICT (workspace_id,deployment_id) DO UPDATE SET health=EXCLUDED.health,updated_at=clock_timestamp()",
                (context.workspace_id, uuid4(), deployment_id, health),
            )
