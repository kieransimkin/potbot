"""Workspace-scoped Xpra-only X server and authenticated binary WebSocket relay.

One Xpra seamless X11 server per workspace/sandbox, with the X socket and
Xauthority supplied to admitted processes. This process never starts Xvfb,
uses XTEST, captures PNG frames, or exposes a desktop framebuffer.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hmac
import json
import os
import secrets
import subprocess
import threading
import time
from pathlib import Path
from uuid import UUID

import websockets
from fastapi import FastAPI, Header, HTTPException, WebSocket
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect

from potbot.tools.xpra_seamless import XpraSeamlessSession


class XpraBridgeError(RuntimeError):
    """Xpra-only session or transport not ready."""


class WorkspaceXServer:
    """Own display allocation, Xpra process and one-time browser stream tickets."""

    def __init__(self, workspace_id: UUID, sandbox_root: Path, *,
                 display: int | None = None, xpra_executable: str = "xpra") -> None:
        if not isinstance(workspace_id, UUID):
            raise ValueError("workspace ID must be a UUID")
        selected = Path(sandbox_root)
        root = selected.resolve(strict=True)
        if not root.is_dir() or selected.is_symlink():
            raise ValueError("sandbox root must be a provisioned directory")
        if display is not None and (type(display) is not int or not 100 <= display <= 999):
            raise ValueError("invalid workspace display number")
        self.workspace_id, self.sandbox_root = workspace_id, root
        self.display_number = display
        self.xpra_executable = xpra_executable
        self.auth_file = root / ".potbot" / "x11" / "Xauthority"
        self.session_file = root / ".potbot" / "x11" / "session.json"
        self._xpra: XpraSeamlessSession | None = None
        self._runtime_lock: int | None = None
        self._display_lock: int | None = None
        self._owned_session = False
        self._owned_auth = False
        self._tickets: dict[str, float] = {}
        self._ticket_lock = threading.Lock()

    @property
    def display(self) -> str:
        if self.display_number is None:
            raise XpraBridgeError("workspace Xpra server is not running")
        return f":{self.display_number}"

    @property
    def socket_path(self) -> Path:
        if self.display_number is None:
            raise XpraBridgeError("workspace Xpra display not allocated")
        return Path(f"/tmp/.X11-unix/X{self.display_number}")

    def _claim_display(self) -> int:
        root = Path(f"/tmp/potbot-x-display-locks-{os.getuid()}")
        if root.is_symlink():
            raise XpraBridgeError("display lock root is symlinked")
        root.mkdir(mode=0o700, exist_ok=True)
        if root.stat().st_uid != os.getuid():
            raise XpraBridgeError("display lock owner mismatch")
        for number in ([self.display_number] if self.display_number else range(410, 950)):
            assert number is not None
            fd = os.open(root / f"X{number}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if Path(f"/tmp/.X11-unix/X{number}").exists():
                    os.close(fd)
                    continue
                self._display_lock = fd
                return number
            except BlockingIOError:
                os.close(fd)
        raise XpraBridgeError("no private X11 display available")

    def start(self) -> None:
        if self._xpra is not None:
            raise XpraBridgeError("workspace Xpra session already started")
        runtime = self.auth_file.parent
        if runtime.is_symlink() or self.auth_file.is_symlink() or self.session_file.is_symlink():
            raise XpraBridgeError("workspace runtime is symlinked")
        runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(runtime, 0o700)
        self._runtime_lock = os.open(runtime / "runtime.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self._runtime_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._runtime_lock)
            self._runtime_lock = None
            raise XpraBridgeError("another Xpra session owns this workspace") from None
        try:
            self.display_number = self._claim_display()
            if self.session_file.exists():
                raise XpraBridgeError("stale workspace Xpra session requires reconciliation")
            self.auth_file.unlink(missing_ok=True)
            result = subprocess.run([
                "xauth", "-f", str(self.auth_file), "add", self.display,
                "MIT-MAGIC-COOKIE-1", secrets.token_hex(16),
            ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=5, check=False)
            if result.returncode != 0:
                raise XpraBridgeError("cannot provision workspace Xauthority")
            os.chmod(self.auth_file, 0o600)
            self._owned_auth = True
            self._xpra = XpraSeamlessSession(self.sandbox_root, self.display,
                                              self.auth_file, binary=self.xpra_executable)
            self._xpra.start()
            payload = json.dumps({
                "schema_version": 3, "workspace_id": str(self.workspace_id),
                "display": self.display, "socket_path": str(self.socket_path),
                "xauthority": str(self.auth_file),
                "transport": "xpra-native-binary", "frame_transport": "xpra-html5",
            })
            fd = os.open(self.session_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            self._owned_session = True
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            self.stop()
            raise

    def environment(self) -> dict[str, str]:
        if self._xpra is None or not self._xpra.running or not self.socket_path.is_socket():
            raise XpraBridgeError("workspace Xpra server is not running")
        return {"DISPLAY": self.display, "XAUTHORITY": str(self.auth_file),
                "POTBOT_X11_SOCKET": str(self.socket_path)}

    def sandbox_mount_plan(self) -> dict[str, object]:
        self.environment()
        return {"workspace_id": str(self.workspace_id), "mounts": [
            {"source": str(self.socket_path), "target": str(self.socket_path), "readonly": False},
            {"source": str(self.auth_file), "target": "/run/potbot/Xauthority", "readonly": True},
        ], "environment": {"DISPLAY": self.display, "XAUTHORITY": "/run/potbot/Xauthority"},
            "isolation_profile_required": "x11-workspace-private-unix-v1",
            "automatically_admitted": False}

    @property
    def xpra_protocol_socket(self) -> Path:
        self.environment()
        assert self._xpra is not None
        return self._xpra.protocol_socket

    def ticket(self) -> str:
        self.environment()
        secret = secrets.token_urlsafe(32)
        with self._ticket_lock:
            now = time.monotonic()
            self._tickets = {k: t for k, t in self._tickets.items() if t > now}
            if len(self._tickets) >= 64:
                raise XpraBridgeError("Xpra ticket admission limit reached")
            self._tickets[secret] = now + 30
        return secret

    def consume_ticket(self, secret: str) -> bool:
        if not isinstance(secret, str) or len(secret) > 128:
            return False
        with self._ticket_lock:
            deadline = self._tickets.pop(secret, 0)
        return deadline > time.monotonic()

    def stop(self) -> None:
        with self._ticket_lock:
            self._tickets.clear()
        xpra, self._xpra = self._xpra, None
        if xpra is not None:
            xpra.stop()
        if self._owned_session:
            self.session_file.unlink(missing_ok=True)
            self._owned_session = False
        if self._owned_auth:
            self.auth_file.unlink(missing_ok=True)
            self._owned_auth = False
        for name in ("_runtime_lock", "_display_lock"):
            fd = getattr(self, name)
            if fd is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
                setattr(self, name, None)


def create_app(server: WorkspaceXServer, token: str, allowed_origin: str) -> FastAPI:
    """Private HTTP admission and one-use WebSocket relay to Xpra over AF_UNIX."""
    if not isinstance(token, str) or len(token) < 32 or "\n" in token:
        raise ValueError("Xpra bridge needs a private service bearer")
    if not allowed_origin.startswith(("http://127.0.0.1:", "http://localhost:", "https://")):
        raise ValueError("Xpra browser origin must be explicitly trusted")
    app = FastAPI(title="Potbot Xpra Workspace Relay", docs_url=None, redoc_url=None,
                  openapi_url=None)

    def authenticate(authorization: str | None) -> None:
        if authorization is None or not hmac.compare_digest(authorization, "Bearer " + token):
            raise HTTPException(401, "Xpra service authentication required")

    @app.get("/v1/xpra/status")
    def status(authorization: str | None = Header(default=None)) -> dict[str, object]:
        authenticate(authorization)
        server.environment()
        return {"workspaceId": str(server.workspace_id), "available": True,
                "transport": "xpra-native-binary", "windowPresentation": "dockview"}

    @app.post("/v1/xpra/tickets")
    def tickets(authorization: str | None = Header(default=None)) -> JSONResponse:
        authenticate(authorization)
        return JSONResponse({"workspaceId": str(server.workspace_id), "ticket": server.ticket(),
                             "expiresInSeconds": 30},
                            headers={"Cache-Control": "private, no-store"})

    @app.websocket("/v1/xpra/ws")
    async def websocket_bridge(ws: WebSocket) -> None:
        if ws.headers.get("origin") != allowed_origin or not server.consume_ticket(
            ws.query_params.get("ticket", "")
        ):
            await ws.close(code=4403)
            return
        try:
            unix_socket = str(server.xpra_protocol_socket)
            upstream = await websockets.unix_connect(
                unix_socket, uri="ws://localhost/", open_timeout=5,
                max_size=8 * 1024 * 1024, max_queue=4, compression=None,
            )
        except (OSError, RuntimeError, TimeoutError):
            await ws.close(code=1011)
            return
        await ws.accept()
        async def browser_to_xpra() -> None:
            while True:
                incoming = await ws.receive()
                if incoming["type"] == "websocket.disconnect":
                    break
                blob = incoming.get("bytes")
                if not isinstance(blob, bytes) or len(blob) > 8 * 1024 * 1024:
                    break
                await upstream.send(blob)
        async def xpra_to_browser() -> None:
            async for packet in upstream:
                if not isinstance(packet, bytes) or len(packet) > 8 * 1024 * 1024:
                    break
                await ws.send_bytes(packet)
        try:
            tasks = [asyncio.create_task(browser_to_xpra()), asyncio.create_task(xpra_to_browser())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.exception()  # consume task exceptions
        except (WebSocketDisconnect, OSError, RuntimeError):
            pass
        finally:
            await upstream.close()
            try:
                await ws.close()
            except RuntimeError:
                pass
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-id", type=UUID, required=True)
    parser.add_argument("--sandbox-root", type=Path, required=True)
    parser.add_argument("--display", type=int)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--xpra-binary", default="xpra")
    parser.add_argument("--browser-origin", required=True)
    args = parser.parse_args(argv)
    token = os.environ.get("POTBOT_XPRA_BRIDGE_TOKEN", "")
    server = WorkspaceXServer(args.workspace_id, args.sandbox_root, display=args.display,
                              xpra_executable=args.xpra_binary)
    app = create_app(server, token, args.browser_origin)
    import uvicorn
    try:
        server.start()
        print(json.dumps({"workspaceId": str(server.workspace_id), "display": server.display,
                          "transport": "xpra-native-binary", "screenShared": False}), flush=True)
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
