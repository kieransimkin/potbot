"""Test the S14 two-phase loopback probe with a real local HTTP listener."""

from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "scripts/qualify_operator_xpra_s14.py"
spec = importlib.util.spec_from_file_location("qualify_operator_xpra_s14", SOURCE)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def test_rejects_outside_loopback_and_credential_urls():
    for value in (
        "https://api.example.net",
        "http://example.net:9988",
        "http://user:pass@127.0.0.1:9988/",
        "http://127.0.0.1:9988/?token=x",
        "http://127.0.0.1",
        "http://127.0.0.1:9988/path",
    ):
        with pytest.raises(ValueError):
            module.loopback_root(value)
    assert module.loopback_root("http://127.0.0.1:9988/") == "http://127.0.0.1:9988"


@pytest.fixture
def upstream():
    ws = uuid4()
    state = {"available": True, "status": 200, "tickets": 200}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, kind):
            if self.headers.get("Authorization") != "Bearer " + "x" * 48:
                self.send_response(401)
                self.end_headers()
                return
            code = state[kind]
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if code == 200:
                self.wfile.write(
                    json.dumps({"workspaceId": str(ws), "available": state["available"]}).encode()
                )

        def do_GET(self):
            self.respond("status")

        def do_POST(self):
            self.respond("tickets")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port), ws, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_healthy_and_fenced_phases(upstream):
    base, ws, state = upstream
    assert module.probe(base, "x" * 48, ws, phase="healthy")["passed"]
    state["status"] = 503
    state["tickets"] = 503
    assert module.probe(base, "x" * 48, ws, phase="fenced")["passed"]
    assert not module.probe(base, "x" * 48, ws, phase="healthy")["passed"]
    state["status"] = 401
    assert not module.probe(base, "x" * 48, ws, phase="fenced")["passed"]


def test_wrong_workspace_and_bad_token_cannot_pass(upstream):
    base, ws, state = upstream
    with pytest.raises(ValueError):
        module.probe(base, "short", ws, phase="healthy")
    with pytest.raises(ValueError):
        module.probe(base, "x" * 48, uuid4(), phase="healthy")
