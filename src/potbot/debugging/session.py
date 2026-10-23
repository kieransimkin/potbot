"""Language-neutral debugger orchestration; authorization remains controller owned."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from potbot.debugging.contracts import (
    Admission,
    Breakpoint,
    DebugAdapter,
    DebugIdentity,
    DebugState,
    EventRecorder,
    Frame,
    Scope,
    Stop,
    ValueReference,
    Variable,
)
from potbot.domain.errors import (
    CapabilityUnavailableError,
    IndeterminateExecutionError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
    ToolFailureError,
)
from potbot.domain.execution_events import ExecutionEventInput
from potbot.domain.serialization import content_hash
from potbot.tools.secrets import CaptureRedactor

_SENSITIVE = re.compile(r"password|secret|token|api.?key|authorization|credential", re.I)


def _integer(value: Any, label: str, *, zero: bool = False) -> int:
    if type(value) is not int or not (0 if zero else 1) <= value <= 2**31 - 1:
        raise InvalidInputError(f"Invalid debugger {label}")
    return value


class DebugSession:
    """Single-controller session with explicit admission and causal event recording.

    Adapter pipes must already be admitted by the owning controller. This object
    does not install/launch adapters, authorize targets or claim durable recovery.
    """

    def __init__(
        self,
        adapter: DebugAdapter,
        identity: DebugIdentity,
        *,
        admit: Admission,
        record: EventRecorder,
        redactor: CaptureRedactor | None = None,
        timeout_s: float = 30,
        allow_evaluate: bool = False,
        capture_output: Callable[[Mapping[str, Any]], str] | None = None,
    ) -> None:
        if not callable(admit) or not callable(record):
            raise ScopeDeniedError("Debugger requires controller admission and an event recorder")
        if not 0.01 <= timeout_s <= 300:
            raise InvalidInputError("Debugger stop deadline is invalid")
        self.identity, self.session_id = identity, uuid4()
        self.adapter, self._admit, self._record = adapter, admit, record
        self._redactor = redactor or CaptureRedactor()
        self.timeout = timeout_s
        self.allow_evaluate = allow_evaluate
        self._capture_output = capture_output
        self.state = DebugState.CREATED
        self.stop: Stop | None = None
        self.operations: frozenset[str] = frozenset()
        self._revision = 0
        self._changed = asyncio.Event()
        self._initialized = asyncio.Event()
        self._lock = asyncio.Lock()
        self._closed = False
        self._attached = False
        self.target_process_id: int | None = None
        self.exit_code: int | None = None
        self._frames: set[Frame] = set()
        self._references: set[ValueReference] = set()
        self.adapter.on_event = self._event

    def _invalidate(self) -> None:
        self.stop = None
        self._frames.clear()
        self._references.clear()

    def _event(self, kind: str, body: Mapping[str, Any]) -> None:
        if self._closed:
            return
        if kind == "initialized":
            self._initialized.set()
        elif kind == "stopped":
            self._invalidate()
            self._revision += 1
            self.stop = Stop(
                self.session_id,
                self._revision,
                _integer(body.get("thread_id"), "stopped thread"),
                str(body.get("reason", "unknown"))[:128],
                body.get("all_threads_stopped") is True,
            )
            self.state = DebugState.STOPPED
        elif kind == "continued":
            self._invalidate()
            self.state = DebugState.RUNNING
        elif kind in {"terminated", "exited"}:
            if kind == "exited" and type(body.get("exit_code")) is int:
                self.exit_code = body["exit_code"]
            self._invalidate()
            self.state = DebugState.TERMINATED
        elif kind == "process" and type(body.get("process_id")) is int:
            self.target_process_id = _integer(body["process_id"], "target process")
        elif kind == "connection_lost":
            self._invalidate()
            if self.state not in {DebugState.TERMINATED, DebugState.DISCONNECTED}:
                self.state = DebugState.INDETERMINATE
        # Console/output is intentionally not retained as unreviewed evidence.
        self._changed.set()

    def _require(self, *states: DebugState) -> None:
        if self._closed or self.state not in states:
            raise StateMismatchError("Debugger operation is invalid in its current state")

    def _check_stop(self, stop: Stop) -> None:
        self._require(DebugState.STOPPED)
        if self.stop != stop or stop.session_id != self.session_id:
            raise StateMismatchError("Debugger inspection handle belongs to an expired stop")

    def _text(self, value: object) -> str:
        if not isinstance(value, str):
            raise InvalidInputError("Debugger text field is malformed")
        return cast(str, self._redactor.redact_metadata(value))[:4096]

    async def _invoke(
        self, operation: str, arguments: Mapping[str, Any], *, resume: bool = False
    ) -> dict[str, Any]:
        if operation != "initialize" and operation not in self.operations:
            raise CapabilityUnavailableError("Selected debugger does not support this operation")
        # Each call, including inspection/variable rendering, needs an explicit grant.
        payload = dict(arguments)
        self._admit("debug." + operation, payload)
        run_id = uuid4()
        digest = content_hash(
            {"session": str(self.session_id), "operation": operation, "parameters": payload}
        )
        started = time.monotonic()

        def emit(stage: Any, output: str | None = None, artifact: str | None = None) -> None:
            self._record(
                ExecutionEventInput(
                    event_type_ref=self.identity.event_type_ref,
                    event_type_version=self.identity.event_type_version,
                    subject_type_ref="synny://type/tool-invocation",
                    subject_version="debugger-v1",
                    run_id=run_id,
                    trigger_kind="api_request",
                    trigger_ref=f"debug://session/{self.session_id}/request/{run_id}",
                    trigger_input_sha256=digest,
                    emitter_run_id=self.identity.emitter_run_id,
                    stage=stage,
                    output_sha256=output,
                    output_artifact_ref=artifact,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            )

        emit("started")
        if resume:
            self._invalidate()
            self.state = DebugState.RUNNING
        try:
            output = await self.adapter.call(operation, payload)
            if self._capture_output is not None:
                safe_output = self._safe_output(output)
                artifact = self._capture_output(safe_output)
                emit("completed", content_hash(safe_output), artifact)
            else:
                emit("completed")
            return output
        except BaseException as error:
            uncertain = not isinstance(error, ToolFailureError)
            if uncertain:
                self._invalidate()
                self.state = DebugState.INDETERMINATE
            emit("indeterminate" if uncertain else "failed")
            raise

    def _safe_output(self, output: Mapping[str, Any]) -> dict[str, Any]:
        """Redact before artifact callbacks, including debugger name/value records."""

        def clean(value: Any) -> Any:
            if isinstance(value, dict):
                if isinstance(value.get("name"), str) and _SENSITIVE.search(value["name"]):
                    return {
                        k: "[REDACTED]"
                        if k in {"value", "result"}
                        else 0
                        if k == "reference"
                        else clean(v)
                        for k, v in value.items()
                    }
                return {k: clean(v) for k, v in value.items()}
            if isinstance(value, list):
                return [clean(v) for v in value]
            return value

        return cast(dict[str, Any], self._redactor.redact_metadata(clean(dict(output))))

    async def initialize(self) -> frozenset[str]:
        async with self._lock:
            self._require(DebugState.CREATED)
            result = await self._invoke(
                "initialize",
                {
                    "client_name": "potbot",
                    "adapter_id": self.identity.adapter,
                },
            )
            operations = result.get("operations")
            if not isinstance(operations, list) or any(not isinstance(x, str) for x in operations):
                raise InvalidInputError("Debugger capability negotiation is malformed")
            self.operations = frozenset(operations)
            self.state = DebugState.CONFIGURING
            return self.operations

    async def launch(
        self,
        configuration: Mapping[str, Any],
        *,
        breakpoints: Mapping[str | Path, tuple[int, ...]],
        exception_filters: tuple[str, ...] = (),
    ) -> None:
        """Configure a selected, approved target; launch response may follow configuration."""
        async with self._lock:
            self._require(DebugState.CONFIGURING)
            self.identity.verify_sources()
            if "launch" not in self.operations:
                raise CapabilityUnavailableError("Debugger cannot launch")
            config = dict(configuration)
            # The pending launch deliberately spans configurationDone (debugpy/DAP).
            launch_task = asyncio.create_task(self._invoke("launch", config))
            initialized_task = asyncio.create_task(self._initialized.wait())
            try:
                done, _ = await asyncio.wait(
                    {launch_task, initialized_task},
                    timeout=self.timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if launch_task in done:
                    await launch_task
                await asyncio.wait_for(initialized_task, self.timeout)
                for source, lines in breakpoints.items():
                    points = await self._breakpoints(source, lines)
                    if any(not p.verified for p in points):
                        raise StateMismatchError("Debugger breakpoint is unbound; launch held")
                if exception_filters:
                    await self._invoke("configure_exceptions", {"filters": list(exception_filters)})
                if "configuration_done" in self.operations:
                    await self._invoke("configuration_done", {})
                await launch_task
                if self.state == DebugState.CONFIGURING:
                    self.state = DebugState.RUNNING
            except BaseException:
                launch_task.cancel()
                initialized_task.cancel()
                await asyncio.gather(launch_task, initialized_task, return_exceptions=True)
                self._invalidate()
                self.state = DebugState.INDETERMINATE
                await self.adapter.close()
                raise

    async def _breakpoints(
        self, source: str | Path, lines: tuple[int, ...]
    ) -> tuple[Breakpoint, ...]:
        path = self.identity.source(source)
        if not isinstance(lines, tuple) or len(lines) > 128:
            raise InvalidInputError("Breakpoint list exceeds its bounded contract")
        for line in lines:
            _integer(line, "breakpoint line")
        result = await self._invoke("set_breakpoints", {"source_path": str(path), "lines": lines})
        raw = result.get("breakpoints")
        if not isinstance(raw, list) or len(raw) != len(lines):
            raise InvalidInputError("Debugger returned an invalid breakpoint set")
        return tuple(
            Breakpoint(
                line,
                item.get("verified") is True,
                _integer(item["line"], "breakpoint line") if "line" in item else None,
            )
            for line, item in zip(lines, raw, strict=True)
        )

    async def set_breakpoints(
        self, source: str | Path, lines: tuple[int, ...]
    ) -> tuple[Breakpoint, ...]:
        async with self._lock:
            self._require(DebugState.CONFIGURING, DebugState.STOPPED)
            return await self._breakpoints(source, lines)

    async def wait_stopped(self, *, after_revision: int = 0) -> Stop:
        try:
            async with asyncio.timeout(self.timeout):
                while True:
                    self._changed.clear()
                    if self.state == DebugState.STOPPED and self.stop is not None:
                        if self.stop.revision > after_revision:
                            return self.stop
                    if self.state in {
                        DebugState.INDETERMINATE,
                        DebugState.DISCONNECTED,
                        DebugState.TERMINATED,
                    }:
                        raise StateMismatchError("Debug target did not reach a new stopped state")
                    await self._changed.wait()
        except TimeoutError:
            self._invalidate()
            self.state = DebugState.INDETERMINATE
            raise IndeterminateExecutionError(
                "No confirmed debugger stop before deadline"
            ) from None

    async def wait_terminated(self) -> int | None:
        try:
            async with asyncio.timeout(self.timeout):
                while True:
                    self._changed.clear()
                    if self.state == DebugState.TERMINATED:
                        return self.exit_code
                    if self.state in {DebugState.INDETERMINATE, DebugState.DISCONNECTED}:
                        raise IndeterminateExecutionError(
                            "Target exit was not independently observed"
                        )
                    await self._changed.wait()
        except TimeoutError:
            self._invalidate()
            self.state = DebugState.INDETERMINATE
            raise IndeterminateExecutionError("Target exit deadline exceeded") from None

    async def threads(self) -> tuple[tuple[int, str], ...]:
        async with self._lock:
            self._require(DebugState.RUNNING, DebugState.STOPPED)
            result = await self._invoke("threads", {})
            return tuple(
                (_integer(t["id"], "thread"), self._text(t["name"]))
                for t in result.get("threads", [])[:128]
            )

    async def stack(self, stop: Stop, *, start: int = 0, limit: int = 32) -> tuple[Frame, ...]:
        async with self._lock:
            self._check_stop(stop)
            if (
                type(start) is not int
                or not 0 <= start <= 4096
                or type(limit) is not int
                or not 1 <= limit <= 128
            ):
                raise InvalidInputError("Debugger stack paging is invalid")
            result = await self._invoke(
                "stack", {"thread_id": stop.thread_id, "start_frame": start, "levels": limit}
            )
            self._check_stop(stop)
            frames = []
            for raw in result.get("frames", [])[:limit]:
                source = raw.get("source")
                # External/library frames remain identifiable but are not source-read authority.
                if source is not None:
                    source = self._text(source)
                frame = Frame(
                    stop,
                    _integer(raw["id"], "frame", zero=True),
                    self._text(raw["name"]),
                    source,
                    _integer(raw["line"], "line", zero=True),
                    _integer(raw["column"], "column", zero=True),
                )
                frames.append(frame)
                if len(self._frames) >= 4096 and frame not in self._frames:
                    raise InvalidInputError("Debugger frame handle budget exceeded")
                self._frames.add(frame)
            return tuple(frames)

    async def scopes(self, frame: Frame) -> tuple[Scope, ...]:
        async with self._lock:
            self._check_stop(frame.stop)
            if frame not in self._frames:
                raise ScopeDeniedError("Frame was not issued by this session")
            result = await self._invoke("scopes", {"frame_id": frame.adapter_id})
            self._check_stop(frame.stop)
            scopes = []
            for raw in result.get("scopes", [])[:32]:
                ref = ValueReference(frame.stop, _integer(raw["reference"], "scope reference"))
                if len(self._references) >= 4096 and ref not in self._references:
                    raise InvalidInputError("Debugger variable handle budget exceeded")
                self._references.add(ref)
                scopes.append(Scope(self._text(raw["name"]), ref, raw.get("expensive") is True))
            return tuple(scopes)

    async def variables(
        self, reference: ValueReference, *, start: int = 0, limit: int = 64
    ) -> tuple[Variable, ...]:
        async with self._lock:
            self._check_stop(reference.stop)
            if reference not in self._references:
                raise ScopeDeniedError("Variable reference was not issued by this session")
            if (
                type(start) is not int
                or not 0 <= start <= 4096
                or type(limit) is not int
                or not 1 <= limit <= 128
            ):
                raise InvalidInputError("Debugger variable paging is invalid")
            result = await self._invoke(
                "variables", {"reference": reference.adapter_id, "start": start, "count": limit}
            )
            self._check_stop(reference.stop)
            values = []
            for raw in result.get("variables", [])[:limit]:
                name = self._text(raw["name"])
                sensitive = _SENSITIVE.search(name) is not None
                refid = _integer(raw.get("reference", 0), "variable reference", zero=True)
                child = ValueReference(reference.stop, refid) if refid and not sensitive else None
                if child is not None:
                    if len(self._references) >= 4096 and child not in self._references:
                        raise InvalidInputError("Debugger variable handle budget exceeded")
                    self._references.add(child)
                values.append(
                    Variable(
                        name,
                        "[REDACTED]" if sensitive else self._text(raw["value"]),
                        self._text(raw["type"]) if raw.get("type") else None,
                        child,
                    )
                )
            return tuple(values)

    async def resume(self, stop: Stop, *, operation: str = "continue") -> None:
        async with self._lock:
            self._check_stop(stop)
            if operation not in {"continue", "step_over", "step_into", "step_out"}:
                raise InvalidInputError("Invalid debugger resume operation")
            self.identity.verify_sources()
            await self._invoke(operation, {"thread_id": stop.thread_id}, resume=True)

    async def step(self, stop: Stop, operation: str) -> Stop:
        if operation not in {"step_over", "step_into", "step_out"}:
            raise InvalidInputError("Expected a debugger step operation")
        await self.resume(stop, operation=operation)
        return await self.wait_stopped(after_revision=stop.revision)

    async def pause(self, thread_id: int) -> Stop:
        async with self._lock:
            self._require(DebugState.RUNNING)
            previous = self._revision
            await self._invoke("pause", {"thread_id": _integer(thread_id, "thread")})
        return await self.wait_stopped(after_revision=previous)

    async def exception_info(self, stop: Stop) -> dict[str, Any]:
        async with self._lock:
            self._check_stop(stop)
            result = await self._invoke("exception_info", {"thread_id": stop.thread_id})
            self._check_stop(stop)
            return cast(dict[str, Any], self._redactor.redact_metadata(result))

    async def evaluate(self, frame: Frame, expression: str) -> dict[str, Any]:
        async with self._lock:
            self._check_stop(frame.stop)
            if frame not in self._frames or not self.allow_evaluate:
                raise ScopeDeniedError("Debugger expression execution is not granted")
            if not isinstance(expression, str) or not 1 <= len(expression) <= 1024:
                raise InvalidInputError("Debugger expression exceeds its bound")
            result = await self._invoke(
                "evaluate",
                {"frame_id": frame.adapter_id, "expression": expression, "context": "watch"},
            )
            self._check_stop(frame.stop)
            return cast(dict[str, Any], self._redactor.redact_metadata(result))

    async def close(self, *, terminate_launched: bool = False) -> None:
        if self._closed:
            return
        try:
            if self.state not in {
                DebugState.CREATED,
                DebugState.INDETERMINATE,
                DebugState.DISCONNECTED,
                DebugState.TERMINATED,
            }:
                if terminate_launched and self._attached:
                    raise ScopeDeniedError("Detaching must not terminate an attached target")
                await self._invoke("disconnect", {"terminate_debuggee": terminate_launched})
                self.state = DebugState.DISCONNECTED
        finally:
            self._closed = True
            self._invalidate()
            await self.adapter.close()
