"""Shared bounded public-CLI execution for verified upstream Syn packages."""
from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from potbot.domain.errors import StateMismatchError

from .contracts import document


class SynPublicCommands:
    root: Path

    def _environment(self) -> dict[str, str]:
        import pwd

        environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": pwd.getpwuid(os.getuid()).pw_dir,
            "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": "C.UTF-8",
        }
        # Native session endpoints are not credentials; arbitrary Python, token,
        # private-key and package-manager configuration is never inherited.
        for name in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "DISPLAY", "WAYLAND_DISPLAY"):
            if name in os.environ:
                environment[name] = os.environ[name]
        return environment

    def _public_command(
        self, python: Path, arguments: list[str], *, timeout: int = 30
    ) -> dict[str, Any]:
        process = subprocess.Popen(
            [str(python), "-I", "-B", *arguments],
            cwd=self.root,
            env=self._environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        assert process.stdout is not None
        raw = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + timeout
                while True:
                    if time.monotonic() >= deadline:
                        raise StateMismatchError(
                            "Upstream Syn operation exceeded its deadline"
                        )
                    if not selector.select(0.1):
                        continue
                    part = os.read(process.stdout.fileno(), min(65536, 65537 - len(raw)))
                    if not part:
                        break
                    raw.extend(part)
                    if len(raw) > 65536:
                        raise StateMismatchError(
                            "Upstream Syn public response exceeded its bound"
                        )
            if process.wait(timeout=5) != 0:
                raise StateMismatchError(
                    "Upstream Syn refused; reconcile native keystore and state"
                )
            return document(bytes(raw))
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)
            process.stdout.close()
