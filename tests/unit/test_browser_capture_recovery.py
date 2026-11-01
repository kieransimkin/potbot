"""Bounded browser screenshot recovery; synthetic page/CDP doubles, not Xpra certification."""
from __future__ import annotations

import asyncio
import base64
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from potbot.tools.xpra_browser_worker import BrowserWorker, validate_command

JPEG = b"\xff\xd8" + b"synthetic-evidence" * 20 + b"\xff\xd9"
SURFACE_ERROR = "Page.screenshot: Protocol error (Page.captureScreenshot): Unable to capture screenshot"


def browser(*, screen=None, cdp=None):
    worker = BrowserWorker("playwright", False)
    screenshot = (AsyncMock(side_effect=screen) if isinstance(screen, (list, Exception))
                  else AsyncMock(return_value=screen))
    worker.page = SimpleNamespace(screenshot=screenshot, url="about:blank")
    worker.cdp = SimpleNamespace(send=AsyncMock(return_value=cdp))
    worker._aria = AsyncMock(return_value='- button "Sample"')
    return worker


def sample(worker, *, image=True):
    return asyncio.run(worker.execute({"op": "sample", "image": image}))


def test_clean_capture_is_single_attempt_and_retains_exact_bytes():
    worker = browser(screen=JPEG)
    result = sample(worker)
    frame = result["frame"]
    assert frame["bytes"] == len(JPEG)
    assert frame["sha256"] == hashlib.sha256(JPEG).hexdigest()
    assert base64.b64decode(frame["base64"], validate=True) == JPEG
    assert result["aria"] == '- button "Sample"'
    assert worker.page.screenshot.await_count == 1
    worker.cdp.send.assert_not_awaited()


def test_transient_surface_error_retries_same_page_once():
    worker = browser(screen=[RuntimeError(SURFACE_ERROR), JPEG])
    assert sample(worker)["frame"]["bytes"] == len(JPEG)
    assert worker.page.screenshot.await_count == 2
    worker.cdp.send.assert_not_awaited()


def test_second_surface_error_uses_only_private_nonsurface_capture():
    worker = browser(screen=[RuntimeError(SURFACE_ERROR), RuntimeError(SURFACE_ERROR)],
                     cdp={"data": base64.b64encode(JPEG).decode("ascii")})
    result = sample(worker, image=False)
    assert result["frame"] == {
        "mime": "image/jpeg", "sha256": hashlib.sha256(JPEG).hexdigest(),
        "bytes": len(JPEG),
    }
    worker.cdp.send.assert_awaited_once_with("Page.captureScreenshot", {
        "format": "jpeg", "quality": 48, "fromSurface": False,
        "captureBeyondViewport": False,
    })
    # No new untrusted CDP method is authorized by the fallback.
    with pytest.raises(ValueError):
        validate_command({"op": "cdp", "method": "Page.captureScreenshot"})


def test_unrelated_browser_error_does_not_retry_or_use_fallback():
    worker = browser(screen=RuntimeError("Page.screenshot: browser disconnected"))
    with pytest.raises(RuntimeError, match="disconnected"):
        sample(worker)
    assert worker.page.screenshot.await_count == 1
    worker.cdp.send.assert_not_awaited()


def test_retry_failure_not_matching_transient_error_stops_before_cdp():
    worker = browser(screen=[RuntimeError(SURFACE_ERROR), RuntimeError("permission denied")])
    with pytest.raises(RuntimeError, match="permission denied"):
        sample(worker)
    assert worker.page.screenshot.await_count == 2
    worker.cdp.send.assert_not_awaited()


@pytest.mark.parametrize("fallback", [
    {}, {"data": 7}, {"data": "$$$"}, {"data": "A" * 800001},
    {"data": base64.b64encode(b"not-a-jpeg").decode("ascii")},
])
def test_fallback_requires_bounded_base64_of_actual_jpeg(fallback):
    worker = browser(screen=[RuntimeError(SURFACE_ERROR), RuntimeError(SURFACE_ERROR)],
                     cdp=fallback)
    with pytest.raises(ValueError, match="render sample"):
        sample(worker)
    assert worker.page.screenshot.await_count == 2
    worker.cdp.send.assert_awaited_once()


def test_direct_capture_rejects_oversize_and_invalid_jpeg():
    for content in (b"not-a-jpeg", b"\xff\xd8" + b"a" * 600001 + b"\xff\xd9"):
        worker = browser(screen=content)
        with pytest.raises(ValueError, match="render sample"):
            sample(worker)
        worker.cdp.send.assert_not_awaited()


def test_cdp_timeout_does_not_fabricate_success_or_retry_effects():
    worker = browser(screen=[RuntimeError(SURFACE_ERROR), RuntimeError(SURFACE_ERROR)])
    worker.cdp.send = AsyncMock(side_effect=asyncio.TimeoutError())
    with pytest.raises(asyncio.TimeoutError):
        sample(worker)
    assert worker.page.screenshot.await_count == 2
    assert worker.cdp.send.await_count == 1
