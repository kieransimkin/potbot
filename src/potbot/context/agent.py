"""Host-only agent context lifecycle and tightly scoped mutation admission.

PostgreSQL remains authoritative. The existing host workspace lease ledger is
used solely as a *temporary process admission/fencing mechanism*; it never
stores context documents, history, or credentials. This module is NOT an
untrusted guest RPC handler: callers must be authenticated by the operator.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.context.model import ROLES, validate_identity
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (InvalidInputError, ScopeDeniedError, StateMismatchError,
                                  RevisionConflictError)
from potbot.tools.workspace_concurrency import AccessRequest, AccessResource

if TYPE_CHECKING:
    from potbot.context.service import WorkspaceContextService
    from potbot.tools.agent_homes import AgentHomeRegistry, AgentHome
    from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger


@dataclass(frozen=True, slots=True)
class ContextIdentity:
    """Trusted host's already-authenticated workspace/agent binding."""

    principal: WorkspaceContext
    agent_id: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.principal, WorkspaceContext) or not isinstance(self.agent_id, UUID):
            raise InvalidInputError("Context identity needs authenticated workspace and agent UUIDs")


class AgentContextLifecycle:
    """Reconcile canonical agent-home registration and PostgreSQL context.

    The caller must already have admitted creation of the agent identity.
    Missing context is initialized exactly once; an existing document is never
    reset during restarts or home rename. Name always uses the immutable UUID.
    """

    def __init__(self, service: WorkspaceContextService, registry: AgentHomeRegistry):
        self.service, self.registry = service, registry

    def provision(self, identity: ContextIdentity, *, idempotency_key: str) -> AgentHome:
        home = self.registry.provision(identity.principal.workspace_id, identity.agent_id)
        self.ensure_placeholders(identity, home)
        # A crash between creating a home and committing the context triplet is
        # reconciled on a subsequent invocation, including if the home exists.
        self.service.provision_private(
            identity.principal, "agent", str(identity.agent_id),
            idempotency_key=hashlib.sha256(("agent:" + idempotency_key).encode()).hexdigest(),
        )
        return home

    def ensure_placeholders(self, identity: ContextIdentity, home: AgentHome) -> None:
        """Create-only 0444 guest targets, guarded by a workspace-wide host lease.

        Existing AGENTS/MODEL/MEMORY files are NEVER overwritten or silently
        adopted if they have content, redirects, or additional hard links.
        This does not give guests authority to update PostgreSQL.
        """
        lease = self.registry._guard(identity.principal.workspace_id, identity.agent_id,
                                     "agent.context.placeholders")
        uncertain = True
        fd = -1
        sub = -1
        try:
            fd = self.registry._dir_fd()
            sub = os.open(home.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                          os.O_CLOEXEC, dir_fd=fd)
            for role in ROLES:
                try:
                    created = os.open(role, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                      os.O_NOFOLLOW | os.O_CLOEXEC, 0o444, dir_fd=sub)
                except FileExistsError:
                    pass
                else:
                    try:
                        os.fchmod(created, 0o444)
                        os.fsync(created)
                    finally:
                        os.close(created)
                info = os.stat(role, dir_fd=sub, follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode) or info.st_size != 0 or info.st_nlink != 1
                        or info.st_mode & 0o222):
                    raise ScopeDeniedError("Agent context placeholder is not empty and read-only")
            os.fsync(sub)
            uncertain = False
        finally:
            if sub >= 0:
                os.close(sub)
            if fd >= 0:
                os.close(fd)
            self.registry.ledger.finish(
                lease, result_sha256=hashlib.sha256(
                    f"agent-context:{identity.agent_id}".encode()).hexdigest(),
                uncertain=uncertain,
            )

    def read(self, identity: ContextIdentity) -> dict[str, Any]:
        home = self.registry.lookup(identity.principal.workspace_id, identity.agent_id)
        if home is None:
            raise ScopeDeniedError("Agent home has not been registered")
        return {
            "guest_home": home.guest_path,
            "sandbox": self.service.read_triple(
                identity.principal, "sandbox", str(identity.principal.workspace_id)
            ),
            "agent": self.service.read_triple(
                identity.principal, "agent", str(identity.agent_id)
            ),
        }


