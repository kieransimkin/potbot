"""Host-side context refresh admission and coherent next-launch snapshots.

The filesystem projections are immutable; a refresh replaces operator runtime
references only after all existing workers have left and an exclusive workspace
lease fences new admissions. Previously mounted inodes are never modified.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator
from uuid import UUID, uuid4

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError, StateMismatchError

if TYPE_CHECKING:
    from potbot.context.projection import AgentContextProjection, ContextProjectionManager
    from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger


@contextmanager
def exclusive_context_refresh(
    ledger: WorkspaceLeaseLedger, principal: WorkspaceContext,
) -> Iterator[None]:
    """Acquire a workspace-wide write claim, fencing cross-process tool claims.

    Caller also needs an in-process supervisor gate, because the guest dispatch
    boundary is wider than just one local lease acquisition.
    """
    from potbot.tools.workspace_concurrency import AccessRequest, AccessResource

    if not isinstance(principal, WorkspaceContext):
        raise ScopeDeniedError("Refresh requires an authenticated operator principal")
    pin = ledger.image_status(principal.workspace_id)
    if pin is None or not isinstance(pin.get("digest"), str):
        raise StateMismatchError("Cannot refresh an unpinned workspace")
    request = AccessRequest(
        principal.workspace_id, principal.principal_id,
        "context.refresh", uuid4().hex, "write",
        (AccessResource("workspace", "*", "write"),), pin["digest"],
    )
    lease = ledger.claim(request, ttl_seconds=300, replay_existing=False)
    uncertain = True
    receipt = hashlib.sha256(b"context-refresh-indeterminate").hexdigest()
    try:
        yield
        receipt = hashlib.sha256(b"context-refresh-released").hexdigest()
        uncertain = False
    finally:
        ledger.finish(lease, result_sha256=receipt, uncertain=uncertain)


def collect_agent_projections(
    manager: ContextProjectionManager, ledger: WorkspaceLeaseLedger,
    workspace_id: UUID,
) -> tuple[AgentContextProjection, ...]:
    """Select only operator-registered agents, not arbitrary /home entries."""
    from potbot.context.projection import AgentContextProjection

    if manager.principal.workspace_id != workspace_id:
        raise ScopeDeniedError("Agent projections are outside the operator workspace")
    with ledger._connect() as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_homes'"
        ).fetchone()
        rows = (
            db.execute(
                "SELECT agent_id,directory FROM agent_homes "
                "WHERE workspace_id=? ORDER BY agent_id", (str(workspace_id),)
            ).fetchall() if exists is not None else ()
        )
    result: list[AgentContextProjection] = []
    for row in rows:
        agent_id = UUID(row["agent_id"])
        snapshot = manager.capture_object("agent", str(agent_id))
        binding = AgentContextProjection(agent_id, row["directory"], snapshot)
        # Fail closed before publishing the new launch configuration.
        binding.inspect(home=ledger.home)
        result.append(binding)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class RefreshReceipt:
    workspace_id: UUID
    root_revisions: tuple[int, int, int]
    agent_count: int
    snapshot: str = "next-launch-only"


def context_is_stale(
    manager: ContextProjectionManager, runtime: object,
    ledger: WorkspaceLeaseLedger, *, include_agents: bool,
) -> bool:
    """Check root and every pinned agent against DB state without altering mounts.

    Missing registered-agent bindings also trigger an admitted reconciliation.
    Every read is performed with the operator's authenticated workspace principal.
    """
    from potbot.context.inspection import ContextHistoryInspector

    inspector = ContextHistoryInspector(manager.service)
    root = getattr(runtime, "context_projection", None)
    if root is None:
        raise StateMismatchError("Operator context projection is not initialized")
    principal = manager.principal
    if inspector.freshness(principal, "sandbox", str(principal.workspace_id), root).stale:
        return True
    if not include_agents:
        return False
    agents = getattr(runtime, "agent_context_projections", ())
    with ledger._connect() as db:
        present = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_homes'"
        ).fetchone()
        registered = (
            db.execute("SELECT agent_id FROM agent_homes WHERE workspace_id=?",
                       (str(principal.workspace_id),)).fetchall()
            if present is not None else ()
        )
    if len(registered) > 256:
        raise StateMismatchError("Context refresh agent inventory exceeds limit")
    if {row["agent_id"] for row in registered} != {str(binding.agent_id) for binding in agents}:
        return True
    for binding in agents:
        if inspector.freshness(
            principal, "agent", str(binding.agent_id), binding.projection,
        ).stale:
            return True
    return False
