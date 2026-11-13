"""Operator publication and controller readback of installed Cog permissions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from potbot.controller.cog_capability_policy import (
    Capability,
    CogCapabilityGrant,
    CogCapabilityPolicy,
)
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, RevisionConflictError, StateMismatchError
from potbot.domain.serialization import canonical_json
from potbot.persistence.database import admin_connection


def parse_grants(value: object) -> tuple[CogCapabilityGrant, ...]:
    if not isinstance(value, list) or len(value) > 128:
        raise InvalidInputError("Supply a JSON array of up to 128 installed Cog grants")
    grants = []
    for row in value:
        if not isinstance(row, dict) or set(row) != {
            "installation_sha256",
            "capabilities",
            "expires_at",
        }:
            raise InvalidInputError("Cog grant requires an installation, capabilities and expiry")
        labels = row["capabilities"]
        if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
            raise InvalidInputError("Supply an array of explicit Cog capability names")
        try:
            capabilities = frozenset(Capability(label) for label in labels)
        except ValueError:
            raise InvalidInputError("Unknown Cog capability name") from None
        if len(capabilities) != len(labels):
            raise InvalidInputError("Duplicate Cog capability name")
        grants.append(
            CogCapabilityGrant(row["installation_sha256"], capabilities, row["expires_at"])
        )
    return tuple(grants)


def _grant_document(policy: CogCapabilityPolicy) -> list[dict[str, Any]]:
    return [
        {
            "installation_sha256": grant.installation_sha256,
            "capabilities": sorted(cap.value for cap in grant.capabilities),
            "expires_at": grant.expires_at,
        }
        for grant in sorted(policy.grants, key=lambda grant: grant.installation_sha256)
    ]


@dataclass(frozen=True, slots=True)
class PublishedCogPolicy:
    policy: CogCapabilityPolicy
    generation: int
    published_by: str
    published_at: datetime

    def to_dict(self) -> dict[str, Any]:
        policy = self.policy
        return {
            "workspace_id": str(policy.actor.workspace_id),
            "principal_id": str(policy.actor.principal_id),
            "agent_id": str(policy.agent_id),
            "node_sha256": policy.node_sha256,
            "revision_sha256": policy.revision_sha256,
            "policy_sha256": policy.fingerprint,
            "generation": self.generation,
            "published_by": self.published_by,
            "published_at": self.published_at.isoformat(),
            "grants": _grant_document(policy),
        }


def _lock_scope(connection: Any, actor: WorkspaceContext, agent: UUID, *, shared: bool) -> None:
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    connection.execute(
        f"SELECT pg_catalog.{function}(pg_catalog.hashtextextended(pg_catalog.current_schema() "
        "|| %s,0))",
        (f":potbot:cog-grant:{actor.workspace_id}:{actor.principal_id}:{agent}",),
    )


def current_cog_policy(
    connection: Any, actor: WorkspaceContext, agent: UUID, *, for_dispatch: bool = False
) -> PublishedCogPolicy | None:
    if for_dispatch:
        _lock_scope(connection, actor, agent, shared=True)
    row = connection.execute(
        "SELECT generation,node_sha256,revision_sha256,policy_sha256,grants,"
        "published_by,published_at FROM cog_grant_revisions "
        "WHERE workspace_id=%s AND principal_id=%s AND agent_id=%s "
        "ORDER BY generation DESC LIMIT 1",
        (actor.workspace_id, actor.principal_id, agent),
    ).fetchone()
    if row is None:
        return None
    policy = CogCapabilityPolicy(
        actor, agent, row["node_sha256"], row["revision_sha256"], parse_grants(row["grants"])
    )
    if policy.fingerprint != row["policy_sha256"]:
        raise StateMismatchError("Stored Cog grant policy failed its fingerprint check")
    return PublishedCogPolicy(policy, row["generation"], row["published_by"], row["published_at"])


def publish_cog_policy(
    admin_dsn: str, schema: str, policy: CogCapabilityPolicy, expected_generation: int
) -> PublishedCogPolicy:
    """Publish a complete replacement, keeping every earlier decision as evidence."""
    if type(expected_generation) is not int or not 0 <= expected_generation < 2**63 - 1:
        raise InvalidInputError("Supply a valid expected Cog grant generation")
    actor = policy.actor
    scope = (actor.workspace_id, actor.principal_id, policy.agent_id)
    with admin_connection(admin_dsn, schema) as connection:
        # Serialize initial and subsequent publications to the same Agent scope.
        _lock_scope(connection, actor, policy.agent_id, shared=False)
        member = connection.execute(
            "SELECT 1 FROM workspace_memberships WHERE workspace_id=%s AND principal_id=%s",
            scope[:2],
        ).fetchone()
        if member is None:
            raise InvalidInputError("Cog grant requires a principal bound to the workspace")
        current = current_cog_policy(connection, actor, policy.agent_id)
        generation = current.generation if current is not None else 0
        fingerprint = policy.fingerprint
        if (
            current is not None
            and generation == expected_generation + 1
            and current.policy.fingerprint == fingerprint
        ):
            return current
        if generation != expected_generation:
            raise RevisionConflictError("Cog grant generation changed. Read it before publishing.")
        saved = connection.execute(
            "INSERT INTO cog_grant_revisions(workspace_id,principal_id,agent_id,generation,"
            "node_sha256,revision_sha256,policy_sha256,grants) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb) "
            "RETURNING published_by,published_at",
            (
                *scope,
                generation + 1,
                policy.node_sha256,
                policy.revision_sha256,
                fingerprint,
                canonical_json(_grant_document(policy)),
            ),
        ).fetchone()
        assert saved is not None
        return PublishedCogPolicy(
            policy, generation + 1, saved["published_by"], saved["published_at"]
        )
