"""Registered object contexts, shared source identities and host runtime views.

Registration is host-only and does not authorize execution. Native source
objects are seeded transactionally by migration-33 triggers. Runtime-only
installations get a principal-owned view, shared by that principal's agents;
there is no public registration or self-grant RPC.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from potbot.context.model import ROLES
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError

OBJECT_KINDS = ("tool", "workflow", "gateway", "model", "connector", "operator", "primitive")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class RuntimeObject:
    kind: str
    reference: str
    fingerprint: str
    state: str = "active"

    def __post_init__(self) -> None:
        if (self.kind not in OBJECT_KINDS or not isinstance(self.reference, str)
                or not 1 <= len(self.reference) <= 512
                or any(ord(c) < 33 or ord(c) > 126 for c in self.reference)
                or not isinstance(self.fingerprint, str) or not _HASH.fullmatch(self.fingerprint)
                or self.state not in ("active", "retired")):
            raise InvalidInputError("Invalid host runtime object")

    def name(self, principal: WorkspaceContext) -> str:
        digest = hashlib.sha256((self.kind + ":" + self.reference).encode()).hexdigest()
        return "runtime." + principal.principal_id.hex + "." + digest


def register_runtime_objects(connection: Any, principal: WorkspaceContext,
                             objects: Iterable[RuntimeObject]) -> tuple[UUID, ...]:
    """Borrow the caller's transaction; no second commit or admin connection."""
    from potbot.context.service import WorkspaceContextService
    from potbot.context.sessions import _BoundTransaction

    if not isinstance(principal, WorkspaceContext):
        raise ScopeDeniedError("Runtime registration needs a trusted principal")
    values = tuple(objects)
    if len(values) > 512 or any(not isinstance(v, RuntimeObject) for v in values):
        raise InvalidInputError("Runtime object inventory exceeds its bound")
    # Global ordering prevents two registry reconciliations taking document locks
    # in opposite orders. Repeated definitions in one batch must agree.
    chosen: dict[tuple[str, str], RuntimeObject] = {}
    for value in values:
        key = value.kind, value.reference
        if key in chosen and chosen[key] != value:
            raise InvalidInputError("Conflicting runtime object definitions")
        chosen[key] = value
    service = WorkspaceContextService(_BoundTransaction(principal, connection))
    ids: list[UUID] = []
    for value in sorted(chosen.values(), key=lambda v: (v.kind, v.reference)):
        name = value.name(principal)
        result = service.provision_private(principal, value.kind, name,
                                           idempotency_key="runtime:" + name)
        object_id = UUID(result["object_id"])
        # Existing documents are NEVER rewritten to reflect an installation
        # change. The source-version journal records that separate observation.
        connection.execute(
            "SELECT revision FROM workspace_context_documents WHERE workspace_id=%s "
            "AND object_id=%s AND role='MODEL.md' FOR UPDATE",
            (principal.workspace_id, object_id),
        ).fetchone()
        previous = connection.execute(
            "SELECT fingerprint,state,revision FROM workspace_context_runtime_sources "
            "WHERE workspace_id=%s AND object_id=%s ORDER BY revision DESC LIMIT 1",
            (principal.workspace_id, object_id),
        ).fetchone()
        if previous is None or (previous["fingerprint"], previous["state"]) != (
                value.fingerprint, value.state):
            connection.execute(
                "INSERT INTO workspace_context_runtime_sources "
                "(workspace_id,object_id,owner_principal_id,kind,reference,fingerprint,state,revision) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (principal.workspace_id, object_id, principal.principal_id, value.kind,
                 value.reference, value.fingerprint, value.state,
                 1 if previous is None else previous["revision"] + 1),
            )
        ids.append(object_id)
    return tuple(ids)


# The latest runtime observation is append-only. Source-key ordering never acts
# as permission; both the source index and objects are scoped independently by RLS.
_VISIBLE = """WITH runtime_sources AS (
    SELECT DISTINCT ON (object_id) object_id,reference,state,fingerprint
    FROM workspace_context_runtime_sources WHERE workspace_id=%s
    ORDER BY object_id,revision DESC
), visible AS (
    SELECT o.id,o.kind,o.name,o.visibility,s.source_family AS family,
           s.source_key AS reference,NULL::text AS fingerprint
    FROM workspace_context_objects o JOIN workspace_context_sources s
      ON s.workspace_id=o.workspace_id AND s.object_id=o.id
    WHERE o.workspace_id=%s AND s.state='active'
    UNION ALL
    SELECT o.id,o.kind,o.name,o.visibility,'runtime' AS family,r.reference,r.fingerprint
    FROM workspace_context_objects o JOIN runtime_sources r ON r.object_id=o.id
    WHERE o.workspace_id=%s AND r.state='active'
) """


