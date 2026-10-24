"""Protocol-independent session identity and stop-scoped inspection handles."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.execution_events import ExecutionEventInput


class DebugState(StrEnum):
    CREATED = "created"
    CONFIGURING = "configuring"
    RUNNING = "running"
    STOPPED = "stopped"
    DISCONNECTED = "disconnected"
    TERMINATED = "terminated"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class DebugIdentity:
    workspace_id: UUID
    emitter_run_id: UUID
    source_root: Path
    source_sha256: tuple[tuple[str, str], ...]
    adapter: str
    adapter_version: str
    runtime: str
    event_type_ref: str
    event_type_version: str = "1"

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, UUID) or not isinstance(self.emitter_run_id, UUID):
            raise InvalidInputError("Debugger workspace and emitter must be UUIDs")
        if not self.source_root.is_absolute() or self.source_root.resolve() != self.source_root:
            raise ScopeDeniedError("Debugger source root must be an exact non-symlink directory")
        if not self.source_root.is_dir() or not 1 <= len(self.source_sha256) <= 256:
            raise InvalidInputError("Debugger requires a bounded pinned source inventory")
        if len(dict(self.source_sha256)) != len(self.source_sha256):
            raise InvalidInputError("Debugger source inventory contains duplicate paths")
        for name in (self.adapter, self.adapter_version, self.runtime, self.event_type_version):
            if not isinstance(name, str) or not 1 <= len(name) <= 256:
                raise InvalidInputError("Debugger implementation versions must be explicit")
        for relative, digest in self.source_sha256:
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise InvalidInputError("Debugger source requires exact lowercase SHA-256")
            self.source(relative)

    def source(self, value: str | Path) -> Path:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = self.source_root / candidate
        original = candidate.absolute()
        resolved = candidate.resolve()
        if original != resolved or not resolved.is_relative_to(self.source_root):
            raise ScopeDeniedError("Debugger source path leaves its pinned source root")
        relative = resolved.relative_to(self.source_root).as_posix()
        expected = dict(self.source_sha256).get(relative)
        if expected is None or not resolved.is_file() or resolved.stat().st_size > 16 * 1024**2:
            raise ScopeDeniedError("Debugger source is not in its bounded approved inventory")
        if hashlib.sha256(resolved.read_bytes()).hexdigest() != expected:
            raise StateMismatchError("Debugger source changed from its approved snapshot")
        return resolved

    def verify_sources(self) -> None:
        for relative, _ in self.source_sha256:
            self.source(relative)


@dataclass(frozen=True, slots=True)
class Stop:
    session_id: UUID
    revision: int
    thread_id: int
    reason: str
    all_threads_stopped: bool


@dataclass(frozen=True, slots=True)
class Frame:
    stop: Stop
    adapter_id: int
    name: str
    source: str | None
    line: int
    column: int


@dataclass(frozen=True, slots=True)
class ValueReference:
    stop: Stop
    adapter_id: int


@dataclass(frozen=True, slots=True)
class Scope:
    name: str
    reference: ValueReference
    expensive: bool


@dataclass(frozen=True, slots=True)
class Variable:
    name: str
    value: str = field(repr=False)
    type_name: str | None = None
    reference: ValueReference | None = None


@dataclass(frozen=True, slots=True)
class Breakpoint:
    requested_line: int
    verified: bool
    actual_line: int | None


EventHandler = Callable[[str, Mapping[str, Any]], None]
Admission = Callable[[str, Mapping[str, Any]], None]
EventRecorder = Callable[[ExecutionEventInput], object]


class DebugAdapter(Protocol):
    """An adapter translates these neutral operations to its own wire protocol."""

    on_event: EventHandler | None

    async def call(self, operation: str, arguments: Mapping[str, Any]) -> dict[str, Any]: ...

    async def begin(self, operation: str, arguments: Mapping[str, Any]) -> int: ...

    async def finish(self, request_id: int) -> dict[str, Any]: ...

    async def close(self) -> None: ...
