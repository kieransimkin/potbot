#!/usr/bin/env python3
"""Opt-in end-to-end: real Xpra server + sandboxed XBill + Next/Dockview.

This is NOT exercised by ordinary unit CI. Requires Xpra >= 6.3 with Xdummy,
Xauthority, bubblewrap, xbill, Next dependencies, the generated upstream HTML5
client, and Python Playwright with a locally installed Chromium. Fails rather
than substituting Xvfb, an unsandboxed app or fake window imagery.
"""
from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / "frontend"


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_ready(url: str, *, token: str | None = None, timeout: float = 50) -> None:
    stop = time.monotonic() + timeout
    while time.monotonic() < stop:
        try:
            headers = {"Authorization": "Bearer " + token} if token else {}
            with httpx.Client(timeout=2, follow_redirects=False, trust_env=False) as client:
                result = client.get(url, headers=headers)
            if result.status_code == 200:
                return
        except (OSError, httpx.HTTPError):
            pass
        time.sleep(.25)
    raise RuntimeError("Xpra/Next service failed its readiness deadline")


def _application_in_sandbox(display: str, authfile: Path, workspace: Path,
                            application: str) -> subprocess.Popen[bytes]:
    if application not in {"xbill", "xterm"}:
        raise RuntimeError("Only the two pinned UI acceptance X applications are admitted")
    bwrap = shutil.which("bwrap")
    binary = shutil.which(application)
    if not bwrap or not binary:
        raise RuntimeError("Xpra acceptance requires real bubblewrap, XBill and xterm binaries")
    socket_path = Path("/tmp/.X11-unix/X" + display.removeprefix(":"))
    if not socket_path.is_socket():
        raise RuntimeError("Xpra did not create the workspace X11 Unix socket")
    cmd = [bwrap, "--die-with-parent", "--unshare-all", "--new-session",
           "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
           "--dir", "/tmp/.X11-unix", "--dir", "/run", "--dir", "/run/potbot"]
    for directory in ("/usr", "/bin", "/lib", "/lib64", "/etc/fonts"):
        if Path(directory).exists():
            cmd += ["--ro-bind", directory, directory]
    cmd += ["--bind", str(socket_path), str(socket_path),
            "--ro-bind", str(authfile), "/run/potbot/Xauthority",
            "--bind", str(workspace), "/workspace", "--chdir", "/workspace",
            "--setenv", "DISPLAY", display,
            "--setenv", "XAUTHORITY", "/run/potbot/Xauthority", "--", binary]
    if application == "xterm":
        cmd.extend(["-title", "Potbot Xterm", "-geometry", "60x14"])

    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