def list_objects(connection: Any, principal: WorkspaceContext, *,
                 after: UUID | None = None, limit: int = 25) -> dict[str, Any]:
    if (not isinstance(principal, WorkspaceContext) or type(limit) is not int
            or not 1 <= limit <= 100 or (after is not None and not isinstance(after, UUID))):
        raise InvalidInputError("Invalid object catalogue bounds")
    rows = connection.execute(
        _VISIBLE + "SELECT * FROM visible WHERE kind=ANY(%s) AND (%s::uuid IS NULL OR id>%s) "
        "ORDER BY id LIMIT %s",
        (principal.workspace_id, principal.workspace_id, principal.workspace_id,
         list(OBJECT_KINDS), after, after, limit + 1),
    ).fetchall()
    page = rows[:limit]
    return {"objects": [{**dict(row), "id": str(row["id"])} for row in page],
            "next": str(page[-1]["id"]) if len(rows) > limit and page else None}


def object_target(connection: Any, principal: WorkspaceContext, object_id: UUID,
                  role: str, *, write: bool) -> tuple[str, str]:
    """An opaque ID selects an object, never an actor or authority.

    Agent/worker/skill/sandbox scopes retain their stronger existing checks.
    A generic selector cannot be used to bypass same-principal agent privacy.
    """
    if not isinstance(object_id, UUID) or role not in ROLES or type(write) is not bool:
        raise InvalidInputError("Invalid registered object selector")
    row = connection.execute(
        _VISIBLE + "SELECT * FROM visible WHERE id=%s AND kind=ANY(%s)",
        (principal.workspace_id, principal.workspace_id, principal.workspace_id,
         object_id, list(OBJECT_KINDS)),
    ).fetchone()
    if row is None:
        raise ScopeDeniedError("Registered object is not available")
    if row["family"] != "runtime":
        # Serializes retirement with an in-flight operation, including replay.
        current = connection.execute(
            "SELECT state FROM workspace_context_sources WHERE workspace_id=%s "
            "AND object_id=%s FOR SHARE",
            (principal.workspace_id, object_id),
        ).fetchone()
        if current is None or current["state"] != "active":
            raise ScopeDeniedError("Registered object is retired")
    else:
        # Shared lock on the model is also the registration/version lock. A
        # catalogue entry can never authorize a write after a completed retire.
        connection.execute(
            "SELECT revision FROM workspace_context_documents WHERE workspace_id=%s "
            "AND object_id=%s AND role='MODEL.md' " + ("FOR UPDATE" if write else "FOR SHARE"),
            (principal.workspace_id, object_id),
        ).fetchone()
        latest = connection.execute(
            "SELECT state FROM workspace_context_runtime_sources WHERE workspace_id=%s "
            "AND object_id=%s ORDER BY revision DESC LIMIT 1",
            (principal.workspace_id, object_id),
        ).fetchone()
        if latest is None or latest["state"] != "active":
            raise ScopeDeniedError("Registered object is retired")
    if write and row["visibility"] == "shared":
        allowed = connection.execute(
            "SELECT role FROM workspace_context_write_grants WHERE workspace_id=%s "
            "AND object_id=%s AND principal_id=%s AND role=%s FOR SHARE",
            (principal.workspace_id, object_id, principal.principal_id, role),
        ).fetchone()
        if allowed is None:
            raise ScopeDeniedError("Registered object is not writable")
    return row["kind"], row["name"]


def reconcile_installed_tools(service: Any, principal: WorkspaceContext, ledger: Any) -> None:
    """Observe the existing host-owned tool registry, without granting execution.

    Tool state stays in its existing source registry. Only a bounded definition
    hash and stable slug are recorded here; no credential or tool content is
    copied into an instruction document. Missing installations are retired.
    """
    with ledger._connect() as source:
        present = source.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trusted_shared_tools'"
        ).fetchone()
        rows = source.execute(
            "SELECT slug,definition_json FROM trusted_shared_tools WHERE workspace_id=? "
            "ORDER BY slug LIMIT 257", (str(principal.workspace_id),)
        ).fetchall() if present is not None else ()
    if len(rows) > 256:
        raise InvalidInputError("Installed tool inventory exceeds its bound")
    selected = {}
    for row in rows:
        slug = row["slug"]
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", slug):
            raise InvalidInputError("Invalid installed tool identity")
        raw = row["definition_json"].encode()
        if len(raw) > 1_048_576:
            raise InvalidInputError("Installed tool definition exceeds its bound")
        reference = "shared-tool:" + slug
        selected[reference] = RuntimeObject("tool", reference, hashlib.sha256(raw).hexdigest())
    with service.database.transaction(principal) as conn:
        previous = conn.execute(
            "WITH latest AS (SELECT DISTINCT ON (object_id) reference,fingerprint,state "
            "FROM workspace_context_runtime_sources "
            "WHERE workspace_id=%s AND owner_principal_id=%s AND kind='tool' "
            "AND reference LIKE 'shared-tool:%%' ORDER BY object_id,revision DESC) "
            "SELECT * FROM latest WHERE state='active' LIMIT 257",
            (principal.workspace_id, principal.principal_id),
        ).fetchall()
        if len(previous) > 256:
            raise InvalidInputError("Previous tool inventory exceeds its bound")
        for row in previous:
            if row["reference"] not in selected and row["state"] == "active":
                selected[row["reference"]] = RuntimeObject(
                    "tool", row["reference"], row["fingerprint"], "retired")
        register_runtime_objects(conn, principal, selected.values())
