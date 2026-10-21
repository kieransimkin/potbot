"""Durable resolved configuration and concrete execution environment identities."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from potbot.domain.auth import WorkspaceContext
from potbot.domain.configuration import (
    ResolvedConfiguration,
    resolve_configuration,
    validate_secret_refs,
)
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash

__all__ = [
    "EnvironmentManifest",
    "resolve_configuration",
    "store_configuration",
    "store_environment",
]


def store_configuration(
    connection: Any,
    context: WorkspaceContext,
    config: ResolvedConfiguration,
    *,
    episode_id: UUID | None = None,
) -> dict[str, Any]:
    from psycopg.types.json import Jsonb

    row = connection.execute(
        "INSERT INTO resolved_configurations (workspace_id,id,episode_id,content_hash,content) VALUES (%s,%s,%s,%s,%s) RETURNING *",
        (context.workspace_id, uuid4(), episode_id, config.fingerprint, Jsonb(config.settings)),
    ).fetchone()
    return dict(row)


@dataclass(frozen=True, slots=True)
class EnvironmentManifest:
    configuration_id: UUID
    configuration_hash: str
    operating_system: str
    interpreter: str
    toolchains: tuple[tuple[str, str], ...]
    services: tuple[tuple[str, str], ...] = ()
    relevant_inputs: tuple[tuple[str, str], ...] = ()
    secret_refs: tuple[tuple[str, str], ...] = ()
    container_digest: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            not isinstance(self.configuration_id, UUID)
            or self.schema_version != 1
            or type(self.schema_version) is not int
        ):
            raise InvalidInputError("invalid environment identity")
        if not re.fullmatch(r"[0-9a-f]{64}", self.configuration_hash):
            raise InvalidInputError("configuration_hash must be a SHA256 digest")
        for name in ("operating_system", "interpreter"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise InvalidInputError("environment inputs must be explicit")
        for name in ("toolchains", "services", "relevant_inputs", "secret_refs"):
            values = getattr(self, name)
            if (
                not isinstance(values, tuple)
                or any(
                    not isinstance(v, tuple)
                    or len(v) != 2
                    or any(not isinstance(x, str) or not x.strip() for x in v)
                    for v in values
                )
                or len(dict(values)) != len(values)
            ):
                raise InvalidInputError("environment inputs require distinct named values")
            object.__setattr__(self, name, tuple(sorted(values)))
        validate_secret_refs(dict(self.secret_refs))
        if any(not re.fullmatch(r"[0-9a-f]{64}", v) for _, v in self.relevant_inputs):
            raise InvalidInputError(
                "relevant inputs must be content hashes, not raw environment values"
            )
        if self.container_digest is not None and not re.fullmatch(
            r"sha256:[0-9a-f]{64}", self.container_digest
        ):
            raise InvalidInputError("container identity must be a pinned sha256 digest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "configuration_id": str(self.configuration_id),
            "configuration_hash": self.configuration_hash,
            "operating_system": self.operating_system,
            "interpreter": self.interpreter,
            "toolchains": dict(self.toolchains),
            "services": dict(self.services),
            "relevant_inputs": dict(self.relevant_inputs),
            "secret_refs": dict(self.secret_refs),
            "container_digest": self.container_digest,
        }

    @property
    def fingerprint(self) -> str:
        return content_hash(self.to_dict())


def store_environment(
    connection: Any, context: WorkspaceContext, manifest: EnvironmentManifest
) -> dict[str, Any]:
    from psycopg.types.json import Jsonb

    config = connection.execute(
        "SELECT content_hash,content FROM resolved_configurations WHERE workspace_id=%s AND id=%s",
        (context.workspace_id, manifest.configuration_id),
    ).fetchone()
    if config is None:
        raise ScopeDeniedError("record is unavailable")
    if config["content_hash"] != manifest.configuration_hash or config["content"][
        "secret_refs"
    ] != dict(manifest.secret_refs):
        raise InvalidInputError("environment does not match its immutable configuration")
    connection.execute(
        "INSERT INTO environment_manifests (workspace_id,id,configuration_id,content_hash,content) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (workspace_id,content_hash) DO NOTHING",
        (
            context.workspace_id,
            uuid4(),
            manifest.configuration_id,
            manifest.fingerprint,
            Jsonb(manifest.to_dict()),
        ),
    )
    row = connection.execute(
        "SELECT * FROM environment_manifests WHERE workspace_id=%s AND content_hash=%s",
        (context.workspace_id, manifest.fingerprint),
    ).fetchone()
    return dict(row)


def read_environment(
    connection: Any, context: WorkspaceContext, environment_id: UUID
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM environment_manifests WHERE workspace_id=%s AND id=%s",
        (context.workspace_id, environment_id),
    ).fetchone()
    if row is None:
        raise ScopeDeniedError("record is unavailable")
    if content_hash(row["content"]) != row["content_hash"]:
        raise InvalidInputError("environment integrity check failed")
    return dict(json.loads(canonical_json(row["content"])))
