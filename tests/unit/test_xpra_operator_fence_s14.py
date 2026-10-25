"""S14 owner fence exercises real threads but no Xpra/system processes."""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
import types
from pathlib import Path
from uuid import uuid4

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "src/potbot/tools/xpra_operator_fence_s14.py"


@pytest.fixture
def types_fixture(monkeypatch):
    # Small dependency stubs let this standalone test run without cloning
    # Potbot's unrelated PostgreSQL, model, and frontend packages.
    for name in ("potbot", "potbot.domain", "potbot.tools"):
        module = types.ModuleType(name)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
    errors = types.ModuleType("potbot.domain.errors")
    errors.CapabilityUnavailableError = type("CapabilityUnavailableError", (Exception,), {})
    errors.InvalidInputError = type("InvalidInputError", (ValueError,), {})
    errors.ScopeDeniedError = type("ScopeDeniedError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "potbot.domain.errors", errors)
    attachment_module = types.ModuleType("potbot.tools.operator_attachment_s13")

    class OperatorAttachmentClient:
        def __init__(self, workspace_id):
            self.workspace_id = workspace_id
            self.online = True
            self.epoch = "a"
            self.previous_epoch = None
            self.calls = 0

        def require_healthy(self):
            self.calls += 1
            if not self.online or (
                self.previous_epoch is not None and self.epoch != self.previous_epoch
            ):
                raise errors.CapabilityUnavailableError("unhealthy")
            self.previous_epoch = self.epoch
            return {"healthy": True, "epoch": self.epoch}

    attachment_module.OperatorAttachmentClient = OperatorAttachmentClient
    monkeypatch.setitem(sys.modules, attachment_module.__name__, attachment_module)
    spec = importlib.util.spec_from_file_location("potbot.tools.xpra_operator_fence_s14", SOURCE)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, errors, OperatorAttachmentClient


class Display:
    def __init__(self, workspace_id):
        self.workspace_id = workspace_id
        self.calls = 0

    def stop(self):
        self.calls += 1


class Apps:
    def __init__(self):
        self.calls = 0

    def fence_owner(self):
        self.calls += 1


def test_launch_requires_matching_pinned_workspace(types_fixture):
    module, errors, Client = types_fixture
    with pytest.raises(errors.ScopeDeniedError):
        module.XpraOperatorFence(attachment=Client(uuid4()), display=Display(uuid4()))
    with pytest.raises(errors.ScopeDeniedError):
        module.XpraOperatorFence(attachment=object(), display=Display(uuid4()))
    ws = uuid4()
    with pytest.raises(errors.InvalidInputError):
        module.XpraOperatorFence(
            attachment=Client(ws), display=Display(ws), check_interval_seconds=0
        )


def test_healthy_session_keeps_display(types_fixture):
    module, _, Client = types_fixture
    ws = uuid4()
    client = Client(ws)
    display = Display(ws)
    apps = Apps()
    with module.XpraOperatorFence(
        attachment=client, display=display, applications=apps, check_interval_seconds=0.05
    ) as fencer:
        assert fencer.require_healthy()["epoch"] == "a"
        time.sleep(0.07)
        assert not fencer.fenced
    assert display.calls == apps.calls == 0


def test_owner_failure_stops_apps_and_display_once(types_fixture):
    module, errors, Client = types_fixture
    ws = uuid4()
    client = Client(ws)
    display = Display(ws)
    apps = Apps()
    fencer = module.XpraOperatorFence(
        attachment=client, display=display, applications=apps, check_interval_seconds=0.05
    ).start()
    client.online = False
    for _ in range(40):
        if fencer.fenced:
            break
        time.sleep(0.015)
    assert fencer.fenced
    fencer.close()
    assert apps.calls == display.calls == 1
    assert not fencer.healthy()
    with pytest.raises(errors.CapabilityUnavailableError):
        fencer.start()
    assert apps.calls == display.calls == 1


def test_restart_epoch_is_not_automatically_adopted(types_fixture):
    module, errors, Client = types_fixture
    ws = uuid4()
    client = Client(ws)
    display = Display(ws)
    fencer = module.XpraOperatorFence(attachment=client, display=display)
    fencer.require_healthy()
    client.epoch = "b"
    with pytest.raises(errors.CapabilityUnavailableError):
        fencer.require_healthy()
    assert fencer.fenced and display.calls == 1


def test_shutdown_error_does_not_reopen_admission(types_fixture):
    module, errors, Client = types_fixture
    ws = uuid4()
    client = Client(ws)
    display = Display(ws)

    class FailingApps:
        def fence_owner(self):
            raise RuntimeError("unexpected failure")

    fencer = module.XpraOperatorFence(
        attachment=client, display=display, applications=FailingApps()
    )
    client.online = False
    with pytest.raises(errors.CapabilityUnavailableError):
        fencer.require_healthy()
    assert fencer.fenced
    assert display.calls == 1


def test_concurrent_health_requests_only_teardown_once(types_fixture):
    module, _, Client = types_fixture
    ws = uuid4()
    client = Client(ws)
    display = Display(ws)
    apps = Apps()
    fencer = module.XpraOperatorFence(attachment=client, display=display, applications=apps)
    client.online = False
    threads = [threading.Thread(target=fencer.healthy) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert fencer.fenced and apps.calls == display.calls == 1
