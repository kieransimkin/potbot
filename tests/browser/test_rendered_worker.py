"""Real Chromium behavior; opt-in, synthetic pages, never a kernel/Xpra certificate.

Set POTBOT_RUN_BROWSER_RENDERED=1 and run under an existing isolated display or
xvfb-run. Missing selected dependencies/display/browser are failures, not skips.
The production worker always requests /usr/bin/chromium. The optional hosted
CI adapter redirects that one launch argument to the installed engine binary;
its identity is recorded, and neither system files nor production permissions
are changed. System mode exercises the ordinary worker entrypoint unchanged.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
import tempfile
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path

import pytest

from potbot.tools.xpra_browser_worker import BrowserWorker
from scripts.browser_rendered_runtime import binary_identity, selected_browser, selection_mode

pytestmark = pytest.mark.skipif(
    os.environ.get("POTBOT_RUN_BROWSER_RENDERED") != "1",
    reason="explicit rendered-browser acceptance was not requested",
)
FIXTURES = Path(__file__).parent / "fixtures"
PROFILES = {
    "playwright": ("playwright", False),
    "patchright": ("patchright", False),
    "playwright-stealth": ("playwright", True),
    "patchright-stealth": ("patchright", True),
}


def profile():
    selected = os.environ.get("POTBOT_BROWSER_LIVE_PROFILE", "playwright")
    if selected not in PROFILES:
        raise ValueError("unknown browser acceptance profile")
    engine, stealth = PROFILES[selected]
    # importlib.metadata.version raises when an explicitly selected package is absent.
    packages = {engine: version(engine)}
    if stealth:
        packages["playwright-stealth"] = version("playwright-stealth")
    return selected, engine, stealth, packages


@pytest.fixture(autouse=True)
def select_rendered_browser():
    with selected_browser(profile()[1]):
        yield


@pytest.fixture
def probe_page():
    # Trusted synthetic content is injected by the TEST harness, not a new
    # worker command. This needs no filesystem or network navigation exception.
    html = (FIXTURES / "probe.html").read_text()
    html = html.replace('<link rel="stylesheet" href="probe.css">', "")
    html = html.replace('<script src="probe.js"></script>', "")
    return html


@asynccontextmanager
async def browser(html):
    _, engine, stealth, _ = profile()
    worker = BrowserWorker(engine, stealth)
    try:
        await worker.start()
        # No fixture resource may fetch anything from the network. This route
        # is only test hygiene, not evidence of the production egress boundary.
        await worker.context.route("**/*", lambda route: route.abort())
        # Commit a real document through the public navigation path. Replacing
        # the initial about:blank document via document.write can leave a
        # headful compositor without its first committed rendering lifecycle.
        await worker.page.route(
            "https://potbot.invalid/probe.html",
            lambda route: route.fulfill(status=200, content_type="text/html", body=html),
        )
        await worker.execute({"op": "navigate", "url": "https://potbot.invalid/probe.html"})
        await worker.page.add_style_tag(
            content=(FIXTURES / "probe.css").read_text()
            + "\n/*# sourceURL=https://potbot.invalid/probe.css */"
        )
        await worker.page.add_script_tag(
            content=(FIXTURES / "probe.js").read_text()
            + "\n//# sourceURL=https://potbot.invalid/probe.js"
        )
        # The worker's bounded capture verifies rendered pixels. A headful
        # document can suspend animation callbacks even after navigation commits.
        print(
            "initial_render_state",
            await worker.page.evaluate(
                "() => ({url: location.href, ready: document.readyState, "
                "visibility: document.visibilityState, focused: document.hasFocus(), "
                "width: innerWidth, height: innerHeight})"
            ),
        )
        yield worker
    finally:
        await worker.close()


def decode_frame(observation):
    frame = observation["frame"]
    image = base64.b64decode(frame["base64"], validate=True)
    assert frame["mime"] == "image/jpeg"
    assert 1000 < len(image) <= 600_000
    assert image.startswith(b"\xff\xd8") and image.endswith(b"\xff\xd9")
    assert len(image) == frame["bytes"]
    assert hashlib.sha256(image).hexdigest() == frame["sha256"]
    return image


async def save_position(worker):
    elements = (await worker.execute({"op": "interactive"}))["elements"]
    button = next(e for e in elements if e["tag"] == "button" and e["name"] == "Save")
    x, y, width, height = button["box"]
    return x + width / 2, y + height / 2


def test_accessibility_role_actions_and_verified_render_change(probe_page, record_property):
    async def scenario():
        async with browser(probe_page) as worker:
            selected, _, _, packages = profile()
            record_property("browser_profile", selected)
            record_property("browser_version", worker.browser.version)
            record_property("packages", json.dumps(packages, sort_keys=True))
            mode = selection_mode()
            executable = (
                worker.playwright.chromium.executable_path
                if mode == "engine"
                else "/usr/bin/chromium"
            )
            record_property(
                "browser_binary", json.dumps(binary_identity(executable, mode), sort_keys=True)
            )
            capture_send = worker.cdp.send

            async def trace_capture(method, params=None):
                started = asyncio.get_running_loop().time()
                try:
                    result = await capture_send(method, params)
                except BaseException as error:
                    if method == "Page.captureScreenshot":
                        print(
                            "capture_attempt",
                            json.dumps(
                                {
                                    "fromSurface": params["fromSurface"],
                                    "outcome": type(error).__name__,
                                    "elapsed_seconds": round(
                                        asyncio.get_running_loop().time() - started, 3
                                    ),
                                }
                            ),
                        )
                    raise
                if method == "Page.captureScreenshot":
                    print(
                        "capture_attempt",
                        json.dumps(
                            {
                                "fromSurface": params["fromSurface"],
                                "outcome": "returned",
                                "elapsed_seconds": round(
                                    asyncio.get_running_loop().time() - started, 3
                                ),
                            }
                        ),
                    )
                return result

            worker.cdp.send = trace_capture
            before = await worker.execute({"op": "sample", "image": True})
            assert 'textbox "Username"' in before["aria"]
            assert 'button "Save"' in (await worker.execute({"op": "accessibility"}))["aria"]
            await worker.execute(
                {
                    "op": "role.fill",
                    "role": "textbox",
                    "name": "Username",
                    "text": "synthetic-probe",
                }
            )
            assert await worker.page.locator("#username").input_value() == "synthetic-probe"
            await worker.execute({"op": "role.click", "role": "button", "name": "Save"})
            assert await worker.page.locator("#count").text_content() == "1"
            after = await worker.execute({"op": "sample", "image": True})
            assert decode_frame(before) != decode_frame(after)
            assert "synthetic-probe" in after["aria"]
            without_pixels = await worker.execute({"op": "sample", "image": False})
            assert "base64" not in without_pixels["frame"]
        # Repeat independent cold starts to expose first-frame display races.
        for _ in range(2):
            async with browser(probe_page) as fresh:
                observation = await fresh.execute({"op": "sample", "image": True})
                assert 'textbox "Username"' in observation["aria"]
                decode_frame(observation)

    asyncio.run(scenario())


def test_picker_click_maps_css_script_and_accessibility_without_activating(probe_page):
    async def scenario():
        async with browser(probe_page) as worker:
            x, y = await save_position(worker)
            await worker.execute({"op": "inspect.start"})
            await worker.execute({"op": "input.pointer", "action": "move", "x": x, "y": y})
            await worker.execute({"op": "input.pointer", "action": "click", "x": x, "y": y})
            async with asyncio.timeout(5):
                while True:
                    result = await worker.execute({"op": "inspect.poll"})
                    if result["selected"] is not None:
                        break
                    await asyncio.sleep(0.025)
            selected = result["selected"]
            assert result["picker"] is False
            assert selected["tag"] == "BUTTON"
            assert selected["attributes"]["id"] == "save"
            assert selected["attributes"]["data-component"] == "SettingsForm"
            assert any(
                a["role"] == "button" and a["name"] == "Save" for a in selected["accessibility"]
            )
            assert any(
                s["source"].endswith("probe.css") and ".action" in s["selector"]
                for s in selected["styles"]
            )
            assert any(
                e["type"] == "click" and e["source"].endswith("probe.js") and type(e["line"]) is int
                for e in selected["events"]
            )
            assert selected["box"]["width"] > 0
            # The picker must consume the selection click, not submit the page.
            assert await worker.page.locator("#count").text_content() == "0"
            # Following pointer input must reach the page after the picker ends.
            await worker.execute({"op": "input.pointer", "action": "click", "x": x, "y": y})
            assert await worker.page.locator("#count").text_content() == "1"
            assert (await worker.execute({"op": "inspect.poll"}))["selected"] is None

    asyncio.run(scenario())


def test_keyboard_pointer_cdp_and_exact_locator_rejection(probe_page):
    async def scenario():
        async with browser(probe_page) as worker:
            await worker.execute({"op": "input.keyboard", "action": "press", "key": "Tab"})
            await worker.execute({"op": "input.keyboard", "action": "type", "text": "typed"})
            assert await worker.page.locator("#username").input_value() == "typed"
            await worker.execute(
                {
                    "op": "cdp",
                    "method": "Input.dispatchKeyEvent",
                    "params": {"type": "char", "text": "!"},
                }
            )
            assert await worker.page.locator("#username").input_value() == "typed!"
            x, y = await save_position(worker)
            for event in ("mousePressed", "mouseReleased"):
                await worker.execute(
                    {
                        "op": "cdp",
                        "method": "Input.dispatchMouseEvent",
                        "params": {
                            "type": event,
                            "x": x,
                            "y": y,
                            "button": "left",
                            "clickCount": 1,
                        },
                    }
                )
            assert await worker.page.locator("#count").text_content() == "1"
            metrics = await worker.execute({"op": "cdp", "method": "Page.getLayoutMetrics"})
            # The scrollbar occupies part of the configured 1280px viewport.
            assert 1200 <= metrics["result"]["cssLayoutViewport"]["clientWidth"] <= 1280
            for bad in (
                {"op": "role.click", "role": "button", "name": "Duplicate"},
                {
                    "op": "cdp",
                    "method": "Runtime.evaluate",
                    "params": {"expression": "document.getElementById('save').click()"},
                },
            ):
                with pytest.raises(ValueError):
                    await worker.execute(bad)
            assert await worker.page.locator("#count").text_content() == "1"
            await worker.execute(
                {"op": "input.pointer", "action": "wheel", "x": 200, "y": 400, "deltaY": 500}
            )
            async with asyncio.timeout(3):
                while True:
                    scrolled = await worker.execute(
                        {"op": "cdp", "method": "Page.getLayoutMetrics"}
                    )
                    if scrolled["result"]["cssLayoutViewport"]["pageY"] > 0:
                        break
                    await asyncio.sleep(0.025)

    asyncio.run(scenario())


def test_two_real_browser_contexts_do_not_share_form_or_picker_state(probe_page):
    async def scenario():
        async with browser(probe_page) as first, browser(probe_page) as second:
            await first.execute(
                {
                    "op": "role.fill",
                    "role": "textbox",
                    "name": "Username",
                    "text": "first-context-only",
                }
            )
            await first.execute({"op": "inspect.start"})
            assert (await first.execute({"op": "state"}))["picker"] is True
            assert (await second.execute({"op": "state"}))["picker"] is False
            assert await second.page.locator("#username").input_value() == ""
            await first.close()
            await second.execute({"op": "role.click", "role": "button", "name": "Save"})
            assert await second.page.locator("#count").text_content() == "1"
            decode_frame(await second.execute({"op": "sample", "image": True}))

    asyncio.run(scenario())


def test_real_worker_stdio_rejects_duplicate_and_nonfinite_frames():
    async def scenario():
        _, engine, stealth, _ = profile()
        entrypoint = (
            "scripts.browser_rendered_runtime"
            if selection_mode() == "engine"
            else "potbot.tools.xpra_browser_worker"
        )
        args = [sys.executable, "-m", entrypoint, "--engine", engine]
        if stealth:
            args.append("--stealth")
        # No raw browser diagnostics are published as acceptance artifacts.
        with tempfile.TemporaryFile() as diagnostics:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=diagnostics,
                limit=1_500_001,
            )
            try:

                async def raw(message):
                    process.stdin.write(message + b"\n")
                    await process.stdin.drain()
                    response = await asyncio.wait_for(process.stdout.readline(), timeout=25)
                    assert response, "browser worker exited before its RPC response"
                    return json.loads(response)

                async def call(identifier, command):
                    answer = await raw(json.dumps({"id": identifier, "command": command}).encode())
                    assert answer["id"] == identifier and answer["ok"] is True
                    return answer["result"]

                await call(1, {"op": "navigate", "url": "about:blank"})
                assert isinstance((await call(2, {"op": "accessibility"}))["aria"], str)
                for message in (
                    b'{"id":3,"id":4,"command":{"op":"state"}}',
                    b'{"id":5,"command":{"op":"state","op":"navigate","url":"about:blank"}}',
                    b'{"id":6,"command":{"op":"input.pointer","action":"wheel","x":0,"y":0,"deltaY":NaN}}',
                ):
                    answer = await raw(message)
                    assert answer["ok"] is False and answer["indeterminate"] is False
                state = await call(7, {"op": "state"})
                assert state["url"] == "about:blank"
                decode_frame(await call(8, {"op": "sample", "image": True}))
                # Overlong unterminated input must end the channel, not await
                # unlimited attacker bytes until a newline eventually arrives.
                process.stdin.write(b"x" * 4097)
                await process.stdin.drain()
                assert await asyncio.wait_for(process.wait(), timeout=10) == 0
            finally:
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except TimeoutError:
                        process.kill()
                        await process.wait()

    asyncio.run(scenario())
