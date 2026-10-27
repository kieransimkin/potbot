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
