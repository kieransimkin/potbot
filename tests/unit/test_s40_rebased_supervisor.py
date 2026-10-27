"""S40+S14 integration contracts. Tests do NOT claim live kernel/Xpra qualification."""
from __future__ import annotations

import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.tools.browser_session_leases import BrowserSessionCoordinator
from potbot.tools.isolation import IsolationLaunch
from potbot.tools.xpra_app_sessions import AppRecipe, XpraApplicationSupervisor, _App


class FakeProcess:
    pid = 98765432
    def __init__(self, exited=False):
        self.exit = 0 if exited else None
    def poll(self):
        return self.exit


class FakeChannel:
    def __init__(self):
        self.commands = []
    def call(self, command):
        self.commands.append(command)
        return {"aria": "- button: Save"}
    def close(self):
        pass


def service(*, exited=False):
    workspace, principal, agent = uuid4(), uuid4(), uuid4()
    session = "a" * 32
    app = _App(session, AppRecipe("controlled-browser", "Browser", ("/usr/bin/python3",)),
               FakeProcess(exited), "image-pin")
    app.agent_id = agent
    app.browser_control = FakeChannel()
    app.detached = False
    supervisor = object.__new__(XpraApplicationSupervisor)
    supervisor.workspace_id = workspace
    supervisor.principal_id = principal
    supervisor._lock = threading.RLock()
    supervisor._guard = SimpleNamespace(fenced=False)
    supervisor._apps = {session: app}
    supervisor._group_ids = {session}
    supervisor._stop = threading.Event()
    supervisor.operator_attachment = None
    supervisor.browser_leases = BrowserSessionCoordinator(workspace, principal)
    retired = []
    supervisor.agent_displays = SimpleNamespace(
        environment=lambda value: {"DISPLAY": ":477"},
        retire=lambda value, **opts: retired.append((value, opts)),
        stop_all=lambda: retired.append(("all", {})),
    )
    return supervisor, app, agent, retired


def test_private_agent_session_is_hidden_from_operator_ui():
    host, app, agent, _ = service()
    assert app.view().document()["kind"] == "agent-browser"
    assert host.list_sessions() == []
    assert host._get(app.id) is app  # still available to trusted host control plane
    with pytest.raises(ScopeDeniedError):
        host.attach(app.id)
    with pytest.raises(ScopeDeniedError):
        host.detach(app.id)
    with pytest.raises(ScopeDeniedError):
        host.terminate(app.id)
    with pytest.raises(Exception):
        host.browser_reservation(app.id, "acquire")


def test_agent_lease_scopes_session_and_verified_agent_identity():
    host, app, agent, _ = service()
    ticket = host.agent_browser_reservation(agent, app.id)["token"]
    with pytest.raises(ScopeDeniedError):
        host.browser_command(app.id, {"op": "accessibility"}, lease_token=ticket)
    with pytest.raises(ScopeDeniedError):
        host.browser_command(app.id, {"op": "accessibility"}, lease_token=ticket,
                             actor_agent_id=uuid4())
    data = host.browser_command(app.id, {"op": "accessibility"},
                                lease_token=ticket, actor_agent_id=agent)
    assert data["aria"] == "- button: Save"
    assert len(app.browser_control.commands) == 1
    host.agent_browser_reservation(agent, app.id, "release", ticket)
    with pytest.raises(ScopeDeniedError):
        host.browser_command(app.id, {"op": "accessibility"},
                             lease_token=ticket, actor_agent_id=agent)


def test_private_xpra_retirement_requires_confirmed_stop():
    host, app, agent, retired = service()
    host._retire_agent_display(app)
    assert not retired
    assert app.indeterminate
    with pytest.raises(StateMismatchError):
        host._close_agent_displays()
    app.process.exit = 0
    host._retire_agent_display(app)
    assert retired == [(agent, {"process_stopped": True})]
    host._close_agent_displays()
    assert retired[-1] == ("all", {})


def test_agent_launch_uses_private_pool_not_shared_xpra(monkeypatch):
    host, app, agent, _retired = service(exited=True)
    host._apps = {}
    host._group_ids = set()
    host.max_apps = 4
    host._settle = lambda: None
    called = []
    host.agent_displays.prepare = lambda agent_id, **options: (
        called.append((agent_id, options)) or
        IsolationLaunch(("qualified-bwrap",), (options["status_fd"],), "b" * 64)
    )
    host.backend = SimpleNamespace(server=SimpleNamespace(environment=lambda: {"DISPLAY": ":411"}),
                                   prepare=lambda **_: (_ for _ in ()).throw(AssertionError("shared Xpra")),
                                   runtime=SimpleNamespace(image_digest="a"*64))
    host.recipes = {"controlled-browser": app.recipe}
    host.popen = lambda *args, **kwargs: FakeProcess()
    monkeypatch.setattr("potbot.tools.xpra_browser_control.BrowserControlChannel", lambda process: FakeChannel())
    launched = host.start("controlled-browser", agent_id=agent)
    assert launched["kind"] == "agent-browser"
    assert len(called) == 1 and called[0][0] == agent
    assert host._apps[launched["id"]].agent_id == agent
    with pytest.raises(ScopeDeniedError):
        host.start("terminal", agent_id=agent)


def test_legacy_operator_session_still_allowed():
    host, app, _agent, _ = service()
    app.agent_id = None
    host.agent_displays = None
    assert host.list_sessions()[0]["kind"] == "controlled-browser"
    token = host.browser_reservation(app.id, "acquire")["token"]
    assert host.browser_command(app.id, {"op": "state"}, lease_token=token) == {"aria": "- button: Save"}


def test_host_only_clean_agent_stop_and_reattachment_inventory():
    host, app, agent, _ = service()
    assert host.agent_sessions(agent)[0]["id"] == app.id
    assert host.agent_sessions(uuid4()) == []
    with pytest.raises(ScopeDeniedError):
        host.terminate_agent_browser(uuid4(), app.id)
    host._terminate = lambda current: setattr(current.process, "exit", 0)
    host._settle = lambda: None
    stopped = host.terminate_agent_browser(agent, app.id)
    assert stopped["state"] == "exited"
