"""S15 cgroup/strace GUI monitor: fail-closed lifecycle and source isolation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError
from potbot.tools.gui_resource_audit import GuiAuditFactory, GuiAuditHandle
from potbot.tools.worker_telemetry import WorkerMonitorConfig


class FakeMonitor:
    def __init__(self, *args, **kwargs):
        self.arguments = args
        self.keywords = kwargs
        self.calls = []
        self.healthy = True
        self.complete = True

    def command(self, argv):
        self.calls.append(("command", argv))
        return ("/operator/strace", *argv)

    def start(self, pid):
        self.calls.append(("start", pid))

    def finish(self, *, confirmed_stopped):
        self.calls.append(("finish", confirmed_stopped))
        return self.complete


def test_monitor_starts_before_process_and_finishes_once():
    worker = FakeMonitor()
    owner = GuiAuditHandle(worker)
    assert not owner.healthy
    argv = ("/bin/true", "--test")
    assert owner.command(argv) == ("/operator/strace", *argv)
    with pytest.raises(InvalidInputError):
        owner.command(argv)
    owner.started(1133)
    assert owner.healthy
    assert owner.finish(confirmed_stopped=True)
    assert owner.finish(confirmed_stopped=True)
    assert worker.calls == [("command", argv), ("start", 1133), ("finish", True)]
    assert not owner.healthy


def test_unconfirmed_tree_cannot_have_clean_audit():
    worker = FakeMonitor()
    handle = GuiAuditHandle(worker)
    handle.command(("/bin/true",))
    handle.started(333)
    assert not handle.finish(confirmed_stopped=False)


def test_missing_task_exit_coverage_never_converts_to_success():
    worker = FakeMonitor()
    worker.complete = False
    handle = GuiAuditHandle(worker)
    handle.command(("/bin/true",))
    handle.started(333)
    assert not handle.finish(confirmed_stopped=True)


def test_failed_audit_finalize_never_returns_complete():
    worker = FakeMonitor()
    def broken(*, confirmed_stopped):
        raise RuntimeError("private trace path must not surface")
    worker.finish = broken
    handle = GuiAuditHandle(worker)
    handle.command(("/bin/true",))
    handle.started(33)
    assert not handle.finish(confirmed_stopped=True)


def test_unstarted_monitor_cannot_claim_clean_audit():
    worker = FakeMonitor()
    handle = GuiAuditHandle(worker)
    handle.command(("/bin/true",))
    assert not handle.finish(confirmed_stopped=True)
    with pytest.raises(InvalidInputError):
        handle.started(44)


def test_gui_identity_is_operator_principal_not_fabricated_agent(tmp_path, monkeypatch):
    # Construct the real validation surface without requiring a delegated cgroup.
    from potbot.tools import gui_resource_audit as module

    for name in ("rootfs", "home", "tools", "models", "ai", "past", "scratch", "audit", "cgroup"):
        (tmp_path / name).mkdir()
    (tmp_path / "strace").write_text("exe")
    (tmp_path / "join").write_text("exe")
    monkeypatch.setattr(module, "WorkerMonitorConfig", DummyConfig)
    config = DummyConfig(tmp_path / "audit", tmp_path / "cgroup", tmp_path / "strace", tmp_path / "join")
    workspace, principal = uuid4(), uuid4()
    sessions = []
    def factory(*a, **kw):
        session = FakeMonitor(*a, **kw)
        sessions.append(session)
        return session
    runtime = SimpleNamespace(rootfs=tmp_path / "rootfs", home=tmp_path / "home",
        model_staging=tmp_path / "scratch", shared_tools=tmp_path / "tools",
        shared_models=tmp_path / "models", ai_config_dir=tmp_path / "ai",
        ai_history_dir=tmp_path / "past")
    owner = GuiAuditFactory(config=config, runtime=runtime,
                            workspace_id=workspace, principal_id=principal,
                            session_factory=factory)
    run = uuid4()
    handle = owner("a" * 64, run)
    assert isinstance(handle, GuiAuditHandle)
    assert sessions[0].arguments[1:3] == (workspace, principal)
    assert sessions[0].keywords == {"identity_kind": "principal", "run_id": run}
    for key, name in (("audit_root", "audit"), ("cgroup_root", "cgroup"),
                      ("strace", "strace"), ("cgroup_join", "join")):
        intruder = tmp_path / "home" / name
        if key in {"audit_root", "cgroup_root"}:
            intruder.mkdir()
        else:
            intruder.write_bytes(b"fake")
        values = {field: getattr(config, field)
                  for field in ("audit_root", "cgroup_root", "strace", "cgroup_join")}
        values[key] = intruder
        with pytest.raises(ScopeDeniedError):
            GuiAuditFactory(config=DummyConfig(**values), runtime=runtime,
                            workspace_id=workspace, principal_id=principal,
                            session_factory=factory)


class DummyConfig:
    def __init__(self, audit_root, cgroup_root, strace, cgroup_join):
        self.audit_root, self.cgroup_root = audit_root, cgroup_root
        self.strace, self.cgroup_join = strace, cgroup_join


def test_invalid_process_identity_is_rejected(tmp_path, monkeypatch):
    from potbot.tools import gui_resource_audit as module
    monkeypatch.setattr(module, "WorkerMonitorConfig", DummyConfig)
    for name in ("rootfs", "home", "audit", "cgroup"):
        (tmp_path/name).mkdir()
    for name in ("strace", "join"):
        (tmp_path/name).write_bytes(b"X")
    runtime = SimpleNamespace(rootfs=tmp_path/"rootfs", home=tmp_path/"home",
        shared_tools=None, model_staging=None, shared_models=None,
        ai_config_dir=None, ai_history_dir=None)
    owner = GuiAuditFactory(config=DummyConfig(tmp_path/"audit",tmp_path/"cgroup",tmp_path/"strace",tmp_path/"join"),
                            runtime=runtime, workspace_id=uuid4(), principal_id=uuid4(),
                            session_factory=FakeMonitor)
    for wrong in ("abc", "z"*64, "a"*63):
        with pytest.raises(InvalidInputError):
            owner(wrong, uuid4())
