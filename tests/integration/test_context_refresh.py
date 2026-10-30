"""Real PostgreSQL snapshot refresh requires disposable POTBOT_TEST_DSN."""
from uuid import UUID, uuid4

import pytest

from potbot.context.bootstrap import grant_shared_context_writer
from potbot.context.inspection import ContextHistoryInspector
from potbot.context.projection import ContextProjectionManager
from potbot.context.service import WorkspaceContextService
from potbot.persistence.database import admin_connection

pytestmark = pytest.mark.postgres


def test_refresh_uses_new_revision_without_mutating_previous_snapshot(app_env, tmp_path):
    principal = app_env.context
    store = WorkspaceContextService(app_env.db)
    inspector = ContextHistoryInspector(store)
    parent = tmp_path / "host-only-context"
    parent.mkdir(mode=0o700)
    manager = ContextProjectionManager(service=store, principal=principal, parent=parent)
    name = str(principal.workspace_id)
    previous = manager.capture()
    old_bytes = (previous.directory / "MODEL.md").read_bytes()
    document = store.read(principal, "sandbox", name, "MODEL.md")
    with admin_connection(app_env.admin_dsn, app_env.schema) as connection:
        grant_shared_context_writer(connection, principal.workspace_id, UUID(document["object_id"]),
                                    principal.principal_id, ("MODEL.md",))
    changed = store.update(principal, "sandbox", name, "MODEL.md",
                           content="# Snapshot two\n\nVerified.\n",
                           expected_revision=document["revision"], idempotency_key=uuid4().hex)
    assert inspector.freshness(principal, "sandbox", name, previous).stale is True
    updated = manager.capture()
    assert updated.revisions[1] == changed["revision"]
    assert inspector.freshness(principal, "sandbox", name, updated).stale is False
    assert (previous.directory / "MODEL.md").read_bytes() == old_bytes
    assert (updated.directory / "MODEL.md").read_bytes() != old_bytes
    manager.close()
    assert list(parent.iterdir()) == []
