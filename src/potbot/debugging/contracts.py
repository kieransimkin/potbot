"""Protocol-independent session identity and stop-scoped inspection handles."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import InitVar, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.execution_events import ExecutionEventInput
from potbot.domain.serialization import content_hash
from potbot.tools.execution_contracts import remote_target


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
class RemoteDebugTarget:
    """Pin a qualified target and read its current snapshot from the connection owner.

    The observer reads state already collected by the trusted transport. It must
    not block on the adapter that is currently delivering a debugger event.
    """

    approved: InitVar[object]
    observe: Callable[[], object] = field(repr=False)
    fingerprint: str = field(init=False)
    attach_revision: int = field(init=False)
    capabilities: frozenset[str] = field(init=False, repr=False)

    def __post_init__(self, approved: object) -> None:
        selected = remote_target(approved)
        if selected["qualification"] != "qualified" or not callable(self.observe):
            raise ScopeDeniedError("Remote debugger needs a qualified target and live observation")
        object.__setattr__(self, "fingerprint", self._identity(selected))
        object.__setattr__(self, "attach_revision", selected["halt_revision"])
        object.__setattr__(
            self,
            "capabilities",
            frozenset(
                name for name, support in selected["capabilities"].items() if support == "supported"
            ),
        )

    @staticmethod
    def _identity(target: Mapping[str, Any]) -> str:
        return content_hash({key: value for key, value in target.items() if key != "halt_revision"})

    def require_current(self, *, halt_revision: int | None = None) -> int:
        try:
            current = remote_target(self.observe())
            fingerprint = self._identity(current)
        except (InvalidInputError, OSError, UnicodeError):
            raise ScopeDeniedError("Remote debugger target observation is unavailable") from None
        if fingerprint != self.fingerprint:
            raise ScopeDeniedError("Remote debugger target differs from its approved identity")
        revision = int(current["halt_revision"])
        if halt_revision is not None and revision != halt_revision:
            raise ScopeDeniedError("Remote debugger halt revision changed")
        return revision

    def require_attach(self) -> None:
        self.require_current(halt_revision=self.attach_revision)


@dataclass(frozen=True, slots=True)
class DebugAttachTarget:
    """Controller's selection for one already-owned process and adapter connection."""

    process_id: int
    configuration_sha256: str
    process_is_current: Callable[[], bool]
    remote: RemoteDebugTarget | None = None

    def __post_init__(self) -> None:
        if (
            type(self.process_id) is not int
            or self.process_id < 1
            or not isinstance(self.configuration_sha256, str)
            or len(self.configuration_sha256) != 64
            or any(c not in "0123456789abcdef" for c in self.configuration_sha256)
            or not callable(self.process_is_current)
            or (self.remote is not None and not isinstance(self.remote, RemoteDebugTarget))
        ):
            raise InvalidInputError("Debugger attach requires a process and configuration pin")

    def require_current(self, observed_pid: int | None) -> None:
        if (
            observed_pid is not None and observed_pid != self.process_id
        ) or self.process_is_current() is not True:
            raise ScopeDeniedError("Debugger attach target changed or its lease expired")

    def authorize(self, configuration: Mapping[str, Any]) -> None:
        if content_hash(dict(configuration)) != self.configuration_sha256:
            raise ScopeDeniedError("Debugger attach configuration differs from its target pin")
        self.require_current(None)
        if self.remote is not None:
            self.remote.require_attach()


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