def main() -> int:
    # Explicit opt-in prevents accidentally launching a browser/X server on CI.
    if os.environ.get("POTBOT_RUN_REAL_XPRA_XBILL") != "1":
        raise SystemExit("Set POTBOT_RUN_REAL_XPRA_XBILL=1 for real acceptance")
    if not (FRONT / "public/xpra-html5/UPSTREAM-REVISION").is_file():
        raise SystemExit("Prepare the pinned Xpra HTML5 source before acceptance")
    if not shutil.which("xpra") or not shutil.which("xauth") or not shutil.which("xterm"):
        raise SystemExit("Xpra/Xauthority/xterm dependencies are not installed")
    from playwright.sync_api import sync_playwright  # explicit optional dependency

    bridge_port, front_port, stub_port = _port(), _port(), _port()
    workspace_id = str(uuid4())
    private_password = secrets.token_urlsafe(28)
    bridge_secret = secrets.token_urlsafe(44)
    cookie_key = secrets.token_urlsafe(56)
    origin = f"http://127.0.0.1:{front_port}"
    class ModelsStub(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/v1/models":
                self.send_error(404)
                return
            document = b'{"object":"list","data":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(document)))
            self.end_headers()
            self.wfile.write(document)
        def log_message(self, *_args) -> None:
            pass
    stub = ThreadingHTTPServer(("127.0.0.1", stub_port), ModelsStub)
    server_thread = Thread(target=stub.serve_forever, daemon=True)
    server_thread.start()
    with tempfile.TemporaryDirectory(prefix="potbot-xpra-accept-") as tmp:
        sandbox = Path(tmp) / "workspace"
        sandbox.mkdir()
        state = sandbox / ".potbot/x11/session.json"
        environment = dict(os.environ)
        environment.update({
            "PYTHONPATH": str(ROOT / "src"),
            "POTBOT_XPRA_BRIDGE_TOKEN": bridge_secret,
        })
        bridge_cmd = [sys.executable, "-m", "potbot.tools.x11_workspace_server",
                      "--workspace-id", workspace_id, "--sandbox-root", str(sandbox),
                      "--port", str(bridge_port), "--browser-origin", origin]
        ui_env = dict(os.environ)
        ui_env.update({
            "POTBOT_UI_PUBLIC_ORIGIN": origin,
            "POTBOT_UI_PASSWORD": private_password,
            "POTBOT_UI_COOKIE_SECRET": cookie_key,
            "POTBOT_UI_BACKEND_URL": f"http://127.0.0.1:{stub_port}",
            "POTBOT_UI_BACKEND_TOKEN": secrets.token_urlsafe(24),
            "POTBOT_UI_WORKSPACE_ID": workspace_id,
            "POTBOT_UI_XPRA_BRIDGE_URL": f"http://127.0.0.1:{bridge_port}/",
            "POTBOT_UI_XPRA_BRIDGE_TOKEN": bridge_secret,
            "POTBOT_UI_XPRA_PUBLIC_WS_URL": f"ws://127.0.0.1:{bridge_port}/v1/xpra/ws",
        })
        bridge = subprocess.Popen(bridge_cmd, env=environment, cwd=ROOT,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  start_new_session=True)
        nextjs = None
        game = None
        terminal = None
        try:
            deadline = time.monotonic() + 40
            while not state.exists() and time.monotonic() < deadline and bridge.poll() is None:
                time.sleep(.1)
            _wait_ready(f"http://127.0.0.1:{bridge_port}/v1/xpra/status", token=bridge_secret)
            metadata = json.loads(state.read_text())
            display = metadata["display"]
            game = _application_in_sandbox(display, Path(metadata["xauthority"]), sandbox, "xbill")
            terminal = _application_in_sandbox(display, Path(metadata["xauthority"]), sandbox, "xterm")
            nextjs = subprocess.Popen(["./node_modules/.bin/next", "dev", "--hostname", "127.0.0.1",
                                       "--port", str(front_port)], env=ui_env, cwd=FRONT,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      start_new_session=True)
            _wait_ready(origin + "/")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.goto(origin, wait_until="domcontentloaded")
                page.locator("#operator-password").fill(private_password)
                page.get_by_role("button", name="Unlock workspace").click()
                panel = page.locator(".xwindow-panel").filter(has_text="XBill")
                panel.wait_for(timeout=45000)
                canvas = panel.locator(".xpra-app-canvas")
                canvas.wait_for()
                # Verify a genuinely painted, nonuniform application surface,
                # not merely a canvas with the default 640x480 dimensions.
                page.wait_for_function("""() => {
                  const panel = [...document.querySelectorAll('.xwindow-panel')].find(
                    p => p.textContent?.includes('XBill'));
                  const canvas = panel?.querySelector('.xpra-app-canvas');
                  if (!canvas || canvas.width < 200 || canvas.height < 120) return false;
                  const ctx = canvas.getContext('2d');
                  if (!ctx) return false;
                  const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
                  // Subsample the screen; the XBill game contains distinct
                  // foreground/background colours when actually decoded.
                  const colours = new Set();
                  for (let y = 0; y < canvas.height; y += 17) {
                    for (let x = 0; x < canvas.width; x += 19) {
                      const i = (y * canvas.width + x) * 4;
                      colours.add(`${data[i]}:${data[i + 1]}:${data[i + 2]}`);
                    }
                  }
                  return colours.size >= 3;
                }""", timeout=30000)
                canvas.click(position={"x": 40, "y": 40})
                canvas.press("ArrowRight")
                other = page.locator(".xwindow-panel").filter(has_text="Potbot Xterm")
                other.wait_for(timeout=45000)
                other_canvas = other.locator(".xpra-app-canvas")
                other_canvas.wait_for()
                assert other_canvas.count() == 1
                other_canvas.click(position={"x": 20, "y": 35})
                other_canvas.press("a")
                # App chrome is managed by Dockview, not Xpra's upstream client.
                other.get_by_role("button", name="Minimize application pane").click()
                assert other.count() == 0
                assert terminal.poll() is None, "Minimizing the pane killed the X11 process"
                page.get_by_role("button", name="Manage Xpra application windows").click()
                manager = page.get_by_role("region", name="Workspace Xpra applications")
                manager.wait_for()
                manager.get_by_role("button", name="Minimized").click()
                manager.get_by_role("button", name="Restore Potbot Xterm").click()
                other.wait_for(timeout=20000)
                other_canvas.wait_for()
                assert game.poll() is None and terminal.poll() is None
                screenshot = Path(os.environ.get("POTBOT_XPRA_ACCEPTANCE_SCREENSHOT",
                    str(ROOT / "artifacts/xpra-xbill-dock.png")))
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshot), full_page=False)
                print("Xpra-only XBill appeared in Dockview; input was sent through the native client")
                print("Screenshot:", screenshot)
                browser.close()
        finally:
            for process in (terminal, game, nextjs, bridge):
                if process is not None and process.poll() is None:
                    process.terminate()
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        process.wait(timeout=8)
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=3)
            stub.shutdown()
            stub.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
