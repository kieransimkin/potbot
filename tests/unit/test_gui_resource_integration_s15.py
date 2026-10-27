"""S15 integration: Xpra and Chromium are accounted as principal-owned workers.

These tests use fake launchers; they validate wiring, not a real kernel sandbox.
The accompanying live host acceptance must qualify cgroup/taskstats/strace.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.domain.errors import CapabilityUnavailableError
from potbot.tools.isolation import IsolationLaunch
from potbot.tools.xpra_app_sessions import AppRecipe, XpraApplicationSupervisor


class FakeAudit:
    def __init__(self, identity, run_id):
        self.identity = identity
        self.run_id = run_id
        self.events = []
        self.healthy = True
        self.complete = True

    def command(self, argv):
        self.events.append("command")
        return ("/operator/pinned-cgroup-join", *argv)

    def started(self, pid):
        assert self.events == ["command"]
        self.events.append(("start", pid))

    def finish(self, *, confirmed_stopped):
        self.events.append(("finish", confirmed_stopped))
        return self.complete and confirmed_stopped


class FakeProcess:
    def __init__(self):
        self.pid = 928172
        self.exit_code = None

    def poll(self):
        return self.exit_code


def test_gui_launch_is_wrapped_and_lease_waits_for_kernel_audit(monkeypatch):
    workspace, principal = uuid4(), uuid4()
    processes, monitors, leases = [], [], []

    class Guard:
        fenced = False
        def start(self): leases.append("start")
        def ensure_live(self): pass
        def finish(self, *, evidence_sha256, uncertain=False):
            leases.append(("finish", uncertain))
        def abandon(self): leases.append("abandon")

    fake_supervision = types.SimpleNamespace(
        WorkspaceLeaseGuard=types.SimpleNamespace(exclusive_home=lambda *a, **k: Guard()),
        WorkspaceExecutorIdentity=lambda *a: a,
        admission_digest=lambda x: "f" * 64,
    )
    monkeypatch.setitem(sys.modules, "potbot.tools.workspace_supervision", fake_supervision)

    class Backend:
        server = types.SimpleNamespace(workspace_id=workspace,
                                        environment=lambda: {"DISPLAY": ":412"})
        runtime = types.SimpleNamespace(image_digest="a" * 64)
        def prepare(self, *, argv, status_fd):
            return IsolationLaunch(("/operator/bwrap", *argv), (status_fd,), "b" * 64)

    def launch(*args, **kwargs):
        assert args[0][0] == "/operator/pinned-cgroup-join"
        assert kwargs["start_new_session"] is True
        child = FakeProcess()
        processes.append(child)
        return child

    def audit(identity, run_id):
        monitor = FakeAudit(identity, run_id)
        monitors.append(monitor)
        return monitor

    apps = XpraApplicationSupervisor(
        workspace_id=workspace, principal_id=principal,
        recipes=(AppRecipe("terminal", "Terminal", ("/usr/bin/xterm",)),),
        backend=Backend(), ledger=object(), popen=launch,
        gui_audit_factory=audit,
    )
    try:
        view = apps.start("terminal")
        assert len(monitors) == 1
        assert monitors[0].events == ["command", ("start", processes[0].pid)]
        assert leases == ["start"]
        # No ledger completion while the child is active.
        assert apps.list_sessions()[0]["id"] == view["id"]
        assert leases == ["start"]
        monkeypatch.setattr(apps, "_terminate", lambda app: setattr(app.process, "exit_code", 0))
        apps.terminate(view["id"])
        assert monitors[0].events[-1] == ("finish", True)
        assert leases[-1] == ("finish", False)
    finally:
        apps.shutdown()


def test_failed_gui_audit_cannot_be_reported_as_clean(monkeypatch):
    workspace, principal = uuid4(), uuid4()
    receipts = []

    class Guard:
        fenced = False
        def start(self): pass
        def finish(self, *, evidence_sha256, uncertain=False): receipts.append(uncertain)
        def abandon(self): receipts.append(True)

    monkeypatch.setitem(sys.modules, "potbot.tools.workspace_supervision", types.SimpleNamespace(
        WorkspaceLeaseGuard=types.SimpleNamespace(exclusive_home=lambda *a, **k: Guard()),
        WorkspaceExecutorIdentity=lambda *a: a,
        admission_digest=lambda x: "a" * 64,
    ))

    class Backend:
        server = types.SimpleNamespace(workspace_id=workspace,
                                       environment=lambda: {"DISPLAY": ":412"})
        runtime = types.SimpleNamespace(image_digest="a" * 64)
        def prepare(self, *, argv, status_fd):
            return IsolationLaunch(("/operator/bwrap", *argv), (status_fd,), "b" * 64)

    proc = FakeProcess()
    def audit(identity, run_id):
        m = FakeAudit(identity, run_id)
        m.complete = False
        return m

    apps = XpraApplicationSupervisor(
        workspace_id=workspace, principal_id=principal,
        recipes=(AppRecipe("terminal", "Terminal", ("/usr/bin/xterm",)),),
        backend=Backend(), ledger=object(), popen=lambda *a, **k: proc,
        gui_audit_factory=audit,
    )
    try:
        view = apps.start("terminal")
        monkeypatch.setattr(apps, "_terminate", lambda app: setattr(app.process, "exit_code", 0))
        apps.terminate(view["id"])
        assert receipts[-1] is True
        assert apps.list_sessions()[0]["state"] == "indeterminate"
    finally:
        apps.shutdown()
