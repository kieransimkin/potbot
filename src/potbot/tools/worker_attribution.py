"""Host-trusted attribution of sandbox work to registered agents or principals.

A WorkspaceContext principal is a human/service actor, not necessarily a Cog
agent. Never turn principal attribution into an agent heat bar. Model-authored
work requests cannot select an agent UUID. A host-bound registry/agent pair
must be installed on the controller before a worker may claim agent identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError

if TYPE_CHECKING:
    from potbot.tools.agent_homes import AgentHomeRegistry
    from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger


@dataclass(frozen=True, slots=True)
class WorkerSubject:
    subject_id: UUID
    identity_kind: str  # 'agent' only for a matching host registry; otherwise 'principal'


def validate_host_agent_binding(*, shared: bool, registry: AgentHomeRegistry | None,
                                agent_id: UUID | None, ledger: WorkspaceLeaseLedger | None) -> None:
    if (registry is None) != (agent_id is None):
        raise InvalidInputError("an agent identity requires its host-owned registry")
    if agent_id is not None and not isinstance(agent_id, UUID):
        raise InvalidInputError("host-bound agent identity must be UUID")
    if agent_id is not None and not shared:
        raise ScopeDeniedError("registered agent attribution requires shared sandbox")
    if registry is not None and (getattr(registry, "ledger", None) is not ledger or
                                 not callable(getattr(registry, "lookup", None))):
        raise ScopeDeniedError("agent registry does not match admitted workspace ledger")


def worker_subject(context: WorkspaceContext, *, registry: AgentHomeRegistry | None,
                   agent_id: UUID | None, ledger: WorkspaceLeaseLedger) -> WorkerSubject:
    """Resolve the agent against the pinned workspace after launch admission.

    The gateway binds `agent_id` in its trusted constructor, never by the Cog
    arguments. The caller's principal remains available for audit when a
    registration is not supplied, but cannot masquerade as a Cog agent.
    """
    validate_host_agent_binding(shared=True, registry=registry, agent_id=agent_id, ledger=ledger)
    if registry is None:
        return WorkerSubject(context.principal_id, "principal")
    assert agent_id is not None
    home = registry.lookup(context.workspace_id, agent_id)
    if (home is None or home.agent_id != agent_id
            or home.workspace_id != context.workspace_id):
        raise ScopeDeniedError("agent home registration was absent or changed")
    return WorkerSubject(agent_id, "agent")
