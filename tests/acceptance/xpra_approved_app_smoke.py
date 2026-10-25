"""Opt-in *real host* workspace GUI application session smoke check.

Requires an operator-provisioned, pinned rootfs, Xpra Xdummy server, app policy,
and running authenticated bridge. Never falls back to an unconfined host app.
The existing xbill_dock_smoke handles real per-window pixel/control testing.
"""
from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen


def main() -> int:
    if os.environ.get("POTBOT_RUN_XPRA_APPROVED_APPS") != "1":
        print("SKIP: operator-approved Xpra app smoke requires explicit host opt-in")
        return 0
    base = os.environ.get("POTBOT_XPRA_APP_BRIDGE_URL", "")
    token = os.environ.get("POTBOT_XPRA_BRIDGE_TOKEN", "")
    workspace = os.environ.get("POTBOT_XPRA_APP_WORKSPACE_ID", "")
    if (not base.startswith("http://127.0.0.1:") or not base.endswith("/") or
            not token or len(token) < 32 or not workspace):
        raise SystemExit("Refusing unqualified/non-loopback application acceptance target")

    def call(path: str, body: dict | None = None) -> dict:
        payload = json.dumps(body).encode() if body is not None else None
        request = Request(base.rstrip("/") + path, data=payload,
            headers={"Authorization":"Bearer "+token,
                     **({"Content-Type":"application/json"} if body is not None else {})},
            method="POST" if body is not None else "GET")
        with urlopen(request,timeout=10) as response:
            result = json.load(response)
        if result.get("workspaceId") != workspace:
            raise AssertionError("application scope did not match expected workspace")
        return result

    listing=call("/v1/xpra/apps")
    if listing.get("configured") is not True or not any(c["kind"]=="terminal" for c in listing["choices"]):
        raise AssertionError("qualified terminal recipe not configured")
    identifier = None
    try:
        launched=call("/v1/xpra/apps", {"kind":"terminal"})["session"]
        identifier=launched["id"]
        if launched["state"] != "running":
            raise AssertionError("GUI launcher failed to start a sandbox process")
        call(f"/v1/xpra/apps/{identifier}/detach", {})
        found=call("/v1/xpra/apps")["sessions"]
        if not any(item["id"]==identifier and item["state"]=="running" and item["detached"] for item in found):
            raise AssertionError("detach stopped the GUI process")
        call(f"/v1/xpra/apps/{identifier}/attach", {})
        print("PASS: sandbox xterm launch and GUI session detach/reattach are operational")
        return 0
    finally:
        if identifier:
            stopped=call(f"/v1/xpra/apps/{identifier}/terminate", {})["session"]
            if stopped["state"] not in ("exited", "indeterminate"):
                raise AssertionError("operator application did not stop safely")


if __name__ == "__main__":
    raise SystemExit(main())
