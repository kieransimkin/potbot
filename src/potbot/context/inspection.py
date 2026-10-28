"""Host-side inspection, recovery, and snapshot freshness for context documents.

The authenticated database transaction enforces workspace membership and RLS.
MODEL.md and MEMORY.md are evidence, never instructions. No guest credentials,
filesystem writes, or privileged admin connections are provided by this module.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from potbot.context.model import ROLES, sha256, validate_identity
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError

if TYPE_CHECKING:
    from potbot.context.projection import ContextProjection
    from potbot.context.service import WorkspaceContextService


@dataclass(frozen=True, slots=True)
class ContextRevision:
    object_id: str
    role: str
    revision: int
    content: str
    sha256: str
    actor: str
    reason: str
    created_at: str

    def metadata(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id, "role": self.role,
            "revision": self.revision, "sha256": self.sha256,
            "actor": self.actor, "reason": self.reason,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ContextFreshness:
    """Read-only reconciliation; `stale` never modifies mounted inodes."""

    workspace_id: str
    roles: tuple[str, ...]
    pinned_revisions: tuple[int, ...]
    current_revisions: tuple[int, ...]
    stale: bool


class ContextHistoryInspector:
    """RLS-scoped history inspection and compare-and-swap restoration.

    Restores are *new* revisions, not rewrites or deletions of history. Shared
    context still requires the operator-issued per-role PostgreSQL write grant.
    """

    def __init__(self, service: WorkspaceContextService) -> None:
        self.service = service

    @staticmethod
    def _check(context: WorkspaceContext, kind: str, name: str, role: str) -> None:
        if not isinstance(context, WorkspaceContext):
            raise ScopeDeniedError("Authenticated workspace context required")
        validate_identity(kind, name, role)

    def revision(self, context: WorkspaceContext, kind: str, name: str,
                 role: str, revision: int) -> ContextRevision:
        self._check(context, kind, name, role)
        if type(revision) is not int or revision < 1:
            raise InvalidInputError("Revision must be a positive integer")
        with self.service.database.transaction(context) as conn:
            object_id = self.service._get_object(conn, context, kind, name)
            row = conn.execute(
                "SELECT revision,content,sha256,actor,reason,created_at "
                "FROM workspace_context_revisions "
                "WHERE workspace_id=%s AND object_id=%s AND role=%s AND revision=%s",
                (context.workspace_id, object_id, role, revision),
            ).fetchone()
        if row is None:
            raise ScopeDeniedError("The requested context revision is not accessible")
        content = row["content"]
        if sha256(content) != row["sha256"]:
            raise RevisionConflictError("Historical context content digest mismatch")
        return ContextRevision(
            str(object_id), role, row["revision"], content, row["sha256"],
            row["actor"], row["reason"], row["created_at"].isoformat(),
        )

    def diff(self, context: WorkspaceContext, kind: str, name: str, role: str,
             before_revision: int, after_revision: int, *, max_bytes: int = 65536,
             max_lines: int = 5000) -> str:
        """Bounded unified diff, for humans and UI; never prompts or instructions."""
        if (type(max_bytes) is not int or not 1024 <= max_bytes <= 262144
                or type(max_lines) is not int or not 1 <= max_lines <= 5000):
            raise InvalidInputError("Diff output bounds are invalid")
        before = self.revision(context, kind, name, role, before_revision)
        after = self.revision(context, kind, name, role, after_revision)
        a, b = before.content.splitlines(keepends=True), after.content.splitlines(keepends=True)
        if len(a) > max_lines or len(b) > max_lines:
            raise InvalidInputError("Diff exceeds input line budget")
        diff = difflib.unified_diff(a, b, fromfile=f"{role}@{before_revision}",
                                    tofile=f"{role}@{after_revision}", lineterm="\n")
        result: list[str] = []
        total = 0
        for part in diff:
            encoded = part.encode("utf-8")
            if total + len(encoded) > max_bytes:
                result.append("\n[diff truncated at output limit]\n")
                break
            total += len(encoded)
            result.append(part)
        return "".join(result)

    def restore(self, context: WorkspaceContext, kind: str, name: str,
                role: str, *, source_revision: int, expected_revision: int,
                idempotency_key: str, reason: str = "") -> dict[str, Any]:
        """Restore content as a fresh CAS revision through the existing ACL path."""
        self._check(context, kind, name, role)
        if type(expected_revision) is not int or expected_revision < 1:
            raise InvalidInputError("Restore requires current expected revision")
        source = self.revision(context, kind, name, role, source_revision)
        # If another agent updated the live document after inspection, the
        # service CAS rejects the write. Never silently merge or force replace.
        return self.service.update(
            context, kind, name, role, content=source.content,
            expected_revision=expected_revision, idempotency_key=idempotency_key,
            reason=f"Restore from revision {source_revision}: {reason}"[:1000],
        )

    def inspect(self, context: WorkspaceContext, kind: str, name: str,
                role: str, *, history_limit: int = 30) -> dict[str, Any]:
        """Compact metadata for UI/CLI, without copying historical text."""
        self._check(context, kind, name, role)
        current = self.service.read(context, kind, name, role)
        if sha256(current["content"]) != current["sha256"]:
            raise RevisionConflictError("Current document digest mismatch")
        history = self.service.history(context, kind, name, role, limit=history_limit)
        if (not history or history[0]["revision"] != current["revision"]
                or history[0]["sha256"] != current["sha256"]):
            raise RevisionConflictError("Context changed during inspection or history head is inconsistent; retry")
        return {
            "kind": kind, "name": name, "role": role,
            "object_id": current["object_id"],
            "revision": current["revision"], "sha256": current["sha256"],
            "bytes": len(current["content"].encode("utf-8")),
            "history": history,
        }

    def freshness(self, context: WorkspaceContext, kind: str, name: str,
                  snapshot: ContextProjection) -> ContextFreshness:
        """Compare database HEAD with operator-pinned snapshot; do not refresh live mounts.

        The controller must separately ensure snapshot provenance and guest
        isolation before accepting this as evidence of a safe mount.
        """
        if not isinstance(context, WorkspaceContext):
            raise ScopeDeniedError("Authenticated workspace context required")
        validate_identity(kind, name)
        if snapshot.workspace_id != context.workspace_id:
            raise ScopeDeniedError("Projection belongs to another workspace")
        snapshot.inspect()
        docs = self.service.read_triple(context, kind, name)
        revisions = tuple(docs[role]["revision"] for role in ROLES)
        hashes = tuple(docs[role]["sha256"] for role in ROLES)
        return ContextFreshness(
            str(context.workspace_id), ROLES, snapshot.revisions, revisions,
            revisions != snapshot.revisions or hashes != snapshot.digests,
        )
