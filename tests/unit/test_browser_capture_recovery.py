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
    [RuntimeError("browser disconnected"), PermissionError("fixture denial"), TimeoutError()],
)
def test_capture_errors_propagate_with_no_second_attempt(failure):
    worker = browser(error=failure)
    with pytest.raises(type(failure)):
        sample(worker)
    worker.cdp.send.assert_awaited_once()
    worker.page.screenshot.assert_not_awaited()
