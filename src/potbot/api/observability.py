"""Bounded, redacted API/workflow telemetry with optional content disclosure.

Console output is on stderr (stdout remains usable for machine-readable output).
The opt-in content stream is not a private chain-of-thought recorder.  Never
serialize authorization headers, raw transport exceptions or provider credentials.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from potbot.tools.secrets import CaptureRedactor


class EventLogger:
    """Thread-safe event writer; intentionally accepts only caller-selected fields."""

    def __init__(
        self,
        *,
        log_path: Path | None = None,
        log_content: bool = False,
        protected_values: tuple[str, ...] = (),
        stream: Any = None,
    ) -> None:
        self.log_content = log_content
        self._redactor = CaptureRedactor(protected_values)
        self._lock = threading.Lock()
        self._stream = sys.stderr if stream is None else stream
        self._handler: RotatingFileHandler | None = None
        if log_path is not None:
            log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if log_path.exists() and log_path.is_symlink():
                raise ValueError("The log path must not be a symbolic link")
            self._handler = RotatingFileHandler(
                str(log_path), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            if os.name != "nt":
                os.chmod(log_path, 0o600)

    def emit(self, kind: str, *, content: str | None = None, **fields: Any) -> None:
        if content is not None and self.log_content:
            # Bound retained contents independently of wire request limits.
            # The API caps accepted text; retain complete normal exchanges.
            limit = 4 * 1024 * 1024
            fields["content"] = content[:limit]
            fields["content_truncated"] = len(content) > limit
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": kind,
            **fields,
        }
        sanitized = self._redactor.redact_metadata(record)
        line = json.dumps(sanitized, sort_keys=True, ensure_ascii=False, default=str)
        with self._lock:
            print("[Potbot] " + line, file=self._stream, flush=True)
            if self._handler is not None:
                item = logging.LogRecord("potbot.events", logging.INFO, "", 0, line, (), None)
                self._handler.emit(item)

    def close(self) -> None:
        if self._handler is not None:
            self._handler.close()


class TransferProgress:
    """Simple safe speed/ETA estimator for a model download progress callback."""

    def __init__(self, logger: EventLogger, interval: float = 1.0) -> None:
        self.logger, self.interval = logger, interval
        self._started = time.monotonic()
        self._last = 0.0
        self._key = ""

    def __call__(self, event: Any) -> None:
        now = time.monotonic()
        key = str(event.item)
        if key != self._key:
            self._started, self._key, self._last = now, key, 0.0
        complete = event.completed_bytes >= event.total_bytes
        if not complete and now - self._last < self.interval:
            return
        self._last = now
        elapsed = max(now - self._started, 0.001)
        rate = int(event.completed_bytes / elapsed)
        remaining = max(event.total_bytes - event.completed_bytes, 0)
        self.logger.emit(
            "model.download",
            stage=event.stage,
            item=key,
            completed_bytes=event.completed_bytes,
            total_bytes=event.total_bytes,
            percent=round(100 * event.completed_bytes / max(event.total_bytes, 1), 1),
            bytes_per_second=rate,
            eta_seconds=int(remaining / rate) if rate > 0 else None,
        )
