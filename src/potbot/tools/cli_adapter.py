"""T02.3: typed, explicitly admitted local CLI operations.

Only operator-installed ToolDefinition argv prefixes are launched. Model proposals
cannot select arbitrary executables, shell wrappers, host paths, secrets, or
extra arguments; all execution runs through existing ProcessSupervisor and the
registered WorkspaceSandbox's candidate-root confinement.

This is an additive adapter: durable lease/budget admission remains with the
controller's existing ToolGateway/ExecutionStore, NOT with this pure wrapper.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.execution import ActionProposal, CapabilityContext, ToolRegistry
from potbot.domain.serialization import content_hash
from potbot.tools.supervisor import ProcessRequest, ProcessResult, ProcessSupervisor
from potbot.workspace_sandbox import WorkspaceSandbox

ArgPolicy = Callable[[tuple[str, ...]], None]

# Shells execute language text rather than a bounded argv contract. Do not
# expose them as general-purpose tools in this default profile.
_SHELL_NAMES = frozenset(
    {"sh", "bash", "dash", "zsh", "fish", "cmd.exe", "powershell.exe", "pwsh", "pwsh.exe"}
)


@dataclass(frozen=True, slots=True)
class CliObservation:
    """Local process observation; successful exit is not semantic verification."""

    definition_hash: str
    proposal_hash: str
    isolation_identity: str | None
    outcome: str
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    elapsed_ms: int
    processes_stopped: bool

    @property
    def fingerprint(self) -> str:
        return content_hash(
            {
                "definition_hash": self.definition_hash,
                "proposal_hash": self.proposal_hash,
                "isolation_identity": self.isolation_identity,
                "outcome": self.outcome,
                "exit_code": self.exit_code,
                "stdout_hash": hashlib.sha256(self.stdout.encode("utf-8")).hexdigest(),
                "stderr_hash": hashlib.sha256(self.stderr.encode("utf-8")).hexdigest(),
                "stdout_truncated": self.stdout_truncated,
                "stderr_truncated": self.stderr_truncated,
                "elapsed_ms": self.elapsed_ms,
                "processes_stopped": self.processes_stopped,
            }
        )


class ApprovedCliAdapter:
    """Restrict a real ProcessSupervisor to an installed tool and task grant."""

    def __init__(
        self,
        *,
        supervisor: ProcessSupervisor,
        sandbox: WorkspaceSandbox,
        registry: ToolRegistry,
        capabilities: CapabilityContext,
        task_contract: Mapping[str, Any],
        candidate_root: Path,
        pinned_executables: Mapping[str, str],
        argument_policies: Mapping[str, ArgPolicy] | None = None,
    ) -> None:
        if not isinstance(sandbox, WorkspaceSandbox) or not isinstance(
            supervisor, ProcessSupervisor
        ):
            raise InvalidInputError("CLI requires a registered sandbox and ProcessSupervisor")
        self.sandbox = sandbox
        self.candidate_root = sandbox.confine(candidate_root, area="candidates", must_exist=True)
        if not self.candidate_root.is_dir() or self.candidate_root.parent != sandbox.path(
            "candidates"
        ):
            raise ScopeDeniedError("CLI work must use one actual candidate directory")
        self.supervisor = supervisor
        self.registry = registry
        self.capabilities = capabilities
        self.task_contract = task_contract
        self.pinned_executables = dict(pinned_executables)
        self.argument_policies = dict(argument_policies or {})

    def run(self, proposal: ActionProposal) -> CliObservation:
        if not isinstance(proposal, ActionProposal):
            raise InvalidInputError("CLI requires a typed ActionProposal")
        tool = self.registry.get(proposal.kind)
        tool.admit(proposal)
        self.capabilities.authorize(tool, self.task_contract)
        if tool.secret_refs:
            raise ScopeDeniedError("CLI credential injection requires the later scoped broker")
        if any(
            name not in {"LANG", "LC_ALL", "TZ", "PYTHONUNBUFFERED", "NO_COLOR"}
            for name, _ in tool.environment
        ):
            raise ScopeDeniedError("CLI environment variable is not in the safe profile")
        if tool.mutates:
            raise ScopeDeniedError("CLI mutations require the existing durable ToolGateway")
        if tool.argv_prefix[0] not in self.pinned_executables:
            raise ScopeDeniedError("CLI executable has not been explicitly pinned")
        executable = Path(tool.argv_prefix[0])
        if executable.name.lower() in _SHELL_NAMES:
            raise ScopeDeniedError("Default CLI profile forbids shell-language entrypoints")
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ScopeDeniedError("CLI executable is not an approved installed file")
        with executable.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != self.pinned_executables[tool.argv_prefix[0]]:
            raise StateMismatchError("Installed CLI executable differs from its approved digest")
        if proposal.arguments:
            validator = self.argument_policies.get(tool.name)
            if validator is None:
                raise ScopeDeniedError("Model-supplied CLI arguments need a trusted exact policy")
            validator(proposal.arguments)
        work = self.sandbox.confine(
            self.candidate_root / proposal.working_directory, area="candidates", must_exist=True
        )
        if not work.is_dir() or not work.is_relative_to(self.candidate_root):
            raise ScopeDeniedError("CLI working directory escapes its candidate")
        # No inherited shell environment, no host HOME, no host temporary directory.
        environment = {**dict(tool.environment), **self.sandbox.runtime_environment()}
        request = ProcessRequest(
            argv=(*tool.argv_prefix, *proposal.arguments),
            cwd=work,
            workspace_root=self.candidate_root,
            sandbox_root=self.sandbox.root,
            timeout_ms=tool.timeout_ms,
            max_output_bytes=tool.max_output_bytes,
            environment=environment,
            read_only_roots=tuple(
                self.sandbox.path(area) for area in ("models", "tools", "dependencies")
            ),
            writable_roots=tuple(
                self.sandbox.path(area)
                for area in ("tmp", "cache", "environments", "builds", "tests")
            ),
        )
        result: ProcessResult = self.supervisor.run(request)
        if result.dispatched and not result.isolation_identity:
            raise StateMismatchError("CLI claimed dispatch without an isolation identity")
        return CliObservation(
            definition_hash=tool.fingerprint,
            proposal_hash=content_hash(proposal.to_dict()),
            isolation_identity=result.isolation_identity,
            outcome=result.outcome,
            exit_code=result.exit_code,
            stdout=result.stdout.decode("utf-8", errors="replace"),
            stderr=result.stderr.decode("utf-8", errors="replace"),
            stdout_truncated=result.stdout_truncated,
            stderr_truncated=result.stderr_truncated,
            elapsed_ms=result.elapsed_ms,
            processes_stopped=result.processes_stopped,
        )
