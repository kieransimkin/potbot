"""Operator policy for the installed Cog admission path."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from time import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import content_hash
from potbot.tools.execution_contracts import digest

if TYPE_CHECKING:
    from potbot.cogs.contracts import CogRunContext
    from potbot.tools.sandbox_cog_worker import SandboxedCogOperation


class Capability(StrEnum):
    READ = "read"
    EXTERNAL_DISCLOSURE = "external_disclosure"
    FILE_MUTATION = "file_mutation"
    NETWORK = "network"
    DB_WRITE = "db_write"
    CODE_EXECUTION = "code_execution"
    MONEY = "money"
    ADMIN = "admin"


class Denial(StrEnum):
    AGENT = "cog_policy_agent_mismatch"
    INSTALLATION = "cog_policy_installation_denied"
    REVISION = "cog_policy_revision_mismatch"
    EXPIRED = "cog_policy_grant_expired"
    TASK = "cog_policy_task_denied"
    PARENT = "cog_policy_parent_exceeded"


class CogPolicyDenied(ScopeDeniedError):
    def __init__(self, reason: Denial) -> None:
        self.reason = reason
        super().__init__(reason.value)


def installation_hash(operation: SandboxedCogOperation) -> str:
    return content_hash(
        {
            "binding": operation.binding.to_dict(),
            "argv": operation.argv,
            "executable": operation.executable_sha256,
            "protocol": operation.protocol,
        }
    )


@dataclass(frozen=True, slots=True)
class CogCapabilityGrant:
    installation_sha256: str
    capabilities: frozenset[Capability]
    expires_at: int

    def __post_init__(self) -> None:
        digest(self.installation_sha256)
        if (
            type(self.capabilities) is not frozenset
            or not self.capabilities
            or any(type(capability) is not Capability for capability in self.capabilities)
            or type(self.expires_at) is not int
            or not 0 < self.expires_at < 2**63
        ):
            raise InvalidInputError("Cog capability grant needs explicit permissions and expiry")


@dataclass(frozen=True, slots=True)
class CogCapabilityPolicy:
    """A trusted, in-process allowlist scoped to one native Syn Agent."""

    actor: WorkspaceContext
    agent_id: UUID
    node_sha256: str
    revision_sha256: str
    grants: tuple[CogCapabilityGrant, ...]

    def __post_init__(self) -> None:
        digest(self.node_sha256)
        digest(self.revision_sha256)
        if (
            type(self.actor) is not WorkspaceContext
            or not isinstance(self.agent_id, UUID)
            or type(self.grants) is not tuple
            or len(self.grants) > 128
            or any(type(grant) is not CogCapabilityGrant for grant in self.grants)
            or len({grant.installation_sha256 for grant in self.grants}) != len(self.grants)
        ):
            raise InvalidInputError("Cog policy requires one scoped grant per installation")

    @property
    def fingerprint(self) -> str:
        return content_hash(
            {
                "profile": "potbot.cog-capability-policy/1",
                "workspace_id": str(self.actor.workspace_id),
                "principal_id": str(self.actor.principal_id),
                "agent_id": str(self.agent_id),
                "node_sha256": self.node_sha256,
                "revision_sha256": self.revision_sha256,
                "grants": [
                    [
                        grant.installation_sha256,
                        sorted(capability.value for capability in grant.capabilities),
                        grant.expires_at,
                    ]
                    for grant in sorted(self.grants, key=lambda grant: grant.installation_sha256)
                ],
            }
        )

    def select(
        self,
        actor: WorkspaceContext,
        agent_id: UUID,
        native: Mapping[str, Any] | None,
        operation: SandboxedCogOperation,
        *,
        parent: CogCapabilityGrant | None,
    ) -> CogCapabilityGrant:
        if (
            actor != self.actor
            or agent_id != self.agent_id
            or native is None
            or native.get("profile") != "potbot.syn-cog-native-binding/1"
            or native.get("workspace_id") != str(actor.workspace_id)
            or native.get("agent_id") != str(agent_id)
            or native.get("node") != self.node_sha256
        ):
            raise CogPolicyDenied(Denial.AGENT)
        binding = operation.binding
        if binding.policy.policy_revision_hash != self.revision_sha256:
            raise CogPolicyDenied(Denial.REVISION)
        identity = installation_hash(operation)
        grant = next(
            (grant for grant in self.grants if grant.installation_sha256 == identity), None
        )
        required = {Capability.READ} if binding.effect_class == "read_only" else set()
        if binding.kind in {"api", "browser"}:
            required.update({Capability.NETWORK, Capability.EXTERNAL_DISCLOSURE})
        if binding.kind in {"script", "ci"}:
            required.add(Capability.CODE_EXECUTION)
        if binding.kind == "sql" and binding.effect_class != "read_only":
            required.add(Capability.DB_WRITE)
        if binding.effect_class == "destructive":
            required.add(Capability.ADMIN)
        if (
            grant is None
            or not required <= grant.capabilities
            or (
                binding.effect_class != "read_only"
                and not grant.capabilities.intersection(
                    {
                        Capability.FILE_MUTATION,
                        Capability.DB_WRITE,
                        Capability.EXTERNAL_DISCLOSURE,
                        Capability.CODE_EXECUTION,
                        Capability.MONEY,
                        Capability.ADMIN,
                    }
                )
            )
        ):
            raise CogPolicyDenied(Denial.INSTALLATION)
        if parent is not None and not grant.capabilities <= parent.capabilities:
            raise CogPolicyDenied(Denial.PARENT)
        if time() >= min(grant.expires_at, parent.expires_at if parent else grant.expires_at):
            raise CogPolicyDenied(Denial.EXPIRED)
        if parent is not None and parent.expires_at < grant.expires_at:
            return replace(grant, expires_at=parent.expires_at)
        return grant

    def require_active_task(
        self,
        connection: Any,
        context: CogRunContext,
        grant: CogCapabilityGrant,
    ) -> None:
        if time() >= grant.expires_at:
            raise CogPolicyDenied(Denial.EXPIRED)
        current = connection.execute(
            "SELECT c.content FROM contract_revisions c JOIN episodes e "
            "ON e.workspace_id=c.workspace_id AND e.id=c.episode_id "
            "JOIN repository_states r ON r.workspace_id=e.workspace_id "
            "AND r.repository_id=e.repository_id "
            "WHERE c.workspace_id=%s AND c.id=%s AND r.id=%s "
            "AND e.active_contract_id=c.id AND e.status='running'",
            (self.actor.workspace_id, context.task_revision_id, context.repository_state_id),
        ).fetchone()
        if current is None or not grant.capabilities <= set(current["content"]["capabilities"]):
            raise CogPolicyDenied(Denial.TASK)
