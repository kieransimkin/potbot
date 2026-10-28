"""Idempotent operator/admin seed of sandbox context inside the provisioning transaction.

Only the *admin* provisioning or migration code calls this. It never accepts
agent-supplied instructions and never overwrites already-committed documents.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from potbot.context.model import document_templates, sandbox_object_id, sha256


def seed_sandbox_context(connection: Any, workspace_id: UUID) -> UUID:
    object_id = sandbox_object_id(workspace_id)
    name = str(workspace_id)
    connection.execute(
        "INSERT INTO workspace_context_objects "
        "(workspace_id,id,kind,name,visibility) VALUES (%s,%s,'sandbox',%s,'shared') "
        "ON CONFLICT (workspace_id,kind,name) DO NOTHING",
        (workspace_id, object_id, name),
    )
    row = connection.execute(
        "SELECT id,visibility FROM workspace_context_objects "
        "WHERE workspace_id=%s AND kind='sandbox' AND name=%s FOR UPDATE",
        (workspace_id, name),
    ).fetchone()
    if row is None or row["id"] != object_id or row["visibility"] != "shared":
        raise RuntimeError("Sandbox context identity conflicts with an existing object")
    for role, content in document_templates("sandbox", name).items():
        checksum = sha256(content)
        inserted = connection.execute(
            "INSERT INTO workspace_context_documents "
            "(workspace_id,object_id,role,content,revision,sha256) "
            "VALUES (%s,%s,%s,%s,1,%s) ON CONFLICT DO NOTHING RETURNING revision",
            (workspace_id, object_id, role, content, checksum),
        ).fetchone()
        if inserted is None:
            continue
        connection.execute(
            "INSERT INTO workspace_context_revisions "
            "(workspace_id,object_id,role,revision,content,sha256,actor,reason) "
            "VALUES (%s,%s,%s,1,%s,%s,'operator:bootstrap','initial sandbox context')",
            (workspace_id, object_id, role, content, checksum),
        )
        connection.execute(
            "INSERT INTO workspace_context_events "
            "(workspace_id,object_id,role,revision,actor,event_type,payload) "
            "VALUES (%s,%s,%s,1,'operator:bootstrap','document.provisioned',%s)",
            (workspace_id, object_id, role, Jsonb({"sha256": checksum})),
        )
    return object_id


def seed_shared_context(connection: Any, workspace_id: UUID, kind: str, name: str) -> UUID:
    """Admin-only idempotent provisioning for every shared Potbot primitive.

    The caller MUST use the migration-owner/admin connection; never expose it
    through the guest API. Old revisions are never replaced on repeated calls.
    """
    from uuid import NAMESPACE_URL, uuid5

    from potbot.context.model import validate_identity

    validate_identity(kind, name)
    if not isinstance(workspace_id, UUID):
        raise ValueError("workspace_id must be a UUID")
    if kind == "sandbox":
        if name != str(workspace_id):
            raise ValueError("sandbox name must be the canonical workspace UUID")
        return seed_sandbox_context(connection, workspace_id)
    object_id = uuid5(NAMESPACE_URL, f"potbot:context:{workspace_id}:{kind}:{name}")
    connection.execute(
        "INSERT INTO workspace_context_objects "
        "(workspace_id,id,kind,name,visibility) VALUES (%s,%s,%s,%s,'shared') "
        "ON CONFLICT (workspace_id,kind,name) DO NOTHING",
        (workspace_id, object_id, kind, name),
    )
    found = connection.execute(
        "SELECT id,visibility FROM workspace_context_objects "
        "WHERE workspace_id=%s AND kind=%s AND name=%s FOR UPDATE",
        (workspace_id, kind, name),
    ).fetchone()
    if found is None or found["id"] != object_id or found["visibility"] != "shared":
        raise RuntimeError("Context object identity conflicts with an existing object")
    for role, content in document_templates(kind, name).items():
        checksum = sha256(content)
        created = connection.execute(
            "INSERT INTO workspace_context_documents "
            "(workspace_id,object_id,role,content,revision,sha256) "
            "VALUES (%s,%s,%s,%s,1,%s) ON CONFLICT DO NOTHING RETURNING revision",
            (workspace_id, object_id, role, content, checksum),
        ).fetchone()
        if created is None:
            continue
        connection.execute(
            "INSERT INTO workspace_context_revisions "
            "(workspace_id,object_id,role,revision,content,sha256,actor,reason) "
            "VALUES (%s,%s,%s,1,%s,%s,'operator:bootstrap','initial shared object context')",
            (workspace_id, object_id, role, content, checksum),
        )
        connection.execute(
            "INSERT INTO workspace_context_events "
            "(workspace_id,object_id,role,revision,actor,event_type,payload) "
            "VALUES (%s,%s,%s,1,'operator:bootstrap','document.provisioned',%s)",
            (workspace_id, object_id, role, Jsonb({"sha256": checksum})),
        )
    return object_id


def grant_shared_context_writer(
    connection: Any, workspace_id: UUID, object_id: UUID, principal_id: UUID,
    roles: tuple[str, ...],
) -> None:
    """Admin-only explicit grant for shared object document roles.

    The database authenticates the granted principal's workspace membership via
    composite foreign keys; a restricted application login cannot self-grant.
    """
    from potbot.context.model import ROLES

    if not all(isinstance(x, UUID) for x in (workspace_id, object_id, principal_id)):
        raise ValueError("context writer grant needs canonical UUID identities")
    if not isinstance(roles, tuple) or not roles or any(role not in ROLES for role in roles):
        raise ValueError("context writer grant needs explicit document roles")
    row = connection.execute(
        "SELECT visibility FROM workspace_context_objects WHERE workspace_id=%s AND id=%s",
        (workspace_id, object_id),
    ).fetchone()
    if row is None or row["visibility"] != "shared":
        raise ValueError("grant target must be an existing shared context object")
    for role in dict.fromkeys(roles):
        connection.execute(
            "INSERT INTO workspace_context_write_grants "
            "(workspace_id,object_id,principal_id,role) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            (workspace_id, object_id, principal_id, role),
        )


def revoke_shared_context_writer(
    connection: Any, workspace_id: UUID, object_id: UUID, principal_id: UUID,
    roles: tuple[str, ...],
) -> None:
    """Admin-only grant revocation, immediately effective on new SQL statements."""
    from potbot.context.model import ROLES

    if not all(isinstance(x, UUID) for x in (workspace_id, object_id, principal_id)):
        raise ValueError("context writer revoke needs UUID identities")
    if not isinstance(roles, tuple) or any(role not in ROLES for role in roles):
        raise ValueError("invalid context grant roles")
    for role in dict.fromkeys(roles):
        connection.execute(
            "DELETE FROM workspace_context_write_grants "
            "WHERE workspace_id=%s AND object_id=%s AND principal_id=%s AND role=%s",
            (workspace_id, object_id, principal_id, role),
        )
