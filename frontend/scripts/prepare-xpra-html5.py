#!/usr/bin/env python3
"""Build a stripped Potbot window-only HTML5 client from pinned upstream Xpra.

Requires a separate, exact pinned local Xpra-org/xpra-html5 git checkout.
Copies upstream MPL-2.0 assets without changing their copyright. Our adapter
is loaded after the upstream Client class and BEFORE its initial connection.
This is a source-level integration, not a second remote desktop implementation.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

REVISION = "3752e59b7bb3120a5f20794ce754b3b76ac96c4b"
MARK = '<script type="text/javascript" src="js/Client.js"></script>'
CLIENT_HOOKS = (
    "  _new_window(", "  _process_lost_window(",
    "  _process_window_metadata(", "  send_configure_window(",
    "  on_mousemove(", "  on_mousedown(", "  on_mouseup(",
    "  on_mousescroll(", "  _keyb_onkeydown(", "  _keyb_onkeyup(",
    "  release_keys(", "  set_focus(",
)
WINDOW_HOOKS = ("class XpraWindow", "  draw()", "this.draw_canvas")


def check_protocol_hooks(client_js: str, window_js: str) -> None:
    """Fail if an upstream decoder/window lifecycle API moved or was renamed."""
    for member in CLIENT_HOOKS:
        if member not in client_js:
            raise ValueError(f"Xpra client method unavailable: {member.strip()}")
    for member in WINDOW_HOOKS:
        if member not in window_js:
            raise ValueError(f"Xpra canvas method unavailable: {member.strip()}")


def patch_html(source: str) -> str:
    hooks = (
        (MARK, MARK + '\n    <script type="text/javascript" src="potbot-window-bridge.js"></script>'),
        ('const client = new XpraClient("screen");',
         'const client = new XpraClient("screen");\n        window.PotbotXpraBridge.bindClient(client);'),
        ('const path = getstrparam("path", ALLOWED_CHARS + "/") || window.location.pathname;',
         'const path = getstrparam("path", ALLOWED_CHARS + "/?=&") || window.location.pathname;'),
        ('client.connect();', '''client.reconnect = false;
            client.open_url = false;
            client.file_transfer = false;
            client.clipboard_enabled = false;
            client.printing = false;
            client.audio_enabled = false;
            client.remote_logging = false;
            client.on_connect = () => window.PotbotXpraBridge.ready();
            client.callback_close = () => window.PotbotXpraBridge.onClose();
            client.connect();'''),
        ('<div id="screen" style="width: 100%; height: 100%">',
         '<div id="screen" style="width: 1920px; height: 1200px; position: fixed; left: -22000px; top: 0">'),
        ('</head>', '''<style>
      /* Upstream decoding happens offscreen; ONLY Dockview paints visible windows. */
      body { overflow: hidden !important; background: transparent !important; }
      #float_menu, #window_preview, #login-overlay, #progress, #about,
      #sessioninfo, #bugreport, #pasteboard, .notifications, .simple-keyboard,
      .windowhead, .ui-resizable-handle { display: none !important; }
      #screen { pointer-events: none !important; }
    </style>
  </head>'''),
    )
    current = source
    for needle, substitute in hooks:
        if current.count(needle) != 1:
            raise ValueError(f"incompatible upstream Xpra HTML5 source: {needle[:55]}")
        current = current.replace(needle, substitute, 1)
    return current


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("frontend/public/xpra-html5"))
    arguments = parser.parse_args()
    upstream = arguments.upstream.resolve(strict=True)
    result = subprocess.run(["git", "-C", str(upstream), "rev-parse", "HEAD"],
                            capture_output=True, check=True, text=True)
    if result.stdout.strip() != REVISION:
        parser.error(f"expected exact Xpra HTML5 revision {REVISION}")
    dirty = subprocess.run(
        ["git", "-C", str(upstream), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True, check=True, text=True,
    )
    if dirty.stdout.strip():
        parser.error("upstream Xpra HTML5 checkout has uncommitted tracked changes")
    source = upstream / "html5"
    original = source / "index.html"
    license_file = upstream / "LICENSE"
    if not original.is_file() or not license_file.is_file():
        parser.error("upstream HTML5 source or MPL license unavailable")
    check_protocol_hooks(
        (source / "js" / "Client.js").read_text(encoding="utf-8"),
        (source / "js" / "Window.js").read_text(encoding="utf-8"),
    )
    contents = patch_html(original.read_text(encoding="utf-8"))
    output = arguments.output.resolve()
    if output.exists():
        parser.error("client destination exists: remove your old generated vendor directory explicitly")
    shutil.copytree(source, output, symlinks=False)
    (output / "index.html").write_text(contents, encoding="utf-8")
    (output / "potbot-window-bridge.js").write_bytes(
        Path(__file__).resolve().parents[1].joinpath(
            "vendor/xpra-html5/potbot-window-bridge.js"
        ).read_bytes()
    )
    (output / "LICENSE").write_bytes(license_file.read_bytes())
    (output / "UPSTREAM-REVISION").write_text(REVISION + "\n")
    print(f"Prepared Potbot window-only Xpra HTML5 client at {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
