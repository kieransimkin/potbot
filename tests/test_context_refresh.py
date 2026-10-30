"""No real PostgreSQL or Bubblewrap needed for the refresh admission contract."""
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4

import pytest

from potbot.context.refresh import collect_agent_projections, exclusive_context_refresh
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError, StateMismatchError


@dataclass
class Lease:
    id: int = 1


class Ledger:
    def __init__(self):
        self.workspace_id = uuid4()
        self.home = None
        self.recorded = []
        self.pinned = True
        self.fail = False

    def image_status(self, wid):
        assert wid == self.workspace_id
        return {"digest": "a" * 64} if self.pinned else None

    def claim(self, request, **kwargs):
        if self.fail:
            raise StateMismatchError("another live worker owns the workspace")
        self.recorded.append((request, kwargs))
        return Lease()

    def finish(self, lease, **kwargs):
        self.recorded.append((lease, kwargs))


def test_refresh_fences_whole_workspace_and_completes():
    ledger = Ledger()
    principal = WorkspaceContext(ledger.workspace_id, uuid4())
    with exclusive_context_refresh(ledger, principal):
        assert len(ledger.recorded) == 1
        request = ledger.recorded[0][0]
        assert request.resources[0].kind == "workspace"
        assert request.resources[0].name == "*"
        assert request.effect == "write"
    assert ledger.recorded[-1][1]["uncertain"] is False


def test_refresh_failure_is_fenced():
    ledger = Ledger()
    principal = WorkspaceContext(ledger.workspace_id, uuid4())
    with pytest.raises(RuntimeError):
        with exclusive_context_refresh(ledger, principal):
            raise RuntimeError("source mismatch")
    assert ledger.recorded[-1][1]["uncertain"] is True


def test_refresh_requires_a_pinned_workspace():
    ledger = Ledger()
    ledger.pinned = False
    with pytest.raises(StateMismatchError):
        with exclusive_context_refresh(ledger, WorkspaceContext(ledger.workspace_id, uuid4())):
            pass
    assert ledger.recorded == []


def test_refresh_rejects_missing_principal():
    with pytest.raises(ScopeDeniedError):
        with exclusive_context_refresh(Ledger(), None):
            pass


def test_refresh_does_not_claim_when_parallel_lease_exists():
    ledger = Ledger()
    ledger.fail = True
    with pytest.raises(StateMismatchError):
        with exclusive_context_refresh(ledger, WorkspaceContext(ledger.workspace_id, uuid4())):
            pass
    assert ledger.recorded == []


def test_agent_capture_rejects_other_workspace():
    class Manager:
        principal = WorkspaceContext(uuid4(), uuid4())

    with pytest.raises(ScopeDeniedError):
        collect_agent_projections(Manager(), Ledger(), uuid4())


def _memory_view(workspace, *, model_revision=1):
    from potbot.context.model import ROLES, sha256

    docs = {
        role: {"content": "# " + role, "revision": model_revision if role == "MODEL.md" else 1,
               "sha256": sha256("# " + role)}
        for role in ROLES
    }
    class Snapshot:
        workspace_id = workspace
        revisions = (1, 1, 1)
        digests = tuple(docs[role]["sha256"] for role in ROLES)
        def inspect(self, **kwargs): return ()

    return docs, Snapshot()


def test_root_revision_refresh_is_detected():
    from potbot.context.refresh import context_is_stale
    from types import SimpleNamespace

    wid = uuid4()
    docs, snapshot = _memory_view(wid, model_revision=2)
    class Service:
        def read_triple(self, principal, kind, name):
            assert kind == "sandbox" and name == str(wid)
            return docs
    manager = SimpleNamespace(principal=WorkspaceContext(wid, uuid4()), service=Service())
    runtime = SimpleNamespace(context_projection=snapshot, agent_context_projections=())
    assert context_is_stale(manager, runtime, Ledger(), include_agents=False)


def test_fresh_root_without_agents_does_not_refresh():
    from potbot.context.refresh import context_is_stale
    from types import SimpleNamespace

    wid = uuid4()
    docs, snapshot = _memory_view(wid)
    class Service:
        def read_triple(self, principal, kind, name): return docs
    manager = SimpleNamespace(principal=WorkspaceContext(wid, uuid4()), service=Service())
    runtime = SimpleNamespace(context_projection=snapshot, agent_context_projections=())
    assert not context_is_stale(manager, runtime, Ledger(), include_agents=False)
