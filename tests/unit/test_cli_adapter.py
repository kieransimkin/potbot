"""T02.3 typed CLI adapter contract tests; ProcessSupervisor is simulated."""

from __future__ import annotations

import hashlib
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.execution import ActionProposal, CapabilityContext, ToolDefinition, ToolRegistry
from potbot.tools.cli_adapter import ApprovedCliAdapter
from potbot.tools.supervisor import ProcessResult, ProcessSupervisor
from potbot.workspace_sandbox import WorkspaceSandbox


class RecordingSupervisor(ProcessSupervisor):
    def __init__(self, *, isolation_identity: str | None = "unit-test-backend") -> None:
        self.requests = []
        self.isolation_identity = isolation_identity

    def run(self, request):
        self.requests.append(request)
        return ProcessResult(
            "completed",
            0,
            b"verified command output\n",
            b"",
            17,
            False,
            False,
            True,
            self.isolation_identity,
            processes_stopped=True,
        )


def setup(tmp_path: Path, *, mutates: bool = False, allow_arguments: bool = False):
    root = tmp_path / "workspace"
    candidate = root / "candidates" / "candidate-one"
    candidate.mkdir(parents=True, exist_ok=True)
    tool = ToolDefinition(
        name="python-version",
        capability="local-cli",
        argv_prefix=(sys.executable, "-V"),
        mutates=mutates,
        allow_arguments=allow_arguments,
    )
    with open(sys.executable, "rb") as fd:
        pinned = hashlib.file_digest(fd, "sha256").hexdigest()
    arguments = dict(
        supervisor=RecordingSupervisor(),
        sandbox=WorkspaceSandbox(root),
        registry=ToolRegistry((tool,)),
        capabilities=CapabilityContext((tool.name,)),
        task_contract={"capabilities": (tool.capability,)},
        candidate_root=candidate,
        pinned_executables={sys.executable: pinned},
    )
    return arguments, tool


def test_admitted_command_uses_fixed_argv_and_workspace_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CANARY_HOST_ENV", "must_not_inherit")
    arguments, tool = setup(tmp_path)
    adapter = ApprovedCliAdapter(**arguments)
    observation = adapter.run(ActionProposal(kind=tool.name))
    assert observation.outcome == "completed"
    assert observation.stdout.strip() == "verified command output"
    assert len(observation.fingerprint) == 64
    req = arguments["supervisor"].requests[0]
    assert req.argv == (sys.executable, "-V")
    assert req.cwd == arguments["candidate_root"]
    assert req.workspace_root == arguments["candidate_root"]
    assert req.sandbox_root == arguments["sandbox"].root
    assert req.environment["HOME"].startswith(str(arguments["sandbox"].root))
    assert "CANARY_HOST_ENV" not in req.environment


def test_rejects_missing_permission_before_supervision(tmp_path: Path):
    arguments, tool = setup(tmp_path)
    arguments["capabilities"] = CapabilityContext(())
    with pytest.raises(ScopeDeniedError):
        ApprovedCliAdapter(**arguments).run(ActionProposal(kind=tool.name))
    assert not arguments["supervisor"].requests


def test_rejects_missing_capability_in_task_contract(tmp_path: Path):
    arguments, tool = setup(tmp_path)
    arguments["task_contract"] = {"capabilities": ()}
    with pytest.raises(ScopeDeniedError):
        ApprovedCliAdapter(**arguments).run(ActionProposal(kind=tool.name))


def test_unregistered_executable_denied(tmp_path: Path):
    arguments, tool = setup(tmp_path)
    arguments["pinned_executables"] = {}
    with pytest.raises(ScopeDeniedError):
        ApprovedCliAdapter(**arguments).run(ActionProposal(kind=tool.name))


def test_executable_hash_change_refused(tmp_path: Path):
    arguments, tool = setup(tmp_path)
    arguments["pinned_executables"] = {sys.executable: "0" * 64}
    with pytest.raises(StateMismatchError):
        ApprovedCliAdapter(**arguments).run(ActionProposal(kind=tool.name))