class ContextMutationBroker:
    """Admitted CAS writes by registered agents; never a public DB endpoint.

    The host operator passes an identity created from its authenticated worker
    dispatch, not fields from guest JSON. A workspace lease is acquired before
    database mutation and finished with its SHA receipt after completion.
    Resource admission and PostgreSQL CAS are separate safeguards.
    """

    def __init__(self, *, service: WorkspaceContextService,
                 ledger: WorkspaceLeaseLedger, registry: AgentHomeRegistry) -> None:
        self.service, self.ledger, self.registry = service, ledger, registry

    @staticmethod
    def _permitted(identity: ContextIdentity, kind: str, name: str, role: str) -> None:
        validate_identity(kind, name, role)
        if kind == "agent" and name == str(identity.agent_id):
            return
        if (kind == "sandbox" and name == str(identity.principal.workspace_id)
                and role in {"MODEL.md", "MEMORY.md"}):
            return
        raise ScopeDeniedError("Agent context scope does not permit the requested document")

    def update(self, identity: ContextIdentity, kind: str, name: str, role: str, *,
               content: str, expected_revision: int, idempotency_key: str,
               reason: str = "") -> dict[str, Any]:
        self._permitted(identity, kind, name, role)
        if self.registry.lookup(identity.principal.workspace_id, identity.agent_id) is None:
            raise ScopeDeniedError("Unregistered agent cannot mutate context")
        pin = self.ledger.image_status(identity.principal.workspace_id)
        if pin is None or not isinstance(pin.get("digest"), str):
            raise StateMismatchError("No qualified pinned workspace image")
        resource = f"potbot:context:{kind}:{name}:{role.lower()}"
        request = AccessRequest(
            identity.principal.workspace_id, identity.agent_id, "context.update",
            idempotency_key, "write", (AccessResource("external", resource, "write"),),
            pin["digest"],
        )
        lease = self.ledger.claim(request, ttl_seconds=120, replay_existing=False)
        uncertain = True  # An interrupted DB commit cannot be assumed rolled back.
        evidence = hashlib.sha256(b"context-operation-indeterminate").hexdigest()
        try:
            result = self.service.update(
                identity.principal, kind, name, role,
                content=content, expected_revision=expected_revision,
                idempotency_key=idempotency_key, reason=reason,
            )
            evidence = str(result["sha256"])
            uncertain = False
            return result
        except (InvalidInputError, ScopeDeniedError, StateMismatchError, RevisionConflictError):
            # These failures are locally admitted and never write durable context.
            uncertain = False
            evidence = hashlib.sha256(b"context-admission-rejected").hexdigest()
            raise
        finally:
            self.ledger.finish(lease, result_sha256=evidence, uncertain=uncertain)


def context_instruction_packet(bundle: dict[str, Any], *, max_bytes: int = 262_144) -> str:
    """Build a bounded, deterministic packet without promoting MODEL/MEMORY to policy.

    This is a serialization helper, NOT a user/system prompt injector. Callers
    must place untrusted content in data slots, not privileged instruction slots.
    """
    if type(max_bytes) is not int or not 1024 <= max_bytes <= 1_048_576:
        raise InvalidInputError("Context packet limit is invalid")
    if not isinstance(bundle, dict) or set(bundle) != {"sandbox", "agent", "guest_home"}:
        raise InvalidInputError("Context packet needs exact sandbox and agent views")
    result: list[dict[str, str]] = []
    for kind in ("sandbox", "agent"):
        docs = bundle[kind]
        if not isinstance(docs, dict) or set(docs) != set(ROLES):
            raise InvalidInputError("Incomplete context document triple")
        for role in ROLES:
            content = docs[role]["content"]
            from potbot.context.model import validate_content
            validate_content(content)
            result.append({"scope": kind, "role": role, "content": content,
                           "classification": ("scoped-instructions" if role == "AGENTS.md"
                                              else "untrusted-context-data")})
    packet = json.dumps({"schema": "potbot-context-packet/v1", "documents": result},
                        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(packet.encode("utf-8")) > max_bytes:
        raise InvalidInputError("Assembled context exceeds bounded packet size")
    return packet
