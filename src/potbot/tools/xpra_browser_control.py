"""Operator-owned bounded stdio RPC to a controlled Chromium inside Xpra.

Only an operator-admitted, leased Xpra *application session* can receive a
browser command. No CDP TCP endpoint or host process/socket identifier leaks to
callers. No retries for actions with indeterminate outcomes.
"""
from __future__ import annotations

import json
import os
import select
import threading
import time
from typing import Any, cast

from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.tools.xpra_browser_worker import validate_command

MAX_REQUEST = 4096
MAX_REPLY = 1_500_000


class BrowserControlChannel:
    def __init__(self, process: Any) -> None:
        if process.stdin is None or process.stdout is None:
            raise StateMismatchError("controlled browser has no private command pipes")
        self.process = process
        self._lock = threading.Lock()
        self._sequence = 0
        self._buffer = bytearray()
        self._fenced = False

    def call(self, command: object, *, timeout: float = 18) -> dict[str, Any]:
        try:
            approved = validate_command(command)
        except (ValueError, TypeError) as exc:
            raise InvalidInputError("invalid controlled-browser operation") from exc
        with self._lock:
            if self._fenced or self.process.poll() is not None:
                raise StateMismatchError("controlled browser has exited or been fenced")
            self._sequence += 1
            seq = self._sequence
            outgoing = json.dumps({"id": seq, "command": approved}, separators=(",", ":")).encode("utf-8") + b"\n"
            if len(outgoing) > MAX_REQUEST:
                raise InvalidInputError("browser operation is too large")
            try:
                self.process.stdin.write(outgoing)
                self.process.stdin.flush()
                deadline = time.monotonic() + timeout
                while True:
                    if b"\n" in self._buffer:
                        line, _, leftover = self._buffer.partition(b"\n")
                        self._buffer = bytearray(leftover)
                        data = json.loads(line)
                        if not isinstance(data, dict) or data.get("id") != seq or type(data.get("ok")) is not bool:
                            raise ValueError("unexpected browser worker response")
                        if not data["ok"]:
                            raise InvalidInputError("browser command was rejected or failed")
                        if not isinstance(data.get("result"), dict):
                            raise ValueError("invalid browser result")
                        return cast(dict[str, Any], data["result"])
                    remain = deadline - time.monotonic()
                    if remain <= 0 or self.process.poll() is not None:
                        raise TimeoutError("browser command outcome is indeterminate")
                    if not select.select([self.process.stdout.fileno()], [], [], remain)[0]:
                        raise TimeoutError("browser command outcome is indeterminate")
                    chunk = os.read(self.process.stdout.fileno(), min(8192, MAX_REPLY - len(self._buffer)))
                    if not chunk:
                        raise EOFError("browser worker response channel closed")
                    self._buffer.extend(chunk)
                    if len(self._buffer) >= MAX_REPLY:
                        raise ValueError("browser worker result exceeds bound")
            except InvalidInputError:
                # An explicit worker rejection is a definite failed action.
                raise
            except (BrokenPipeError, OSError, TimeoutError, EOFError, ValueError, json.JSONDecodeError) as exc:
                # Do not retry! The worker might have clicked/submitted before a
                # disconnect. The operator must reconcile/terminate the session.
                self._fenced = True
                raise StateMismatchError("browser outcome is indeterminate; session fenced") from exc

    def close(self) -> None:
        self._fenced = True
        for stream in (self.process.stdin, self.process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
