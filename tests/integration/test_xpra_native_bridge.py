"""Real AF_UNIX websocket relay backed by a process fake, not an Xpra decoder.

This covers workspace lifecycle and browser ticket admission. The separate opt-in
acceptance test uses the real Xpra binary and Xdummy from its host packages.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from potbot.tools.x11_workspace_server import WorkspaceXServer, XpraBridgeError, create_app


@pytest.fixture
def fake_xpra(tmp_path: Path) -> Path:
    executable = tmp_path / "fake-xpra"
    executable.write_text("""#!/usr/bin/env python3
import asyncio,os,pathlib,re,signal,socket,sys
import websockets
args=sys.argv[1:]
sockdir=pathlib.Path(next(arg.split('=',1)[1] for arg in args if arg.startswith('--socket-dir=')))
display=next(a[1:] for a in args if re.fullmatch(r':\\d+',a))
pathlib.Path('/tmp/.X11-unix').mkdir(exist_ok=True)
x=socket.socket(socket.AF_UNIX)
xpath='/tmp/.X11-unix/X'+display
x.bind(xpath); x.listen(3)
async def echo(conn):
 async for msg in conn:
  await conn.send(msg)
async def run():
 async with websockets.unix_serve(echo,str(sockdir/'xpra.sock')):
  stop=asyncio.Event()
  asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,stop.set)
  await stop.wait()
