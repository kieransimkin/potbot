"""Requires POTBOT_TEST_DSN and a real privileged PostgreSQL fixture."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from potbot.context.bootstrap import grant_shared_context_writer
from potbot.context.model import sandbox_object_id
from potbot.context.service import WorkspaceContextService
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import RevisionConflictError, ScopeDeniedError
from potbot.persistence.database import admin_connection, provision_principal

pytestmark = pytest.mark.postgres


def _grant_test_writer(app_env, role):
    ctx = app_env.context
    with admin_connection(app_env.admin_dsn, app_env.schema) as conn:
        grant_shared_context_writer(conn, ctx.workspace_id, sandbox_object_id(ctx.workspace_id),
                                    ctx.principal_id, (role,))


def test_sandbox_is_seeded_in_same_workspace_registration(app_env):
    service = WorkspaceContextService(app_env.db)
    name = str(app_env.context.workspace_id)
    for role in ("AGENTS.md", "MODEL.md", "MEMORY.md"):
        document = service.read(app_env.context, "sandbox", name, role)
        assert document["revision"] == 1
        assert document["object_id"] == str(sandbox_object_id(app_env.context.workspace_id))
        assert document["content"].startswith("#")
    with pytest.raises(ScopeDeniedError):
        service.read(app_env.other_context, "sandbox", name, "MODEL.md")


def test_atomic_cas_replay_and_event_history(app_env):
    context = app_env.context
    service = WorkspaceContextService(app_env.db)
    name = str(context.workspace_id)
    _grant_test_writer(app_env, "MODEL.md")
    before = service.read(context, "sandbox", name, "MODEL.md")
    args = dict(content="# State\n\nReady.\n", expected_revision=before["revision"],
                idempotency_key=uuid4().hex, reason="verified observation")
    result = service.update(context, "sandbox", name, "MODEL.md", **args)
    assert service.update(context, "sandbox", name, "MODEL.md", **args) == result
    assert result["revision"] == before["revision"] + 1
    with pytest.raises(RevisionConflictError):
        service.update(context, "sandbox", name, "MODEL.md", content="# stale\n",
                       expected_revision=before["revision"], idempotency_key=uuid4().hex)
    assert service.read(context, "sandbox", name, "MODEL.md")["content"] == args["content"]
    history = service.history(context, "sandbox", name, "MODEL.md")
    assert history[0]["revision"] == result["revision"]
    assert history[0]["actor"] == f"principal:{context.principal_id}"
    events = service.events(context)
    assert any(e["event_type"] == "document.updated" and
               e["revision"] == result["revision"] for e in events)
    assert service.events(context, after_id=events[-1]["id"]) == []


def test_concurrent_updates_do_not_lose_writes(app_env):
    svc = WorkspaceContextService(app_env.db)
    ctx = app_env.context
    name = str(ctx.workspace_id)
    _grant_test_writer(app_env, "MEMORY.md")
    revision = svc.read(ctx, "sandbox", name, "MEMORY.md")["revision"]

    def write(i):
        try:
            return svc.update(ctx, "sandbox", name, "MEMORY.md",
                              content=f"# Memory {i}\n", expected_revision=revision,
                              idempotency_key=uuid4().hex)
        except RevisionConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, (1, 2)))
    assert sum(result is not None for result in results) == 1
    assert svc.read(ctx, "sandbox", name, "MEMORY.md")["revision"] == revision + 1


def test_private_principal_rls_blocks_same_workspace_peer(app_env):
    # Grant the second already-restricted login membership of the same workspace.
    peer = provision_principal(
        app_env.admin_dsn, app_env.schema,
        urlsplit(app_env.other_db.dsn).username,
        app_env.context.workspace_id,
        principal_id=app_env.other_context.principal_id,
    )
    assert peer == WorkspaceContext(app_env.context.workspace_id, app_env.other_context.principal_id)
    owner = WorkspaceContextService(app_env.db)
    guest = WorkspaceContextService(app_env.other_db)
    private_name = "private-" + uuid4().hex
    owner.provision_private(app_env.context, "agent", private_name,
                            idempotency_key=uuid4().hex)
    assert owner.read(app_env.context, "agent", private_name, "MODEL.md")["revision"] == 1
    with pytest.raises(ScopeDeniedError):
        guest.read(peer, "agent", private_name, "MODEL.md")
    with app_env.other_db.transaction(peer) as conn:
        assert conn.execute(
            "SELECT id FROM workspace_context_objects WHERE workspace_id=%s AND "
            "kind='agent' AND name=%s", (peer.workspace_id, private_name)
        ).fetchall() == []
        assert conn.execute(
            "SELECT role FROM workspace_context_documents WHERE workspace_id=%s AND "
            "object_id=(SELECT id FROM workspace_context_objects "
            "WHERE workspace_id=%s AND kind='agent' AND name=%s)",
            (peer.workspace_id, peer.workspace_id, private_name),
        ).fetchall() == []
    # Shared sandbox remains readable to every authenticated workspace member.
    assert guest.read(peer, "sandbox", str(peer.workspace_id), "AGENTS.md")["revision"] == 1
