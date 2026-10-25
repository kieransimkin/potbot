"""S14: exercise the *existing* Xpra supervisor's owner-loss lease semantics.

No real process, display, browser, controller database or credential is used.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.domain.errors import CapabilityUnavailableError
from potbot.tools.xpra_app_sessions import AppRecipe, XpraApplicationSupervisor, _App


class Process:
    pid = 999999999
    exit_code: int | None = None
    def poll(self) -> int | None:
        return self.exit_code


class FakeAttachment:
    def __init__(self) -> None:
        self.online = True
    def healthy(self) -> bool:
        return self.online
    def require_healthy(self) -> dict[str, bool]:
        if not self.online:
            raise CapabilityUnavailableError("lost operator")
        return {"healthy": True}


class FakeGuard:
    fenced = False
    def __init__(self) -> None:
        self.finished: list[dict[str, object]] = []
    def finish(self, **kwargs: object) -> None:
        self.finished.append(kwargs)
    def abandon(self) -> None:
        raise AssertionError("inconclusive guard termination")


def test_owner_loss_keeps_gui_lease_uncertain(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = uuid4()
    backend = SimpleNamespace(
        server=SimpleNamespace(workspace_id=workspace,
                               environment=lambda: {"DISPLAY": ":421"}),
        backend=SimpleNamespace(operator_attachment=FakeAttachment()),
        runtime=SimpleNamespace(image_digest="b"*64),
    )
    supervisor = XpraApplicationSupervisor(
        workspace_id=workspace, principal_id=uuid4(),
        recipes=(AppRecipe("terminal", "Terminal", ("/usr/bin/xterm",)),),
        backend=backend, ledger=object(), max_apps=1,
    )
    try:
        app_id = "a" * 32
        app = _App(app_id, AppRecipe("terminal", "Terminal", ("/usr/bin/xterm",)),
                   Process(), "isolated-session")
        guard = FakeGuard()
        supervisor._guard = guard
        supervisor._apps[app_id] = app
        supervisor._group_ids.add(app_id)
        monkeypatch.setattr(supervisor, "_terminate",
                            lambda selected: setattr(selected.process, "exit_code", 0))
        supervisor.fence_owner()
        assert app.indeterminate
        assert app.process.poll() == 0
        assert len(guard.finished) == 1 and guard.finished[0]["uncertain"] is True
        assert supervisor._stop.is_set()
    finally:
        supervisor.shutdown()


def test_no_new_browser_command_or_app_launch_after_owner_failure() -> None:
    workspace = uuid4()
    attachment = FakeAttachment()
    attachment.online = False
    backend = SimpleNamespace(
        server=SimpleNamespace(workspace_id=workspace, environment=lambda: {"DISPLAY": ":421"}),
        backend=SimpleNamespace(operator_attachment=attachment),
        runtime=SimpleNamespace(image_digest="b"*64),
    )
    supervisor = XpraApplicationSupervisor(
        workspace_id=workspace, principal_id=uuid4(),
        recipes=(AppRecipe("terminal", "Terminal", ("/usr/bin/xterm",)),),
        backend=backend, ledger=object(), max_apps=1,
    )
    try:
        with pytest.raises(CapabilityUnavailableError): supervisor.start("terminal")
        with pytest.raises(CapabilityUnavailableError): supervisor.browser_command("a"*32, {"op":"inspect"})
        with pytest.raises(CapabilityUnavailableError): supervisor.browser_reservation("a"*32)
    finally:
        supervisor.shutdown()
