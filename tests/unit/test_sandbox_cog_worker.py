"""Worker contracts using real image pins/leases; process execution is a unit double.

These tests do not qualify a kernel sandbox. The separate live S46 runner uses
SandboxedCogExecutor's constructor and the audited ProcessSupervisor unchanged.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    CapabilityUnavailableError,
    IndeterminateExecutionError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.tools.execution_contracts import ExecutionPolicy, WorkBinding
from potbot.tools.sandbox_cog_worker import (
    SandboxedCogExecutor,
    SandboxedCogOperation,
    _one_json,
    require_isolated_work,
)
from potbot.tools.shared_workspace_isolation import SharedWorkspaceRuntime
from potbot.tools.syn_worker_nodes import SynWorkerNodeMesh
from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger

_BINARY = b"admitted binary"


def _op() -> SandboxedCogOperation:
    policy = ExecutionPolicy(
        "a" * 64, "b" * 64, "c" * 64, "test", max_wall_ms=5000, max_output_bytes=16384
    )
    binding = WorkBinding(
        "tool", "syn://thing/contract-cog", "syn://type/in", "syn://type/out",
        "d" * 64, "read_only", policy,
    )
    return SandboxedCogOperation(
        binding, ("/usr/bin/potbot-worker",), hashlib.sha256(_BINARY).hexdigest()
    )


def _fixture(tmp_path: Path):
    workspace, agent, principal = uuid4(), uuid4(), uuid4()
    home, rootfs, private = (tmp_path / name for name in ("home", "rootfs", "private"))
    for path in (home / "agent", rootfs / "usr/bin", private):
        path.mkdir(parents=True)
    binary = rootfs / "usr/bin/potbot-worker"
    binary.write_bytes(_BINARY)
    binary.chmod(0o755)
    runtime = SharedWorkspaceRuntime("b" * 64, rootfs, home, "f" * 64)
    # Use the existing ledger implementation, not an empty namespace or a
    # monkeypatched guard. This does not introduce a new persistence backend.
    ledger = WorkspaceLeaseLedger(private / "leases.sqlite", workspace_home=home)
    ledger.pin_image(
        workspace, digest=runtime.image_digest,
        image_ref="ghcr.io/test/system@sha256:" + runtime.image_digest,
        build_commit="c" * 40, platform="linux/amd64", rootfs_path=rootfs,
        qualification_sha256=runtime.qualification_sha256,
    )
    registered = SimpleNamespace(
        workspace_id=workspace, agent_id=agent, directory="agent", uid=555, gid=69
    )
    # Deliberately isolate the process boundary in this unit test, not image
    # admission, executable hashing or the lease lifecycle under test.
    executor = SandboxedCogExecutor.__new__(SandboxedCogExecutor)
    executor.runtime, executor.ledger = runtime, ledger
    executor.homes = SimpleNamespace(lookup=lambda *_: registered)
    executor.syn_mesh = SynWorkerNodeMesh(interval_seconds=0.05)
    requests = []
    result = SimpleNamespace(
        processes_stopped=True, outcome="completed", exit_code=0,
        stdout=json.dumps({
            "schema_version": 1, "type_ref": "syn://type/out", "value": {"done": True}
        }).encode(), stdout_truncated=False, isolation_identity="unit-process-double",
    )

    def run(request, *, cancelled):
        assert cancelled() is False
        requests.append(request)
        return result

    executor.supervisor = SimpleNamespace(run=run)
    return SimpleNamespace(
        executor=executor, actor=WorkspaceContext(workspace, principal), agent=agent,
        home=registered, requests=requests, result=result, binary=binary,
    )


def _context():
    return CogRunContext("a" * 64, uuid4(), uuid4(), uuid4())


def _run(fixture, context=None):
    return asyncio.run(fixture.executor.execute(
        _op(), TypedCogValue("syn://type/in", {"args": "untrusted"}),
        context or _context(), fixture.actor, fixture.agent,
    ))


def test_in_process_host_callback_cannot_be_installed_as_a_worker() -> None:
    operation = _op()
    require_isolated_work((operation,))
    with pytest.raises(ScopeDeniedError, match="host callbacks"):
        require_isolated_work((SimpleNamespace(binding=operation.binding, handler=lambda: None),))
    with pytest.raises(ScopeDeniedError):
        require_isolated_work([operation])
    with pytest.raises(InvalidInputError):
        SandboxedCogOperation(operation.binding, ("relative-executable",), "a" * 64)


def test_typed_result_rejects_duplicate_keys_and_unpinned_output() -> None:
    assert _one_json(
        b'{"schema_version":1,"type_ref":"syn://type/out","value":{"ok":true}}'
    )["value"] == {"ok": True}
    for raw in (
        b'{}', b'{"schema_version":1,"schema_version":1,"type_ref":"x","value":1}',
        b'{"schema_version":1,"type_ref":"x","value":NaN}',
        b'{"schema_version":1,"type_ref":"x","value":1}garbage',
    ):
        with pytest.raises(StateMismatchError):
            _one_json(raw)


def test_sandbox_cog_controller_passes_registered_uid_and_does_not_execute_callback(tmp_path):
    fixture, context = _fixture(tmp_path), _context()
    result = _run(fixture, context)
    assert result.type_ref == "syn://type/out" and result.value == {"done": True}
    assert len(fixture.requests) == 1
    request = fixture.requests[0]
    assert (request.guest_uid, request.guest_gid) == (555, 69)
    assert request.telemetry_workspace_id == fixture.actor.workspace_id
    assert request.telemetry_agent_id == fixture.agent
    assert request.telemetry_identity_kind == "agent"
    assert request.telemetry_run_id == context.invocation_id
    assert json.loads(request.stdin)["input"]["value"] == {"args": "untrusted"}
    assert request.argv == ("/usr/bin/potbot-worker",)
    # A different invocation can claim the home only after successful release.
    assert _run(fixture).value == {"done": True}
    assert len(fixture.requests) == 2


def test_sandbox_cog_guest_cannot_swap_semantic_port(tmp_path):
    fixture = _fixture(tmp_path)
    fixture.result.stdout = b'{"schema_version":1,"type_ref":"syn://type/malicious","value":123}'
    with pytest.raises(StateMismatchError, match="semantic output"):
        _run(fixture)
    # The possibly dispatched attempt remains fenced, even though its process
    # double says it exited. No fresh invocation may silently retry its effects.
    with pytest.raises((ScopeDeniedError, StateMismatchError)):
        _run(fixture)
    assert len(fixture.requests) == 1


@pytest.mark.parametrize("field", ["image_digest", "qualification_sha256", "rootfs", "home"])
def test_changed_runtime_pin_prevents_dispatch(tmp_path, field):
    fixture = _fixture(tmp_path)
    changed = "e" * 64
    if field in {"rootfs", "home"}:
        changed = tmp_path / ("changed-" + field)
        changed.mkdir()
    fixture.executor.runtime = replace(fixture.executor.runtime, **{field: changed})
    with pytest.raises(StateMismatchError, match="pinned qualified image"):
        _run(fixture)
    assert fixture.requests == []


def test_tampered_pinned_binary_prevents_dispatch(tmp_path):
    fixture = _fixture(tmp_path)
    fixture.binary.write_bytes(b"replacement worker")
    with pytest.raises(StateMismatchError, match="binary changed"):
        _run(fixture)
    assert fixture.requests == []


def test_unknown_agent_prevents_dispatch(tmp_path):
    fixture = _fixture(tmp_path)
    fixture.executor.homes = SimpleNamespace(lookup=lambda *_: None)
    with pytest.raises(ScopeDeniedError, match="not registered"):
        _run(fixture)
    assert fixture.requests == []


def test_unconfirmed_process_exit_retains_the_workspace_fence(tmp_path):
    fixture = _fixture(tmp_path)
    fixture.result.processes_stopped = False
    with pytest.raises(IndeterminateExecutionError, match="not confirmed stopped"):
        _run(fixture)
    with pytest.raises((ScopeDeniedError, StateMismatchError)):
        _run(fixture)
    assert len(fixture.requests) == 1


def test_worker_never_dispatches_without_operator_syn_mesh():
    executor = SandboxedCogExecutor.__new__(SandboxedCogExecutor)
    with pytest.raises(CapabilityUnavailableError, match="SYN node"):
        asyncio.run(executor.execute(
            _op(), TypedCogValue("syn://type/in", {}), _context(),
            WorkspaceContext(uuid4(), uuid4()), uuid4(),
        ))