asyncio.run(run())
x.close(); pathlib.Path(xpath).unlink(missing_ok=True)
""", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def require_xauth() -> None:
    if shutil.which("xauth") is None:
        pytest.skip("xauth is required for private workspace cookie tests")


def test_xpra_owned_display_and_binary_ws_relay(tmp_path: Path, fake_xpra: Path) -> None:
    require_xauth()
    root = tmp_path / "sandbox"
    root.mkdir()
    workspace = uuid4()
    server = WorkspaceXServer(workspace, root, xpra_executable=str(fake_xpra))
    server.start()
    try:
        assert server.display.startswith(":")
        assert server.socket_path.is_socket()
        assert server.xpra_protocol_socket.is_socket()
        assert server.environment()["DISPLAY"] == server.display
        assert server.sandbox_mount_plan()["mounts"][0]["source"] == str(server.socket_path)
        assert str(server.xpra_protocol_socket) not in json.dumps(server.sandbox_mount_plan())
        assert server._xpra is not None
        command = " ".join(server._xpra.command())
        assert "seamless" in command and "--html=on" in command
        assert "--use-display=no" in command
        assert "--sync-xvfb" not in command
        assert "Xvfb " not in command

        app = create_app(server, "a" * 48, "http://127.0.0.1:3100")
        with TestClient(app) as client:
            assert client.post("/v1/xpra/tickets").status_code == 401
            admission = client.post("/v1/xpra/tickets", headers={"Authorization": "Bearer " + "a" * 48})
            assert admission.status_code == 200
            data = admission.json()
            assert data["workspaceId"] == str(workspace)
            assert data["expiresInSeconds"] == 30
            with client.websocket_connect("/v1/xpra/ws?ticket=" + data["ticket"],
                                          headers={"Origin": "http://127.0.0.1:3100"}) as ws:
                ws.send_bytes(b"\x50\x00Xpra binary frame")
                assert ws.receive_bytes() == b"\x50\x00Xpra binary frame"
            assert not server.consume_ticket(data["ticket"])
        metadata = json.loads(server.session_file.read_text())
        assert metadata["transport"] == "xpra-native-binary"
        assert "xpra.sock" not in server.session_file.read_text()
    finally:
        server.stop()
    assert not server.session_file.exists()
    assert not server.auth_file.exists()


def test_distinct_workspaces_have_distinct_displays_and_tokens(tmp_path: Path, fake_xpra: Path) -> None:
    require_xauth()
    roots = [tmp_path / x for x in ("first", "second")]
    for root in roots:
        root.mkdir()
    servers = [WorkspaceXServer(uuid4(), root, xpra_executable=str(fake_xpra)) for root in roots]
    try:
        for server in servers:
            server.start()
        assert servers[0].display != servers[1].display
        assert servers[0].xpra_protocol_socket != servers[1].xpra_protocol_socket
        ticket = servers[0].ticket()
        assert not servers[1].consume_ticket(ticket)
        assert servers[0].consume_ticket(ticket)
        assert not servers[0].consume_ticket(ticket)
    finally:
        for server in reversed(servers):
            server.stop()


def test_xpra_session_refuses_a_second_owner(tmp_path: Path, fake_xpra: Path) -> None:
    require_xauth()
    root = tmp_path / "single"
    root.mkdir()
    first = WorkspaceXServer(uuid4(), root, xpra_executable=str(fake_xpra))
    second = WorkspaceXServer(uuid4(), root, xpra_executable=str(fake_xpra))
    first.start()
    try:
        with pytest.raises(XpraBridgeError, match="another Xpra session"):
            second.start()
    finally:
        first.stop()
        second.stop()


def test_clean_stop_allows_restarting_the_same_workspace(tmp_path: Path, fake_xpra: Path) -> None:
    require_xauth()
    root = tmp_path / "restart"
    root.mkdir()
    session = WorkspaceXServer(uuid4(), root, xpra_executable=str(fake_xpra))
    first_display = None
    session.start()
    first_display = session.display
    first_socket = session.xpra_protocol_socket
    session.stop()
    assert not first_socket.exists()  # dead Xpra sockets must not block clean restart
    assert not session.session_file.exists()
    assert list((root / ".potbot" / "x11").glob("xpra-*.log"))
    session.start()
    assert session.display.startswith(":")
    session.stop()


def test_rejected_origin_does_not_consume_another_workspaces_capability(
    tmp_path: Path, fake_xpra: Path
) -> None:
    from starlette.websockets import WebSocketDisconnect

    require_xauth()
    root = tmp_path / "rejected"
    root.mkdir()
    server = WorkspaceXServer(uuid4(), root, xpra_executable=str(fake_xpra))
    server.start()
    try:
        app = create_app(server, "c" * 48, "http://127.0.0.1:3100")
        with TestClient(app) as client:
            assert client.get("/v1/xpra/status").status_code == 401
            status = client.get("/v1/xpra/status", headers={"Authorization": "Bearer " + "c" * 48})
            assert status.json() == {
                "workspaceId": str(server.workspace_id), "available": True,
                "transport": "xpra-native-binary", "windowPresentation": "dockview",
            }
            secret = server.ticket()
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("/v1/xpra/ws?ticket=" + secret,
                    headers={"Origin": "https://untrusted.example"}) as connection:
                    connection.receive_bytes()
            # The invalid Origin was rejected before consuming this one-use
            # ticket. The legitimate Origin may still use it exactly once.
            with client.websocket_connect("/v1/xpra/ws?ticket=" + secret,
                headers={"Origin": "http://127.0.0.1:3100"}) as connection:
                connection.send_bytes(b"accept")
                assert connection.receive_bytes() == b"accept"
            assert not server.consume_ticket(secret)
    finally:
        server.stop()


def test_restart_rejects_unknown_socket_directory_files(tmp_path: Path, fake_xpra: Path) -> None:
    """Clean Xpra recovery must never delete another tenant's arbitrary files."""
    require_xauth()
    root = tmp_path / "orphan-safe"
    root.mkdir()
    server = WorkspaceXServer(uuid4(), root, xpra_executable=str(fake_xpra))
    server.start()
    xpra = server._xpra
    assert xpra is not None
    server.stop()
    xpra.socket_dir.mkdir(parents=True, exist_ok=True)
    unknown = xpra.socket_dir / "unrelated-file"
    unknown.write_text("keep this file", encoding="utf-8")
    from potbot.tools.xpra_seamless import XpraSessionError

    with pytest.raises(XpraSessionError, match="operator reconciliation"):
        server.start()
    assert unknown.read_text(encoding="utf-8") == "keep this file"
    unknown.unlink()
    server.start()
    server.stop()
