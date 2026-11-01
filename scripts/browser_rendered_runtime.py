"""Test-only engine-binary selection; never installed in the sandbox image.

Production still launches /usr/bin/chromium. Hosted acceptance instead selects
exactly the Chromium executable reported by the installed engine. No binary path
comes from a page, model request or environment variable. Other launch options,
worker commands and CDP allowlists are left untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

ENGINES = frozenset({"playwright", "patchright"})


def selection_mode() -> str:
    mode = os.environ.get("POTBOT_BROWSER_TEST_BINARY", "system")
    if mode not in {"system", "engine"}:
        raise ValueError("unknown rendered-test browser selection")
    return mode


def binary_identity(path: str | Path, source: str) -> dict[str, object]:
    if source not in {"system", "engine"}:
        raise ValueError("unknown browser binary source")
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ValueError("rendered-test browser path must be absolute")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError("selected browser executable is unavailable")
    with resolved.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"source": source, "sha256": digest, "bytes": resolved.stat().st_size}


@contextmanager
def _redirect_launch(browser_type: type[Any]) -> Iterator[None]:
    original = browser_type.launch

    async def launch(instance: Any, *args: Any, **kwargs: Any) -> Any:
        if instance.name != "chromium" or kwargs.get("executable_path") != "/usr/bin/chromium":
            raise ValueError("worker launch no longer matches the rendered-test contract")
        # This property belongs to Playwright/Patchright, not to the guest.
        candidate = Path(instance.executable_path).resolve(strict=True)
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ValueError("the selected engine browser was not installed")
        kwargs["executable_path"] = str(candidate)
        return await original(instance, *args, **kwargs)

    with patch.object(browser_type, "launch", launch):
        yield


@contextmanager
def selected_browser(engine: str) -> Iterator[None]:
    if engine not in ENGINES:
        raise ValueError("unknown rendered-test engine")
    if selection_mode() == "system":
        yield
        return
    if os.environ.get("POTBOT_RUN_BROWSER_RENDERED") != "1":
        raise ValueError("engine selection is restricted to explicit rendered tests")
    module = importlib.import_module(engine + ".async_api")
    with _redirect_launch(module.BrowserType):
        yield


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=sorted(ENGINES), required=True)
    parser.add_argument("--stealth", action="store_true")
    args = parser.parse_args()
    if os.environ.get("POTBOT_RUN_BROWSER_RENDERED") != "1":
        parser.error("this entrypoint is only for explicit rendered acceptance")
    from potbot.tools.xpra_browser_worker import serve

    with selected_browser(args.engine):
        asyncio.run(serve(args.engine, args.stealth))


if __name__ == "__main__":
    main()
