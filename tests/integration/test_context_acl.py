"""Real PostgreSQL RLS tests. Requires a disposable POTBOT_TEST_DSN fixture."""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from potbot.context.bootstrap import (
    grant_shared_context_writer, revoke_shared_context_writer, seed_shared_context,
)
from potbot.context.model import ROLES
from potbot.context.service import WorkspaceContextService
from potbot.domain.errors import ScopeDeniedError
from potbot.persistence.database import admin_connection

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("role", ROLES)
def test_shared_context_requires_explicit_writer_grant(app_env, role):
    ctx = app_env.context
    service = WorkspaceContextService(app_env.db)
    name = str(ctx.workspace_id)
    document = service.read(ctx, "sandbox", name, role)
    history_count = len(service.history(ctx, "sandbox", name, role))
    request = {
        "content": "# approved\n",
        "expected_revision": document["revision"],
        "idempotency_key": uuid4().hex,
    }
    with pytest.raises(ScopeDeniedError):
        service.update(ctx, "sandbox", name, role, **request)
    unchanged = service.read(ctx, "sandbox", name, role)
    assert (unchanged["revision"], unchanged["sha256"]) == (
        document["revision"], document["sha256"],
    )
    assert len(service.history(ctx, "sandbox", name, role)) == history_count

    with admin_connection(app_env.admin_dsn, app_env.schema) as conn:
        grant_shared_context_writer(conn, ctx.workspace_id, UUID(document["object_id"]),
                                    ctx.principal_id, (role,))
    result = service.update(ctx, "sandbox", name, role, **request)
    assert result["revision"] == document["revision"] + 1
    assert service.update(ctx, "sandbox", name, role, **request) == result
    assert len(service.history(ctx, "sandbox", name, role)) == history_count + 1

    with admin_connection(app_env.admin_dsn, app_env.schema) as conn:
        revoke_shared_context_writer(conn, ctx.workspace_id, UUID(document["object_id"]),
                                     ctx.principal_id, (role,))
    # A revoked grant cannot authorize a new mutation. Session receipt-replay
    # revocation is independently tested in test_context_skills.py.
    with pytest.raises(ScopeDeniedError):
        service.update(ctx, "sandbox", name, role, content="# denied\n",
                       expected_revision=result["revision"], idempotency_key=uuid4().hex)
    current = service.read(ctx, "sandbox", name, role)
    assert current["revision"] == result["revision"]
    assert current["content"] == request["content"]
    assert len(service.history(ctx, "sandbox", name, role)) == history_count + 1


def test_shared_primitive_idempotent_provisioning_and_role_scoped_grant(app_env):
    ctx = app_env.context
    name = "tool-" + uuid4().hex
    with admin_connection(app_env.admin_dsn, app_env.schema) as conn:
        object_id = seed_shared_context(conn, ctx.workspace_id, "tool", name)
        assert seed_shared_context(conn, ctx.workspace_id, "tool", name) == object_id
        grant_shared_context_writer(conn, ctx.workspace_id, object_id, ctx.principal_id,
                                    ("MEMORY.md",))
    service = WorkspaceContextService(app_env.db)
    memory = service.read(ctx, "tool", name, "MEMORY.md")
    result = service.update(ctx, "tool", name, "MEMORY.md", content="historical evidence\n",
                            expected_revision=memory["revision"], idempotency_key=uuid4().hex)
    assert result["revision"] == 2
    model = service.read(ctx, "tool", name, "MODEL.md")
    with pytest.raises(ScopeDeniedError):
        service.update(ctx, "tool", name, "MODEL.md", content="not permitted\n",
                       expected_revision=model["revision"], idempotency_key=uuid4().hex)
    assert len(service.history(ctx, "tool", name, "MEMORY.md")) == 2


def test_restricted_login_cannot_self_grant(app_env):
    ctx = app_env.context
    document = WorkspaceContextService(app_env.db).read(
        ctx, "sandbox", str(ctx.workspace_id), "MODEL.md"
    )
    with pytest.raises(ScopeDeniedError):
        with app_env.db.transaction(ctx) as conn:
            conn.execute(
                "INSERT INTO workspace_context_write_grants "
                "(workspace_id,object_id,principal_id,role) VALUES (%s,%s,%s,'MODEL.md')",
                (ctx.workspace_id, document["object_id"], ctx.principal_id),
            )
