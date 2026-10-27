"""Operator context snapshot tests; no PostgreSQL required for these checks."""
from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.context.model import ROLES
from potbot.context.projection import ContextProjectionManager
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError, StateMismatchError


class FakeService:
    def __init__(self) -> None:
        self.calls = 0
        self.data = {role: {"content": f"# {role}\n", "revision": 1,
                            "sha256": hashlib.sha256(f"# {role}\n".encode()).hexdigest()}
                     for role in ROLES}

    def read_triple(self, principal: WorkspaceContext, kind: str, name: str) -> dict:
        assert kind == "sandbox" and name == str(principal.workspace_id)
        self.calls += 1
        return self.data


def test_snapshot_contains_complete_pinned_triple(tmp_path: Path) -> None:
    parent = tmp_path / "operator"
    parent.mkdir(mode=0o700)
    principal = WorkspaceContext(uuid4(), uuid4())
    service = FakeService()
    manager = ContextProjectionManager(service=service, principal=principal, parent=parent)
    snapshot = manager.capture()
    assert service.calls == 1
    assert snapshot.workspace_id == principal.workspace_id
    assert snapshot.revisions == (1, 1, 1)
    assert [item.name for item in snapshot.inspect()] == list(ROLES)
    for path in snapshot.inspect():
        assert not path.stat().st_mode & 0o222
    manager.close()
    assert not snapshot.directory.exists()


def test_disallows_guest_writable_parent(tmp_path: Path) -> None:
    directory = tmp_path / "insecure"
    directory.mkdir(mode=0o777)
    directory.chmod(0o777)
    with pytest.raises(ScopeDeniedError):
        ContextProjectionManager(service=FakeService(),
                                 principal=WorkspaceContext(uuid4(), uuid4()), parent=directory)


def test_corrupted_snapshot_fails_closed(tmp_path: Path) -> None:
    parent = tmp_path / "operator"
    parent.mkdir(mode=0o700)
    manager = ContextProjectionManager(service=FakeService(),
                                       principal=WorkspaceContext(uuid4(), uuid4()), parent=parent)
    snapshot = manager.capture()
    target = snapshot.directory / "MODEL.md"
    target.chmod(0o600)
    target.write_text("tampered", encoding="utf-8")
    target.chmod(0o444)
    with pytest.raises(StateMismatchError):
        snapshot.inspect()
    manager.close()


def test_symlink_and_overlapping_guest_mount_rejected(tmp_path: Path) -> None:
    parent = tmp_path / "operator"
    parent.mkdir(mode=0o700)
    manager = ContextProjectionManager(service=FakeService(),
                                       principal=WorkspaceContext(uuid4(), uuid4()), parent=parent)
    snapshot = manager.capture()
    with pytest.raises(ScopeDeniedError):
        snapshot.inspect(excluded_roots=(parent,))
    model = snapshot.directory / "MODEL.md"
    model.unlink()
    model.symlink_to(snapshot.directory / "AGENTS.md")
    with pytest.raises(ScopeDeniedError):
        snapshot.inspect()
    # Intentionally don't follow the redirected symlink during cleanup.
    with pytest.raises(ScopeDeniedError):
        manager.close()
    model.unlink()
    manager.close()


def test_broken_database_digest_and_partial_snapshot(tmp_path: Path) -> None:
    parent = tmp_path / "operator"
    parent.mkdir(mode=0o700)
    service = FakeService()
    service.data["MODEL.md"]["sha256"] = "0" * 64
    manager = ContextProjectionManager(service=service,
                                       principal=WorkspaceContext(uuid4(), uuid4()), parent=parent)
    with pytest.raises(StateMismatchError):
        manager.capture()
    manager.close()
    assert not list(parent.iterdir())


def test_read_triple_uses_single_document_select() -> None:
    from contextlib import contextmanager
    from potbot.context.service import WorkspaceContextService

    class Connection:
        def __init__(self) -> None:
            self.queries = []

        def execute(self, query: str, params: tuple):
            self.queries.append((query, params))
            if "workspace_context_objects" in query:
                return self
            return self

        def fetchone(self):
            return {"id": uuid4()}

        def fetchall(self):
            return [{"role": role, **item} for role, item in FakeService().data.items()]

    class Database:
        def __init__(self): self.connection = Connection()

        @contextmanager
        def transaction(self, context):
            yield self.connection

    db = Database()
    context = WorkspaceContext(uuid4(), uuid4())
    result = WorkspaceContextService(db).read_triple(context, "sandbox", str(context.workspace_id))
    assert set(result) == set(ROLES)
    assert len(db.connection.queries) == 2  # object lookup + ONE consistent document SELECT


def test_read_triple_rejects_missing_document() -> None:
    from contextlib import contextmanager
    from potbot.context.service import WorkspaceContextService

    class Connection:
        def execute(self, query, _params): return self
        def fetchone(self): return {"id": uuid4()}
        def fetchall(self): return [{"role": "MODEL.md", **FakeService().data["MODEL.md"]}]

    class Database:
        @contextmanager
        def transaction(self, context): yield Connection()

    context = WorkspaceContext(uuid4(), uuid4())
    with pytest.raises(ScopeDeniedError):
        WorkspaceContextService(Database()).read_triple(context, "sandbox", str(context.workspace_id))
