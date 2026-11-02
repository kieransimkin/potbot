"""Private non-surface capture regression; test doubles do not qualify Xpra."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from potbot.tools.xpra_browser_worker import BrowserWorker, validate_command

JPEG = b"\xff\xd8" + b"synthetic-evidence" * 20 + b"\xff\xd9"
PARAMS = {
    "format": "jpeg",
    "quality": 48,
    "fromSurface": False,
    "captureBeyondViewport": False,
}


def browser(packet=None, *, error=None):
    worker = BrowserWorker("playwright", False)
    worker.page = SimpleNamespace(
        screenshot=AsyncMock(side_effect=AssertionError("surface screenshot must not run")),
        url="about:blank",
    )
    worker.cdp = SimpleNamespace(send=AsyncMock(return_value=packet, side_effect=error))
    worker._aria = AsyncMock(return_value='- button "Sample"')
    return worker


def sample(worker, *, image=True):
    return asyncio.run(worker.execute({"op": "sample", "image": image}))


def test_private_capture_is_one_bounded_nonsurface_request_and_exact_bytes():
    worker = browser({"data": base64.b64encode(JPEG).decode("ascii")})
    value = sample(worker)
    frame = value["frame"]
    assert (frame["mime"], frame["bytes"], frame["sha256"]) == (
        "image/jpeg",
        len(JPEG),
        hashlib.sha256(JPEG).hexdigest(),
    )
    assert base64.b64decode(frame["base64"], validate=True) == JPEG
    assert value["aria"] == '- button "Sample"'
    worker.cdp.send.assert_awaited_once_with("Page.captureScreenshot", PARAMS)
    worker.page.screenshot.assert_not_awaited()
    # The trusted internal evidence path must not become public CDP authority.
    with pytest.raises(ValueError):
        validate_command({"op": "cdp", "method": "Page.captureScreenshot"})


def test_omit_image_has_digest_and_no_encoded_bytes():
    worker = browser({"data": base64.b64encode(JPEG).decode()})
    frame = sample(worker, image=False)["frame"]
    assert frame == {
        "mime": "image/jpeg",
        "sha256": hashlib.sha256(JPEG).hexdigest(),
        "bytes": len(JPEG),
    }
    worker.cdp.send.assert_awaited_once()


@pytest.mark.parametrize(
    "packet",
    [
        {},
        {"data": 7},
        {"data": "$$$"},
        {"data": "A" * 800001},
        {"data": base64.b64encode(b"not-a-jpeg").decode()},
        {"data": base64.b64encode(b"\xff\xd8" + b"a" * 600001 + b"\xff\xd9").decode()},
    ],
)
def test_invalid_capture_fails_closed_without_retry(packet):
    worker = browser(packet)
    with pytest.raises(ValueError, match="render sample"):
        sample(worker)
    worker.cdp.send.assert_awaited_once()
    worker.page.screenshot.assert_not_awaited()


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("browser disconnected"), PermissionError("fixture denial")],
)
def test_capture_errors_propagate_with_no_second_attempt(failure):
    worker = browser(error=failure)
    with pytest.raises(type(failure)):
        sample(worker)
    worker.cdp.send.assert_awaited_once()
    worker.page.screenshot.assert_not_awaited()


def test_capture_timeout_uses_one_private_surface_fallback():
    worker = browser()
    worker.cdp.send.side_effect = [TimeoutError(), {"data": base64.b64encode(JPEG).decode("ascii")}]
    frame = sample(worker)["frame"]
    assert frame["sha256"] == hashlib.sha256(JPEG).hexdigest()
    assert worker.cdp.send.await_count == 2
    assert worker.cdp.send.await_args_list[0].args == ("Page.captureScreenshot", PARAMS)
    assert worker.cdp.send.await_args_list[1].args == (
        "Page.captureScreenshot",
        {**PARAMS, "fromSurface": True},
    )
    worker.page.screenshot.assert_not_awaited()


def test_both_capture_paths_timeout_without_further_retries():
    worker = browser(error=TimeoutError())
    with pytest.raises(TimeoutError):
        sample(worker)
    assert worker.cdp.send.await_count == 2


def test_real_capture_timeout_cancels_each_read_with_bounded_budget(monkeypatch):
    from potbot.tools import xpra_browser_worker as module

    worker = browser()
    cancelled = []
    budgets = []
    original_wait_for = asyncio.wait_for

    async def stalled(method, params):
        try:
            await asyncio.Future()
        finally:
            cancelled.append(params["fromSurface"])

    async def short_wait(awaitable, *, timeout):
        budgets.append(timeout)
        return await original_wait_for(awaitable, timeout=0.01)

    worker.cdp.send.side_effect = stalled
    monkeypatch.setattr(module.asyncio, "wait_for", short_wait)
    with pytest.raises(TimeoutError):
        sample(worker)
    assert budgets == [3.0, 3.0]
    assert cancelled == [False, True]


def test_invalid_surface_fallback_is_rejected_without_third_attempt():
    worker = browser()
    worker.cdp.send.side_effect = [TimeoutError(), {"data": "$$$"}]
    with pytest.raises(ValueError, match="render sample"):
        sample(worker)
    assert worker.cdp.send.await_count == 2
