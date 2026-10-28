"""No PostgreSQL required: validates history integrity and restore request shape."""
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from potbot.context.inspection import ContextHistoryInspector
from potbot.context.model import ROLES, sha256
from potbot.context.semantic_roles import classify_context_basename
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError


class FakeCursor:
    def __init__(self, row):
        self.row = row
    def fetchone(self):
        return self.row


class FakeDB:
    def __init__(self, revisions):
        self.revisions = revisions
        self.last_query = None
    @contextmanager
    def transaction(self, context):
        assert isinstance(context, WorkspaceContext)
        yield self
    def execute(self, query, parameters):
        self.last_query = (query, parameters)
        return FakeCursor(self.revisions.get(parameters[-1]))


class FakeService:
    def __init__(self):
        self.workspace = uuid4()
        self.principal = WorkspaceContext(self.workspace, uuid4())
        self.object_id = uuid4()
        self.revisions = {}
        for n, content in [(1, "# One\n"), (2, "# Two\n")]:
            self.revisions[n] = dict(revision=n, content=content, sha256=sha256(content),
                                     actor="principal:test", reason="fixture",
                                     created_at=datetime(2026, 1, n, tzinfo=UTC))
        self.database = FakeDB(self.revisions)
        self.updates = []
    def _get_object(self, conn, context, kind, name):
        assert kind == "agent" and name == "sample"
        return self.object_id
    def update(self, context, kind, name, role, **kwargs):
        self.updates.append(kwargs)
        if kwargs["expected_revision"] != 2:
            raise RevisionConflictError("stale")
        return {"revision": 3, "sha256": sha256(kwargs["content"])}
    def read(self, context, kind, name, role):
        return dict(object_id=str(self.object_id), **self.revisions[2])
    def history(self, context, kind, name, role, *, limit):
        return [{"revision": 2, "sha256": self.revisions[2]["sha256"]}]
    def read_triple(self, context, kind, name):
        return {role: {"revision": 2, "sha256": sha256("# Two\n")} for role in ROLES}


def test_revision_content_and_diff():
    service = FakeService()
    inspector = ContextHistoryInspector(service)
    rev = inspector.revision(service.principal, "agent", "sample", "MODEL.md", 1)
    assert rev.sha256 == sha256(rev.content)
    assert "-# One" in inspector.diff(service.principal, "agent", "sample", "MODEL.md", 1, 2)
    assert "+# Two" in inspector.diff(service.principal, "agent", "sample", "MODEL.md", 1, 2)


def test_restoration_uses_authorized_cas_and_preserves_history():
    s = FakeService()
    svc = ContextHistoryInspector(s)
    result = svc.restore(s.principal, "agent", "sample", "MEMORY.md",
                         source_revision=1, expected_revision=2,
                         idempotency_key="one", reason="reviewed")
    assert result["revision"] == 3
    assert s.updates[-1]["content"] == "# One\n"
    assert "Restore from revision 1" in s.updates[-1]["reason"]
    with pytest.raises(RevisionConflictError):
        svc.restore(s.principal, "agent", "sample", "MEMORY.md",
                    source_revision=1, expected_revision=1, idempotency_key="two")


def test_history_integrity_and_unavailable_revision():
    s = FakeService()
    svc = ContextHistoryInspector(s)
    s.revisions[1]["sha256"] = "0" * 64
    with pytest.raises(RevisionConflictError):
        svc.revision(s.principal, "agent", "sample", "MODEL.md", 1)
    with pytest.raises(ScopeDeniedError):
        svc.revision(s.principal, "agent", "sample", "MODEL.md", 9)
    with pytest.raises(InvalidInputError):
        svc.revision(s.principal, "agent", "sample", "MODEL.md", True)


def test_bounded_diff_and_inspection():
    s = FakeService()
    svc = ContextHistoryInspector(s)
    assert svc.inspect(s.principal, "agent", "sample", "MODEL.md")["revision"] == 2
    with pytest.raises(InvalidInputError):
        svc.diff(s.principal, "agent", "sample", "MODEL.md", 1, 2, max_lines=0)
    s.revisions[1]["content"] = "a\n" * 6000
    s.revisions[1]["sha256"] = sha256(s.revisions[1]["content"])
    with pytest.raises(InvalidInputError):
        svc.diff(s.principal, "agent", "sample", "MODEL.md", 1, 2)


def test_freshness_detects_changed_revisions_without_mutation():
    class Projection:
        workspace_id = None
        revisions = (1, 1, 1)
        digests = (sha256("# Two\n"),) * 3
        def inspect(self): return ()
    s = FakeService()
    projection = Projection()
    projection.workspace_id = s.workspace
    status = ContextHistoryInspector(s).freshness(s.principal, "agent", "sample", projection)
    assert status.stale and status.current_revisions == (2, 2, 2)
    projection.revisions = (2, 2, 2)
    assert not ContextHistoryInspector(s).freshness(s.principal, "agent", "sample", projection).stale
    projection.workspace_id = uuid4()
    with pytest.raises(ScopeDeniedError):
        ContextHistoryInspector(s).freshness(s.principal, "agent", "sample", projection)


@pytest.mark.parametrize("name", ["AGENTS.md", "MODEL.md", "MEMORY.md", "SKILL.md"])
def test_semantic_roles_are_exact_and_untrusted(name):
    result = classify_context_basename(name)
    assert result and result.type_ref.startswith("synny://type/")
    assert not result.trusted and not result.instructions_authoritative
    assert classify_context_basename(name.lower()) is None or name.lower() == name


@pytest.mark.parametrize("name", ["../MODEL.md", "x/MODEL.md", "x\\MODEL.md", "", "\x00"])
def test_semantic_classifier_rejects_paths(name):
    with pytest.raises(InvalidInputError):
        classify_context_basename(name)


def test_inspection_fails_if_history_head_does_not_match_current():
    s = FakeService()
    svc = ContextHistoryInspector(s)
    s.history = lambda *a, **kw: [{"revision": 1, "sha256": "0" * 64}]
    with pytest.raises(RevisionConflictError):
        svc.inspect(s.principal, "agent", "sample", "MODEL.md")


def test_semantic_candidate_descriptor_matches_local_classifier():
    import json
    from pathlib import Path
    from potbot.context.semantic_roles import ROLE_TYPES

    descriptor = json.loads((Path(__file__).resolve().parents[1] /
                             "vocab/context-document-roles-1.0.json").read_text())
    assert descriptor["status"] == "candidate_not_merged_into_pinned_syn_catalog"
    assert {row["basename"]: row["type_ref"] for row in descriptor["types"]} == ROLE_TYPES
    assert {row["rule"] for row in descriptor["types"]} == {
        classify_context_basename(name).classifier for name in ROLE_TYPES
    }
