"""Compose debugger pipes with the existing interactive sandbox supervisor.

Like SandboxedMcpClient, admission must be supplied by the trusted controller
after its durable task/lease/budget checks. There is no unconfined fallback.
"""

from __future__ import annotations

import hashlib

from potbot.debugging.contracts import Admission, DebugIdentity, EventRecorder
from potbot.debugging.dap import DapAdapter
from potbot.debugging.session import DebugSession
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.tools.interactive_supervisor import (
    InteractiveExit,
    InteractiveProcessSupervisor,
    IsolatedInteractiveProcess,
)
from potbot.tools.secrets import CaptureRedactor
from potbot.tools.supervisor import ProcessRequest


class SandboxedDebugger:
    """One explicitly pinned adapter and target inventory, with owned cleanup."""

    def __init__(
        self,
        *,
        supervisor: InteractiveProcessSupervisor,
        request: ProcessRequest,
        identity: DebugIdentity,
        executable_sha256: str,
        admit: Admission,
        record: EventRecorder,
        redactor: CaptureRedactor | None = None,
    ) -> None:
        if not isinstance(supervisor, InteractiveProcessSupervisor):
            raise InvalidInputError("Debugger requires the existing interactive supervisor")
        if identity.source_root != request.workspace_root.resolve():
            raise ScopeDeniedError("Debugger source and admitted process workspace differ")
        if not callable(admit) or not callable(record):
            raise ScopeDeniedError("Debugger admission and causal recorder are required")
        self.supervisor, self.request, self.identity = supervisor, request, identity
        self.executable_sha256 = executable_sha256
        self.admit, self.record, self.redactor = admit, record, redactor
        self.process: IsolatedInteractiveProcess | None = None
        self.session: DebugSession | None = None
        self.exit: InteractiveExit | None = None

    async def __aenter__(self) -> DebugSession:
        from pathlib import Path

        if self.process is not None:
            raise StateMismatchError("Debugger process lease cannot be reused")
        executable = Path(self.request.argv[0])
        if hashlib.sha256(executable.read_bytes()).hexdigest() != self.executable_sha256:
            raise StateMismatchError("Debugger interpreter/executable changed from its pin")
        self.identity.verify_sources()
        self.admit(
            "debug.adapter.start",
            {
                "argv": self.request.argv,
                "workspace": str(self.identity.workspace_id),
                "timeout_ms": self.request.timeout_ms,
            },
        )
        self.process = await self.supervisor.open(self.request)
        try:
            self.session = DebugSession(
                DapAdapter(self.process.reader, self.process.writer),
                self.identity,
                admit=self.admit,
                record=self.record,
                redactor=self.redactor,
            )
            await self.session.initialize()
            return self.session
        except BaseException:
            try:
                if self.session is not None:
                    await self.session.close(terminate_launched=True)
            finally:
                self.exit = await self.process.close(force=True)
            raise

    async def __aexit__(self, *_: object) -> None:
        try:
            if self.session is not None:
                await self.session.close(terminate_launched=True)
        finally:
            if self.process is not None:
                self.exit = await self.process.close()
