"""Bounded browser worker for the *already sandboxed* workspace Xpra display.

Installed as a static file in the qualified rootfs; no Chrome DevTools TCP
listener, XTEST, host browser attachment, arbitrary JS evaluation or shell API.
Playwright/Patchright control their own Chromium subprocess over private pipes.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import hashlib
import json
import os
import sys
from collections import deque
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import unquote, urlsplit, urlunsplit

# The same verified module is packaged with the script in /opt/potbot.
if TYPE_CHECKING:
    from .browser_https_routes import install_https_routes, require_egress_config
else:
    _routes = (
        import_module(".browser_https_routes", __package__)
        if __package__
        else import_module("browser_https_routes")
    )
    install_https_routes = _routes.install_https_routes
    require_egress_config = _routes.require_egress_config

COMMANDS = frozenset(
    {
        "state",
        "navigate",
        "accessibility",
        "interactive",
        "inspect.start",
        "inspect.poll",
        "inspect.stop",
        "inspect.at",
        "input.pointer",
        "input.keyboard",
        "role.click",
        "role.fill",
        "cdp",
        "sample",
    }
)
CDP_READ = frozenset(
    {
        "DOM.getDocument",
        "DOM.describeNode",
        "DOM.getBoxModel",
        "DOM.getNodeForLocation",
        "CSS.getComputedStyleForNode",
        "CSS.getMatchedStylesForNode",
        "Accessibility.getPartialAXTree",
        "Accessibility.getFullAXTree",
        "Page.getLayoutMetrics",
        "Performance.getMetrics",
    }
)
CDP_INPUT = frozenset({"Input.dispatchMouseEvent", "Input.dispatchKeyEvent"})
RULES = {
    "state": set(),
    "accessibility": set(),
    "interactive": set(),
    "inspect.start": set(),
    "inspect.poll": set(),
    "inspect.stop": set(),
    "navigate": {"url"},
    "inspect.at": {"x", "y"},
    "input.pointer": {"action", "x", "y", "deltaY", "button"},
    "input.keyboard": {"action", "key", "text"},
    "role.click": {"role", "name"},
    "role.fill": {"role", "name", "text"},
    "cdp": {"method", "params"},
    "sample": {"image"},
}
REQUIRED = {
    "navigate": {"url"},
    "inspect.at": {"x", "y"},
    "input.pointer": {"action", "x", "y"},
    "input.keyboard": {"action"},
    "role.click": {"role", "name"},
    "role.fill": {"role", "name", "text"},
    "cdp": {"method"},
}
ALLOWED_ROLES = frozenset(
    {
        "button",
        "link",
        "textbox",
        "checkbox",
        "radio",
        "combobox",
        "option",
        "menuitem",
        "tab",
        "switch",
        "searchbox",
    }
)


def browser_operation_may_mutate(command: object) -> bool:
    """Execution-failure outcomes for mutations must be fenced, not retried."""
    return isinstance(command, dict) and (
        command.get("op")
        in {
            "navigate",
            "input.pointer",
            "input.keyboard",
            "role.click",
            "role.fill",
            "inspect.start",
            "inspect.stop",
        }
        or (command.get("op") == "cdp" and command.get("method") in CDP_INPUT)
    )


def _number(value: Any, upper: int = 4096) -> bool:
    return type(value) in (int, float) and 0 <= value <= upper


def _string(value: Any, limit: int = 256) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit and "\x00" not in value


def validate_command(data: Any) -> dict[str, Any]:
    """Reject raw CDP writes, arbitrary scripts, out-of-bounds input and extra keys."""
    if not isinstance(data, dict) or not isinstance(data.get("op"), str):
        raise ValueError("invalid browser command")
    op = data["op"]
    if (
        op not in COMMANDS
        or set(data) - {"op"} - RULES[op]
        or not REQUIRED.get(op, set()) <= set(data)
    ):
        raise ValueError("unapproved browser command")
    if op == "navigate":
        if not _string(data["url"], 2048):
            raise ValueError("invalid browser URL")
        url = urlsplit(data["url"])
        if url.scheme == "file":
            if (
                url.netloc
                or url.query
                or url.fragment
                or not url.path.startswith("/home/")
                or ".." in url.path.split("/")
            ):
                raise ValueError("file URL outside workspace")
        elif url.scheme in ("http", "https"):
            if not url.netloc or url.username or url.password or url.hostname is None:
                raise ValueError("invalid browser origin")
        elif data["url"] != "about:blank":
            raise ValueError("unapproved URL scheme")
    elif op == "inspect.at":
        if not _number(data["x"]) or not _number(data["y"]):
            raise ValueError("invalid viewport coordinates")
    elif op == "input.pointer":
        if data["action"] not in ("move", "click", "wheel") or not all(
            _number(data[k]) for k in ("x", "y")
        ):
            raise ValueError("invalid pointer action")
        if "deltaY" in data and (
            type(data["deltaY"]) not in (int, float) or not -1000 <= data["deltaY"] <= 1000
        ):
            raise ValueError("invalid scroll delta")
        if "button" in data and data["button"] not in ("left", "right", "middle"):
            raise ValueError("invalid pointer button")
    elif op == "input.keyboard":
        if data["action"] not in ("press", "type"):
            raise ValueError("invalid keyboard action")
        if data["action"] == "press" and (
            set(data) != {"op", "action", "key"} or not _string(data["key"], 50)
        ):
            raise ValueError("invalid key")
        if data["action"] == "type" and (
            set(data) != {"op", "action", "text"}
            or not isinstance(data["text"], str)
            or len(data["text"]) > 1024
        ):
            raise ValueError("invalid text")
    elif op in ("role.click", "role.fill"):
        if data["role"] not in ALLOWED_ROLES or not _string(data["name"], 160):
            raise ValueError("invalid accessible locator")
        if op == "role.fill" and (
            data["role"] not in ("textbox", "searchbox", "combobox")
            or not isinstance(data["text"], str)
            or len(data["text"]) > 1024
        ):
            raise ValueError("invalid accessible form action")
    elif op == "cdp":
        method = data["method"]
        params = data.get("params", {})
        if (
            method not in CDP_READ | CDP_INPUT
            or not isinstance(params, dict)
            or len(json.dumps(params)) > 512
        ):
            raise ValueError("CDP method is not on the approved allowlist")
        if method in CDP_READ:
            allowed = {
                "nodeId",
                "backendNodeId",
                "x",
                "y",
                "depth",
                "fetchRelatives",
                "pierce",
                "includeUserAgentShadowDOM",
            }
            if set(params) - allowed or any(
                type(v) not in (int, bool) or (type(v) is int and not -1 <= v <= 100000000)
                for v in params.values()
            ):
                raise ValueError("invalid read-only CDP parameters")
        elif method == "Input.dispatchMouseEvent":
            allowed = {"type", "x", "y", "button", "clickCount", "deltaX", "deltaY"}
            if (
                set(params) - allowed
                or params.get("type")
                not in ("mousePressed", "mouseReleased", "mouseMoved", "mouseWheel")
                or not _number(params.get("x"))
                or not _number(params.get("y"))
                or params.get("button", "none") not in ("none", "left", "right", "middle")
                or type(params.get("clickCount", 0)) is not int
                or not 0 <= params.get("clickCount", 0) <= 2
                or any(
                    type(params.get(k, 0)) not in (int, float)
                    or not -1000 <= params.get(k, 0) <= 1000
                    for k in ("deltaX", "deltaY")
                )
            ):
                raise ValueError("invalid CDP pointer event")
        elif method == "Input.dispatchKeyEvent":
            if (
                set(params) - {"type", "key", "code", "text", "windowsVirtualKeyCode"}
                or params.get("type") not in ("keyDown", "keyUp", "rawKeyDown", "char")
                or any(
                    not isinstance(params.get(k, ""), str) or len(params.get(k, "")) > 64
                    for k in ("key", "code", "text")
                )
                or type(params.get("windowsVirtualKeyCode", 0)) is not int
                or not 0 <= params.get("windowsVirtualKeyCode", 0) <= 255
            ):
                raise ValueError("invalid CDP keyboard event")
    elif op == "sample" and ("image" in data and type(data["image"]) is not bool):
        raise ValueError("invalid sample option")
    return cast(dict[str, Any], data)


def _clip(value: Any, limit: int = 300) -> str:
    return str(value or "")[:limit]


def _safe_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    # Rebuild the authority without userinfo, even following redirects.
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host + (f":{port}" if port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))[:240]


def _ax_node(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": _clip(item.get("role", {}).get("value"), 60),
        "name": _clip(item.get("name", {}).get("value"), 160),
        "ignored": bool(item.get("ignored", False)),
    }


class BrowserWorker:
    def __init__(self, engine: str, stealth: bool) -> None:
        if engine not in ("playwright", "patchright") or type(stealth) is not bool:
            raise ValueError("invalid browser engine configuration")
        self.engine, self.stealth = engine, stealth
        self.playwright: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None
        self.cdp: Any = None
        self._selected: deque[int] = deque(maxlen=4)
        self._errors: deque[str] = deque(maxlen=16)
        self._sheets: dict[str, dict[str, Any]] = {}
        self._scripts: dict[str, dict[str, Any]] = {}
        self._scripts_enabled = False
        self._picker = False

    async def start(self) -> None:
        egress = require_egress_config()
        # Never leak the short-lived CONNECT capability to the Chromium/Node subprocess.
        os.environ.pop("POTBOT_BROWSER_CONNECT_TOKEN_FILE", None)
        os.environ.pop("POTBOT_BROWSER_CONNECT_SOCKET", None)
        # No launch can inherit a host CDP websocket. Browser/driver start inside
        # the namespace; Playwright/Patchright manage Chromium via local pipes.
        if self.engine == "patchright":
            async_playwright = import_module("patchright.async_api").async_playwright
        else:
            async_playwright = import_module("playwright.async_api").async_playwright
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=False,
            executable_path="/usr/bin/chromium",
            timeout=15000,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            accept_downloads=False,
            service_workers="block",
        )
        if self.stealth:
            Stealth = import_module("playwright_stealth").Stealth

            await Stealth().apply_stealth_async(self.context)
        if egress is not None:
            await install_https_routes(self.context, *egress)
        self.page = await self.context.new_page()
        # Activate the newly launched window once, as part of startup. Capture
        # commands never steal focus from an already running browser session.
        await asyncio.wait_for(self.page.bring_to_front(), timeout=3.0)
        self.page.on("requestfailed", lambda request: self._errors.append(_safe_url(request.url)))
        self.cdp = await self.context.new_cdp_session(self.page)
        self.cdp.on("CSS.styleSheetAdded", self._remember_sheet)
        self.cdp.on(
            "CSS.styleSheetRemoved", lambda event: self._sheets.pop(event["styleSheetId"], None)
        )
        self.cdp.on("DOM.documentUpdated", lambda _: self._selected.clear())
        self.cdp.on("Debugger.scriptParsed", self._remember_script)
        self.cdp.on(
            "Overlay.inspectNodeRequested",
            lambda event: self._selected.append(event["backendNodeId"]),
        )
        self.cdp.on("Overlay.inspectModeCanceled", lambda _: setattr(self, "_picker", False))
        await self.cdp.send("DOM.enable")
        await self.cdp.send("CSS.enable")
        await self.cdp.send("Overlay.enable")
        await self.cdp.send("Accessibility.enable")

    def _remember_sheet(self, event: dict[str, Any]) -> None:
        header = event["header"]
        identifier = header["styleSheetId"]
        if identifier not in self._sheets and len(self._sheets) >= 400:
            self._sheets.pop(next(iter(self._sheets)))
        self._sheets[identifier] = {
            "sourceURL": _clip(header.get("sourceURL"), 2048),
            "sourceMapURL": _clip(header.get("sourceMapURL"), 2048),
        }

    def _remember_script(self, event: dict[str, Any]) -> None:
        identifier = event["scriptId"]
        if identifier not in self._scripts and len(self._scripts) >= 400:
            self._scripts.pop(next(iter(self._scripts)))
        self._scripts[identifier] = {
            "url": _clip(event.get("url"), 2048),
            "sourceMapURL": _clip(event.get("sourceMapURL"), 2048),
        }

    async def _script_metadata(self) -> None:
        if not self._scripts_enabled:
            await self.cdp.send("Debugger.enable")
            self._scripts_enabled = True

    async def close(self) -> None:
        try:
            if self.browser is not None:
                await self.browser.close()
        finally:
            try:
                if self.playwright is not None:
                    await self.playwright.stop()
            finally:
                self.browser = self.playwright = self.context = self.page = self.cdp = None
                self._picker = False
                self._selected.clear()
                self._sheets.clear()
                self._scripts.clear()
                self._scripts_enabled = False
                self._errors.clear()

    async def _aria(self, limit: int) -> str:
        # aria_snapshot is a Locator API, not a Page API, in both engines.
        return _clip(await self.page.locator("body").aria_snapshot(timeout=5000), limit)

    async def _stop_picker(self) -> None:
        await self.cdp.send("Overlay.setInspectMode", {"mode": "none", "highlightConfig": {}})
        await self.cdp.send("Overlay.hideHighlight")
        self._picker = False
        self._selected.clear()

    async def inspect(self, backend_id: int) -> dict[str, Any]:
        await self.cdp.send("DOM.getDocument", {"depth": 0})
        pushed = await self.cdp.send(
            "DOM.pushNodesByBackendIdsToFrontend", {"backendNodeIds": [backend_id]}
        )
        node_id = (pushed.get("nodeIds") or [0])[0]
        described = (
            await self.cdp.send("DOM.describeNode", {"backendNodeId": backend_id, "depth": 0})
        )["node"]
        attrs = described.get("attributes", [])
        safe = {
            k: _clip(v, 180)
            for k, v in zip(attrs[::2], attrs[1::2], strict=True)
            if k
            in (
                "id",
                "class",
                "role",
                "aria-label",
                "data-testid",
                "name",
                "data-source",
                "data-file",
                "data-line",
                "data-component",
            )
        }
        result: dict[str, Any] = {
            "tag": _clip(described.get("nodeName"), 50),
            "backendNodeId": backend_id,
            "attributes": safe,
            "nodeId": node_id,
            "styles": [],
            "events": [],
            "accessibility": [],
        }
        ax = await self.cdp.send(
            "Accessibility.getPartialAXTree", {"backendNodeId": backend_id, "fetchRelatives": False}
        )
        result["accessibility"] = [_ax_node(n) for n in ax.get("nodes", [])[:8]]
        if node_id:
            try:
                styles = await self.cdp.send("CSS.getMatchedStylesForNode", {"nodeId": node_id})
                for match in styles.get("matchedCSSRules", [])[:20]:
                    rule = match.get("rule", {})
                    style = rule.get("style", {})
                    source = self._sheets.get(style.get("styleSheetId", ""), {})
                    result["styles"].append(
                        {
                            "selector": _clip(rule.get("selectorList", {}).get("text"), 160),
                            "source": _safe_url(source.get("sourceURL", "")),
                            "sourceMap": _safe_url(source.get("sourceMapURL", "")),
                            "line": (style.get("range") or {}).get("startLine"),
                        }
                    )
                computed = await self.cdp.send("CSS.getComputedStyleForNode", {"nodeId": node_id})
                keys = {
                    "display",
                    "visibility",
                    "position",
                    "z-index",
                    "color",
                    "background-color",
                    "font-size",
                    "font-family",
                    "pointer-events",
                    "opacity",
                }
                result["computed"] = {
                    n["name"]: _clip(n["value"], 100)
                    for n in computed.get("computedStyle", [])
                    if n.get("name") in keys
                }
            except Exception:
                result["stylesUnavailable"] = True
            try:
                box = (await self.cdp.send("DOM.getBoxModel", {"nodeId": node_id}))["model"]
                result["box"] = {k: box[k] for k in ("content", "width", "height") if k in box}
            except Exception:
                pass
        try:
            resolved = await self.cdp.send("DOM.resolveNode", {"backendNodeId": backend_id})
            object_id = resolved.get("object", {}).get("objectId")
            if object_id:
                try:
                    listeners = await self.cdp.send(
                        "DOMDebugger.getEventListeners", {"objectId": object_id, "depth": 0}
                    )
                    result["events"] = [
                        {
                            "type": _clip(e.get("type"), 40),
                            "scriptId": _clip(e.get("scriptId"), 40),
                            "source": _safe_url(
                                self._scripts.get(e.get("scriptId", ""), {}).get("url", "")
                            ),
                            "sourceMap": _safe_url(
                                self._scripts.get(e.get("scriptId", ""), {}).get("sourceMapURL", "")
                            ),
                            "line": e.get("lineNumber"),
                            "column": e.get("columnNumber"),
                        }
                        for e in listeners.get("listeners", [])[:20]
                    ]
                finally:
                    await self.cdp.send("Runtime.releaseObject", {"objectId": object_id})
        except Exception:
            result["eventsUnavailable"] = True
        return result

    async def _capture_jpeg(self) -> bytes:
        """Capture the visible page through the *private* CDP session.

        Direct CDP surface capture avoids Page.screenshot's font-readiness
        helper and the headful view capture path. A completed native capture
        failure may use the view path once. A timeout leaves Chromium's request
        pending, so never overlap it with another capture. Both paths remain
        private reads; neither broadens public CDP authority or retries actions.
        """
        # Let Chromium synchronize its own capture. Page JavaScript (including
        # requestAnimationFrame) can be replaced or suspended by the document;
        # it must not gate this private observation of the compositor.
        # One deadline covers both paths. Do not cancel a cold surface read
        # early merely to reserve time for a fallback that only a completed
        # native error may authorize.
        async with asyncio.timeout(6.0):
            for from_surface in (True, False):
                try:
                    reply = await self.cdp.send(
                        "Page.captureScreenshot",
                        {
                            "format": "jpeg",
                            "quality": 48,
                            "fromSurface": from_surface,
                            "captureBeyondViewport": False,
                        },
                    )
                    break
                except Exception as error:
                    capture_unavailable = type(
                        error
                    ).__module__ == f"{self.engine}._impl._errors" and str(error).endswith(
                        "Protocol error (Page.captureScreenshot): Unable to capture screenshot"
                    )
                    if not from_surface or not capture_unavailable:
                        raise
        encoded = reply.get("data") if isinstance(reply, dict) else None
        if not isinstance(encoded, str) or not 0 < len(encoded) <= 800_000:
            raise ValueError("render sample is absent or exceeds bound")
        try:
            image = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("render sample is not valid base64") from error
        if (
            not 4 <= len(image) <= 600_000
            or not image.startswith(b"\xff\xd8")
            or not image.endswith(b"\xff\xd9")
        ):
            raise ValueError("render sample is not a bounded JPEG")
        return image

    async def execute(self, data: dict[str, Any]) -> dict[str, Any]:
        data = validate_command(data)
        op = data["op"]
        if op == "state":
            return {
                "engine": self.engine,
                "stealth": self.stealth,
                "url": _safe_url(self.page.url),
                "title": _clip(await self.page.title(), 200),
                "picker": self._picker,
                "failedRequests": list(self._errors),
            }
        if op == "navigate":
            url = data["url"]
            if url.startswith("file:"):
                real = Path(unquote(urlsplit(url).path)).resolve(strict=True)
                if not real.is_relative_to(Path("/home")) or not real.is_file():
                    raise ValueError("file outside workspace")
            await self.page.goto(url, wait_until="domcontentloaded", timeout=8000)
            return {"url": _safe_url(self.page.url), "title": _clip(await self.page.title(), 200)}
        if op == "accessibility":
            return {"aria": await self._aria(24000)}
        if op == "interactive":
            data = await self.page.locator(
                "a[href], button, input, select, textarea, [role=button], [role=link], [tabindex]"
            ).evaluate_all(
                "els => els.slice(0, 120).map(e => ({tag:e.tagName.toLowerCase(),role:e.getAttribute('role')||'',name:e.getAttribute('aria-label')||e.innerText?.slice(0,120)||'',label:e.labels?.[0]?.innerText?.slice(0,120)||'',tabIndex:e.tabIndex,disabled:!!e.disabled,box:(()=>{const r=e.getBoundingClientRect();return [r.x,r.y,r.width,r.height]})()}))"
            )
            return {"elements": data}
        if op == "inspect.start":
            await self._script_metadata()
            self._selected.clear()
            await self.cdp.send(
                "Overlay.setInspectMode",
                {
                    "mode": "searchForNode",
                    "highlightConfig": {
                        "showInfo": True,
                        "showStyles": True,
                        "showAccessibilityInfo": True,
                        "contentColor": {"r": 55, "g": 145, "b": 255, "a": 0.17},
                        "borderColor": {"r": 55, "g": 145, "b": 255, "a": 1},
                    },
                },
            )
            self._picker = True
            return {"picker": True}
        if op == "inspect.stop":
            await self._stop_picker()
            return {"picker": False}
        if op == "inspect.poll":
            if not self._selected:
                return {"picker": self._picker, "selected": None}
            node_id = self._selected.pop()
            # Ending the UI state alone does not turn off Chromium's overlay.
            # Stop it before inspecting so subsequent clicks reach the page.
            await self._stop_picker()
            return {"picker": False, "selected": await self.inspect(node_id)}
        if op == "inspect.at":
            await self._script_metadata()
            location = await self.cdp.send(
                "DOM.getNodeForLocation",
                {"x": int(data["x"]), "y": int(data["y"]), "includeUserAgentShadowDOM": False},
            )
            return {"selected": await self.inspect(location["backendNodeId"])}
        if op == "input.pointer":
            x, y = data["x"], data["y"]
            if data["action"] == "wheel":
                await self.page.mouse.move(x, y)
                await self.page.mouse.wheel(0, data.get("deltaY", 200))
            elif data["action"] == "move":
                await self.page.mouse.move(x, y)
            else:
                await self.page.mouse.click(x, y, button=data.get("button", "left"))
            return {"sent": True}
        if op == "input.keyboard":
            if data["action"] == "press":
                await self.page.keyboard.press(data["key"])
            else:
                await self.page.keyboard.insert_text(data["text"])
            return {"sent": True}
        if op in ("role.click", "role.fill"):
            locator = self.page.get_by_role(data["role"], name=data["name"], exact=True)
            if await locator.count() != 1:
                raise ValueError("accessible locator is not unique")
            if op == "role.click":
                await locator.click(timeout=5000)
            else:
                await locator.fill(data["text"], timeout=5000)
            return {"acted": True, "role": data["role"], "name": data["name"]}
        if op == "cdp":
            result = await self.cdp.send(data["method"], data.get("params", {}))
            serialized = json.dumps(result, separators=(",", ":"))
            return {
                "method": data["method"],
                "result": result if len(serialized) <= 100_000 else {"truncated": True},
            }
        if op == "sample":
            jpeg = await self._capture_jpeg()
            frame = {
                "mime": "image/jpeg",
                "sha256": hashlib.sha256(jpeg).hexdigest(),
                "bytes": len(jpeg),
            }
            if data.get("image", True):
                frame["base64"] = base64.b64encode(jpeg).decode("ascii")
            return {
                "frame": frame,
                "aria": await self._aria(12000),
                "url": _safe_url(self.page.url),
                "failedRequests": list(self._errors),
            }
        raise ValueError("unrecognized browser command")


def _unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate browser request key")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError("non-finite browser request number")


async def serve(engine: str, stealth: bool) -> None:
    worker = BrowserWorker(engine, stealth)
    try:
        await worker.start()
        while True:
            # Bound allocation, not just the check after an unbounded read.
            line = await asyncio.to_thread(sys.stdin.buffer.readline, 4097)
            if not line:
                return
            if len(line) > 4096:
                return
            ident: Any = None
            command: dict[str, Any] | None = None
            try:
                request = json.loads(
                    line, object_pairs_hook=_unique_object, parse_constant=_reject_constant
                )
                if (
                    not isinstance(request, dict)
                    or set(request) != {"id", "command"}
                    or type(request["id"]) is not int
                    or not 0 <= request["id"] <= 2**31
                ):
                    raise ValueError("invalid worker envelope")
                ident = request["id"]
                command = validate_command(request["command"])
                value = await asyncio.wait_for(worker.execute(command), timeout=12)
                response = {"id": ident, "ok": True, "result": value}
            except Exception:
                # No raw Playwright exceptions, URLs with secrets or filesystem paths.
                response = {
                    "id": ident,
                    "ok": False,
                    "error": "browser command was rejected or failed",
                    "indeterminate": browser_operation_may_mutate(command),
                }
            encoded = json.dumps(response, separators=(",", ":"), ensure_ascii=True)
            if len(encoded) > 1_500_000:
                encoded = json.dumps(
                    {
                        "id": ident,
                        "ok": False,
                        "error": "browser response exceeded size limit",
                        "indeterminate": browser_operation_may_mutate(command),
                    }
                )
            sys.stdout.write(encoded + "\n")
            sys.stdout.flush()
    finally:
        await worker.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=("playwright", "patchright"), default="playwright")
    parser.add_argument("--stealth", action="store_true")
    args = parser.parse_args()
    asyncio.run(serve(args.engine, args.stealth))


if __name__ == "__main__":
    main()
