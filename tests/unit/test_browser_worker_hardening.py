"""Worker lifecycle/wire contracts; these do not qualify OS or agent isolation."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from potbot.tools import xpra_browser_worker as module
from potbot.tools.xpra_browser_worker import BrowserWorker, validate_command


@pytest.mark.parametrize("delta", [float("nan"), float("inf"), -float("inf"), True, 1001])
def test_pointer_and_cdp_reject_nonfinite_or_non_numeric_wheel_deltas(delta):
    for command in (
        {"op": "input.pointer", "action": "wheel", "x": 1, "y": 2, "deltaY": delta},
        {"op": "cdp", "method": "Input.dispatchMouseEvent", "params": {
            "type": "mouseWheel", "x": 1, "y": 2, "deltaY": delta,
        }},
    ):
        with pytest.raises(ValueError):
            validate_command(command)


@pytest.mark.parametrize("count", [True, False, "1", 1.0, -1, 3])
def test_cdp_click_count_is_a_bounded_integer(count):
    with pytest.raises(ValueError):
        validate_command({"op": "cdp", "method": "Input.dispatchMouseEvent", "params": {
            "type": "mousePressed", "x": 1, "y": 2, "clickCount": count,
        }})


@pytest.mark.parametrize(("engine", "stealth"), [("other", False), ("playwright", 1)])
def test_engine_configuration_fails_closed(engine, stealth):
    with pytest.raises(ValueError):
        BrowserWorker(engine, stealth)


def test_direct_execute_validates_before_touching_browser():
    worker = BrowserWorker("playwright", False)
    with pytest.raises(ValueError, match="allowlist"):
        asyncio.run(worker.execute({"op": "cdp", "method": "Runtime.evaluate"}))
    assert worker.cdp is None


def test_accessibility_uses_body_locator_not_nonexistent_page_api():
    locator = SimpleNamespace(aria_snapshot=AsyncMock(return_value='- button "Save"'))
    calls = []

    def locate(selector):
        calls.append(selector)
        return locator

    worker = BrowserWorker("playwright", False)
    worker.page = SimpleNamespace(locator=locate)
    assert asyncio.run(worker.execute({"op": "accessibility"})) == {"aria": '- button "Save"'}
    assert calls == ["body"]
    locator.aria_snapshot.assert_awaited_once_with(timeout=5000)


def test_cleanup_stops_driver_even_when_browser_close_fails():
    worker = BrowserWorker("playwright", False)
    driver = SimpleNamespace(stop=AsyncMock())
    worker.playwright = driver
    worker.browser = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("fixture")))
    worker._picker = True
    worker._selected.append(2)
    worker._scripts["1"] = {"url": "fixture"}
    with pytest.raises(RuntimeError, match="fixture"):
        asyncio.run(worker.close())
    driver.stop.assert_awaited_once()
    assert worker.browser is worker.playwright is worker.cdp is worker.page is None
    assert not worker._selected and not worker._scripts and not worker._picker
    asyncio.run(worker.close())
    driver.stop.assert_awaited_once()


def test_serve_cleans_up_a_partially_started_worker(monkeypatch):
    worker = SimpleNamespace(
        start=AsyncMock(side_effect=RuntimeError("partial startup")), close=AsyncMock(),
    )
    monkeypatch.setattr(module, "BrowserWorker", lambda *_: worker)
    with pytest.raises(RuntimeError, match="partial startup"):
        asyncio.run(module.serve("playwright", False))
    worker.close.assert_awaited_once()


def test_picker_state_changes_only_after_protocol_success():
    worker = BrowserWorker("playwright", False)
    worker._scripts_enabled = True
    worker.cdp = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("overlay unavailable")))
    with pytest.raises(RuntimeError):
        asyncio.run(worker.execute({"op": "inspect.start"}))
    assert not worker._picker


def test_picker_stop_sends_complete_protocol_parameters_before_releasing_state():
    worker = BrowserWorker("playwright", False)
    worker.cdp = SimpleNamespace(send=AsyncMock(return_value={}))
    worker._picker = True
    worker._selected.append(7)
    assert asyncio.run(worker.execute({"op": "inspect.stop"})) == {"picker": False}
    assert worker.cdp.send.await_args_list[0].args == (
        "Overlay.setInspectMode", {"mode": "none", "highlightConfig": {}},
    )
    assert worker.cdp.send.await_args_list[1].args == ("Overlay.hideHighlight",)
    assert not worker._selected


def test_inspection_releases_remote_object_when_listener_collection_fails():
    worker = BrowserWorker("playwright", False)
    commands = []

    async def send(method, params=None):
        commands.append((method, params))
        if method == "DOM.pushNodesByBackendIdsToFrontend":
            return {"nodeIds": [0]}
        if method == "DOM.describeNode":
            return {"node": {"nodeName": "BUTTON", "attributes": []}}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "opaque"}}
        if method == "DOMDebugger.getEventListeners":
            raise RuntimeError("fixture listener failure")
        return {}

    worker.cdp = SimpleNamespace(send=send)
    assert asyncio.run(worker.inspect(7))["eventsUnavailable"] is True
    assert commands[-1] == ("Runtime.releaseObject", {"objectId": "opaque"})


def test_css_and_script_caches_retain_only_bounded_source_metadata():
    worker = BrowserWorker("playwright", False)
    for number in range(450):
        worker._remember_sheet({"header": {
            "styleSheetId": str(number), "sourceURL": "a" * 9000,
            "sourceMapURL": "b" * 9000, "unneeded": "c" * 9000,
        }})
        worker._remember_script({
            "scriptId": str(number), "url": "a" * 9000,
            "sourceMapURL": "b" * 9000, "stackTrace": {"large": "c" * 9000},
        })
    assert len(worker._sheets) == len(worker._scripts) == 400
    assert "0" not in worker._sheets and "0" not in worker._scripts
    assert set(worker._sheets["449"]) == {"sourceURL", "sourceMapURL"}
    assert set(worker._scripts["449"]) == {"url", "sourceMapURL"}
    assert all(len(v) <= 2048 for v in worker._sheets["449"].values())
    assert all(len(v) <= 2048 for v in worker._scripts["449"].values())
    worker._remember_script({"scriptId": "449", "url": "updated"})
    worker._remember_sheet({"header": {"styleSheetId": "449", "sourceURL": "updated"}})
    assert len(worker._scripts) == len(worker._sheets) == 400


class BoundedInput(io.BytesIO):
    def __init__(self, content):
        super().__init__(content)
        self.sizes = []

    def readline(self, size=-1):
        assert size == 4097, "worker performed an unbounded read"
        self.sizes.append(size)
        return super().readline(size)


@pytest.mark.parametrize("message", [
    b'{"id":1,"id":2,"command":{"op":"state"}}',
    b'{"id":1,"command":{"op":"state","op":"navigate","url":"about:blank"}}',
    b'{"id":1,"command":{"op":"input.pointer","action":"wheel","x":0,"y":0,"deltaY":NaN}}',
    b'{"id":1,"command":{"op":"input.pointer","action":"wheel","x":0,"y":0,"deltaY":Infinity}}',
])
def test_serve_rejects_ambiguous_json_before_dispatch(monkeypatch, message):
    worker = SimpleNamespace(start=AsyncMock(), close=AsyncMock(), execute=AsyncMock())
    source = BoundedInput(message + b"\n")
    output = io.StringIO()
    monkeypatch.setattr(module, "BrowserWorker", lambda *_: worker)
    monkeypatch.setattr(module.sys, "stdin", SimpleNamespace(buffer=source))
    monkeypatch.setattr(module.sys, "stdout", output)
    asyncio.run(module.serve("playwright", False))
    result = json.loads(output.getvalue())
    assert result["ok"] is False and result["indeterminate"] is False
    worker.execute.assert_not_awaited()
    worker.close.assert_awaited_once()
    assert source.sizes == [4097, 4097]


def test_overlong_input_is_bounded_and_closes_without_dispatch(monkeypatch):
    worker = SimpleNamespace(start=AsyncMock(), close=AsyncMock(), execute=AsyncMock())
    source = BoundedInput(b"x" * 100000)
    monkeypatch.setattr(module, "BrowserWorker", lambda *_: worker)
    monkeypatch.setattr(module.sys, "stdin", SimpleNamespace(buffer=source))
    asyncio.run(module.serve("playwright", False))
    assert source.tell() == 4097
    worker.execute.assert_not_awaited()
    worker.close.assert_awaited_once()


def test_file_navigation_decodes_then_checks_real_workspace_path(monkeypatch):
    resolved = []

    def resolve(path, **kwargs):
        resolved.append(str(path))
        return Path("/home/agent/a page.html")

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(Path, "is_file", lambda _: True)
    worker = BrowserWorker("playwright", False)
    worker.page = SimpleNamespace(
        goto=AsyncMock(), title=AsyncMock(return_value="Fixture"),
        url="file:///home/agent/a%20page.html",
    )
    asyncio.run(worker.execute({"op": "navigate", "url": worker.page.url}))
    assert resolved == [str(Path("/home/agent/a page.html"))]
    worker.page.goto.assert_awaited_once()


def test_encoded_traversal_cannot_escape_real_workspace_path(monkeypatch):
    monkeypatch.setattr(Path, "resolve", lambda *_args, **_kwargs: Path("/etc/passwd"))
    worker = BrowserWorker("playwright", False)
    worker.page = SimpleNamespace(goto=AsyncMock())
    with pytest.raises(ValueError, match="outside workspace"):
        asyncio.run(worker.execute({"op": "navigate", "url": "file:///home/%2e%2e/etc/passwd"}))
    worker.page.goto.assert_not_awaited()
