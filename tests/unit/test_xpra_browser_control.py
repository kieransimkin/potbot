"""Contract tests without downloading a browser or starting unconfined Chromium."""
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.tools.x11_workspace_server import WorkspaceXServer, create_app
from potbot.tools.xpra_browser_control import BrowserControlChannel
from potbot.tools.xpra_browser_worker import validate_command


@pytest.mark.parametrize("bad", [
    {"op": "cdp", "method": "Runtime.evaluate", "params": {"expression": "document.cookie"}},
    {"op": "cdp", "method": "Target.attachToTarget"},
    {"op": "navigate", "url": "file:///etc/shadow"},
    {"op": "navigate", "url": "javascript:alert(1)"},
    {"op": "navigate", "url": "https://user:secret@example.org"},
    {"op": "input.pointer", "action": "click", "x": -10, "y": 50},
    {"op": "input.keyboard", "action": "type", "text": "x" * 1025},
    {"op": "role.click", "role": "generic", "name": "click anywhere"},
    {"op": "role.click", "role": "button", "name": "", "selector": "*"},
    {"op": "sample", "image": "yes"},
    {"op": "inspect.at", "x": 10, "y": True},
    {"op": "inspect.start", "argv": ["/bin/sh"]},
])
def test_unsafe_commands_denied(bad):
    with pytest.raises(ValueError):
        validate_command(bad)


@pytest.mark.parametrize("good", [
    {"op": "inspect.start"}, {"op": "inspect.poll"}, {"op": "accessibility"},
    {"op": "role.click", "role": "button", "name": "Save"},
    {"op": "role.fill", "role": "textbox", "name": "Email", "text": "example@example.org"},
    {"op": "navigate", "url": "file:///home/fixtures/index.html"},
    {"op": "input.pointer", "action": "wheel", "x": 20, "y": 100, "deltaY": -120},
    {"op": "input.keyboard", "action": "press", "key": "Tab"},
    {"op": "cdp", "method": "CSS.getMatchedStylesForNode", "params": {"nodeId": 23}},
    {"op": "cdp", "method": "Input.dispatchMouseEvent", "params": {"type": "mouseMoved", "x": 20, "y": 30}},
])
def test_safe_operations_admitted(good):
    assert validate_command(good) is good


def test_stdio_rpc_is_bounded_and_serialized():
    # An innocuous fixture process stands in for the admitted browser worker.
    script = """import sys,json
for line in sys.stdin:
 request=json.loads(line)
 print(json.dumps({'id':request['id'],'ok':True,'result':{'op':request['command']['op']}}),flush=True)
"""
    proc = subprocess.Popen([sys.executable, "-u", "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        channel = BrowserControlChannel(proc)
        assert channel.call({"op": "state"}) == {"op": "state"}
        assert channel.call({"op": "inspect.poll"}) == {"op": "inspect.poll"}
        with pytest.raises(InvalidInputError):
            channel.call({"op": "cdp", "method": "Runtime.evaluate"})
        channel.close()
    finally:
        proc.terminate()
        proc.wait(timeout=4)


def test_disconnect_fences_session_and_never_replays():
    script = "import sys; sys.stdin.readline(); sys.exit(0)"
    proc = subprocess.Popen([sys.executable, "-u", "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        channel = BrowserControlChannel(proc)
        with pytest.raises(StateMismatchError):
            channel.call({"op": "role.click", "role": "button", "name": "Submit"}, timeout=2)
        with pytest.raises(StateMismatchError):
            channel.call({"op": "state"}, timeout=1)
    finally:
        proc.wait(timeout=4)


class FakeBrowserApps:
    def browser_command(self, identifier, command):
        if identifier != "a" * 32:
            raise InvalidInputError("unknown")
        return {"picker": True, "action": command["op"]}


def test_bridge_browser_command_is_authenticated_and_bounded(tmp_path):
    server = WorkspaceXServer(uuid4(), tmp_path)
    client = TestClient(create_app(server, "t" * 40, "http://localhost:3100", FakeBrowserApps()))
    endpoint = "/v1/xpra/apps/" + "a" * 32 + "/browser"
    assert client.post(endpoint, json={"op": "inspect.start"}).status_code == 401
    headers = {"Authorization": "Bearer " + "t" * 40}
    assert client.post(endpoint, headers=headers, json={"op": "inspect.start"}).json()["result"]["picker"] is True
    assert client.post(endpoint, headers=headers, json={"op": "inspect.start", "shell": "bash"}).status_code == 409
    assert client.post(endpoint, headers=headers, content=b"z" * 5000).status_code == 413
