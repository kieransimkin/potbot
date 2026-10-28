"""Real PostgreSQL RLS tests. Requires a disposable POTBOT_TEST_DSN fixture."""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from potbot.context.bootstrap import (
    grant_shared_context_writer, revoke_shared_context_writer, seed_shared_context,
)
from potbot.context.service import WorkspaceContextService
from potbot.domain.errors import ScopeDeniedError
from potbot.persistence.database import admin_connection

pytestmark = pytest.mark.postgres


def test_shared_context_requires_explicit_writer_grant(app_env):
    ctx = app_env.context
    service = WorkspaceContextService(app_env.db)
    name = str(ctx.workspace_id)
    model = service.read(ctx, "sandbox", name, "MODEL.md")
    with pytest.raises(ScopeDeniedError):
        service.update(ctx, "sandbox", name, "MODEL.md", content="# ungranted\n",
                       expected_revision=model["revision"], idempotency_key=uuid4().hex)
    with admin_connection(app_env.admin_dsn, app_env.schema) as conn:
        grant_shared_context_writer(conn, ctx.workspace_id, UUID(model["object_id"]),
                                    ctx.principal_id, ("MODEL.md",))
    result = service.update(ctx, "sandbox", name, "MODEL.md", content="# approved\n",
                            expected_revision=model["revision"], idempotency_key=uuid4().hex)
    assert result["revision"] == model["revision"] + 1
    with admin_connection(app_env.admin_dsn, app_env.schema) as conn:
        revoke_shared_context_writer(conn, ctx.workspace_id, UUID(model["object_id"]),
                                     ctx.principal_id, ("MODEL.md",))
    with pytest.raises(ScopeDeniedError):
        service.update(ctx, "sandbox", name, "MODEL.md", content="# denied\n",
                       expected_revision=result["revision"], idempotency_key=uuid4().hex)
    assert service.read(ctx, "sandbox", name, "MODEL.md")["content"] == "# approved\n"


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