def test_mutation_uses_existing_durable_gateway_not_this_adapter(tmp_path: Path):
    arguments, tool = setup(tmp_path, mutates=True)
    with pytest.raises(ScopeDeniedError):
        ApprovedCliAdapter(**arguments).run(ActionProposal(kind=tool.name))


def test_untrusted_dynamic_arguments_fail_closed(tmp_path: Path):
    arguments, tool = setup(tmp_path, allow_arguments=True)
    with pytest.raises(ScopeDeniedError):
        ApprovedCliAdapter(**arguments).run(
            ActionProposal(kind=tool.name, arguments=("; rm -rf /",))
        )
    assert not arguments["supervisor"].requests


def test_operator_supplied_exact_argument_policy(tmp_path: Path):
    arguments, tool = setup(tmp_path, allow_arguments=True)

    def policy(argv: tuple[str, ...]) -> None:
        if argv != ("--version",):
            raise ScopeDeniedError("not permitted")

    arguments["argument_policies"] = {tool.name: policy}
    adapter = ApprovedCliAdapter(**arguments)
    assert (
        adapter.run(ActionProposal(kind=tool.name, arguments=("--version",))).outcome == "completed"
    )
    with pytest.raises(ScopeDeniedError):
        adapter.run(ActionProposal(kind=tool.name, arguments=("&&", "rm", "-rf", "/")))
    assert len(arguments["supervisor"].requests) == 1


def test_untrusted_workspace_path_rejected(tmp_path: Path):
    arguments, tool = setup(tmp_path)
    arguments["candidate_root"] = tmp_path
    with pytest.raises(ScopeDeniedError):
        ApprovedCliAdapter(**arguments)
    arguments["candidate_root"] = arguments["sandbox"].root / "candidates" / "candidate-one"
    adapter = ApprovedCliAdapter(**arguments)
    (adapter.candidate_root / "nested").mkdir()
    with pytest.raises((ScopeDeniedError, InvalidInputError)):
        adapter.run(ActionProposal(kind=tool.name, working_directory="../.."))


def test_no_shell_wrapper_entrypoints(tmp_path: Path):
    arguments, tool = setup(tmp_path)
    shell = Path("/bin/sh")
    if not shell.exists():
        pytest.skip("system shell fixture not installed")
    altered = replace(tool, argv_prefix=(str(shell), "-c", "echo unsafe"))
    arguments["registry"] = ToolRegistry((altered,))
    arguments["pinned_executables"][str(shell)] = hashlib.sha256(shell.read_bytes()).hexdigest()
    with pytest.raises(ScopeDeniedError):
        ApprovedCliAdapter(**arguments).run(ActionProposal(kind=tool.name))


def test_secrets_not_injected_to_cli(tmp_path: Path):
    arguments, tool = setup(tmp_path)
    arguments["registry"] = ToolRegistry(
        (replace(tool, secret_refs=(("TOKEN", "secret://something"),)),)
    )
    with pytest.raises(ScopeDeniedError):
        ApprovedCliAdapter(**arguments).run(ActionProposal(kind=tool.name))


def test_no_isolation_receipt_fails_closed(tmp_path: Path):
    arguments, tool = setup(tmp_path)
    arguments["supervisor"] = RecordingSupervisor(isolation_identity=None)
    with pytest.raises(StateMismatchError):
        ApprovedCliAdapter(**arguments).run(ActionProposal(kind=tool.name))


def test_forbidden_process_environment_denied(tmp_path: Path):
    arguments, tool = setup(tmp_path)
    arguments["registry"] = ToolRegistry((replace(tool, environment=(("TOKEN", "plaintext"),)),))
    with pytest.raises(ScopeDeniedError):
        ApprovedCliAdapter(**arguments).run(ActionProposal(kind=tool.name))
