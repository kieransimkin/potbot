"""Real PostgreSQL/RLS revision recovery against disposable app_env only."""
from uuid import uuid4

import pytest

from potbot.context.inspection import ContextHistoryInspector
from potbot.context.service import WorkspaceContextService
from potbot.domain.errors import RevisionConflictError, ScopeDeniedError

pytestmark = pytest.mark.postgres


def test_private_context_restore_preserves_audited_revisions(app_env):
    service = WorkspaceContextService(app_env.db)
    inspector = ContextHistoryInspector(service)
    context = app_env.context
    name = "recovery-" + uuid4().hex
    service.provision_private(context, "agent", name, idempotency_key=uuid4().hex)
    before = service.read(context, "agent", name, "MODEL.md")
    changed = service.update(context, "agent", name, "MODEL.md",
                             content="# Verified state\n", expected_revision=before["revision"],
                             idempotency_key=uuid4().hex, reason="operator-reviewed state")
    diff = inspector.diff(context, "agent", name, "MODEL.md", 1, 2)
    assert "Verified state" in diff
    restored = inspector.restore(context, "agent", name, "MODEL.md", source_revision=1,
                                 expected_revision=changed["revision"],
                                 idempotency_key=uuid4().hex, reason="revert observation")
    assert restored["revision"] == 3
    assert service.read(context, "agent", name, "MODEL.md")["content"] == before["content"]
    assert [row["revision"] for row in service.history(context, "agent", name, "MODEL.md")[:3]] == [3, 2, 1]
    with pytest.raises(RevisionConflictError):
        inspector.restore(context, "agent", name, "MODEL.md", source_revision=2,
                          expected_revision=2, idempotency_key=uuid4().hex)
    with pytest.raises(ScopeDeniedError):
        inspector.revision(app_env.other_context, "agent", name, "MODEL.md", 2)
