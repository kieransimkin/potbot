"""The untrusted Cog payload can only reach a qualified isolated worker."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.tools.execution_contracts import WorkBinding, ExecutionPolicy
from potbot.tools.syn_worker_nodes import SynWorkerNodeMesh
from potbot.tools.sandbox_cog_worker import (
    SandboxedCogExecutor, SandboxedCogOperation, _one_json, require_isolated_work,
)


def _op() -> SandboxedCogOperation:
    policy = ExecutionPolicy("a" * 64, "b" * 64, "c" * 64, "test",
                             max_wall_ms=5000, max_output_bytes=16384)
    binding = WorkBinding("tool", "syn://thing/contract-cog",
                          "syn://type/in", "syn://type/out", "d" * 64,
                          "read_only", policy)
    return SandboxedCogOperation(binding,
                                 ("/usr/bin/potbot-worker",),
                                 hashlib.sha256(b"admitted binary").hexdigest())


def test_in_process_host_callback_cannot_be_installed_as_a_worker() -> None:
    operation = _op()
    require_isolated_work((operation,))
    with pytest.raises(ScopeDeniedError, match="host callbacks"):
        require_isolated_work((SimpleNamespace(binding=operation.binding, handler=lambda: None),))
    with pytest.raises(ScopeDeniedError):
        require_isolated_work([operation])
    with pytest.raises(InvalidInputError):
        SandboxedCogOperation(operation.binding,
                              ("relative-executable",), "a"*64)


def test_typed_result_rejects_duplicate_keys_and_unpinned_output() -> None:
    assert _one_json(b'{"schema_version":1,"type_ref":"syn://type/out","value":{"ok":true}}')["value"] == {"ok": True}
    for raw in (b'{}', b'{"schema_version":1,"schema_version":1,"type_ref":"x","value":1}',
                b'{"schema_version":1,"type_ref":"x","value":NaN}',
                b'{"schema_version":1,"type_ref":"x","value":1}garbage'):
        with pytest.raises(StateMismatchError):
            _one_json(raw)


def test_sandbox_cog_controller_passes_registered_uid_and_does_not_execute_callback(tmp_path: Path):
    ws, agent, principal, invocation = uuid4(), uuid4(), uuid4(), uuid4()
    home = tmp_path/'home';agent_home=home/'agent';agent_home.mkdir(parents=True)
    executor = SandboxedCogExecutor.__new__(SandboxedCogExecutor)
    executor.runtime = SimpleNamespace(home=home, image_digest='b'*64)
    executor.ledger = SimpleNamespace()
    executor.homes = SimpleNamespace(lookup=lambda *_: SimpleNamespace(
        workspace_id=ws, agent_id=agent, directory='agent', uid=555, gid=69))
    requests=[]
    output = {"schema_version":1,"type_ref":"syn://type/out","value":{"done":True}}
    class Supervisor:
        def run(self, request, *, cancelled):
            assert cancelled() is False
            requests.append(request)
            return SimpleNamespace(processes_stopped=True,outcome="completed",exit_code=0,
                                   stdout=json.dumps(output).encode(), stdout_truncated=False,
                                   isolation_identity="bwrap-pinned")
    executor.supervisor=Supervisor()
    executor.syn_mesh=SynWorkerNodeMesh(interval_seconds=0.05)
    executor._executable=lambda op: None
    op=_op()
    value=TypedCogValue('syn://type/in',{'args':'untrusted'})
    result=asyncio.run(executor.execute(op,value,CogRunContext("a" * 64, uuid4(), uuid4(), invocation),WorkspaceContext(ws,principal),agent))
    assert result.type_ref == 'syn://type/out' and result.value == {'done':True}
    assert len(requests)==1
    request=requests[0]
    assert request.guest_uid == 555 and request.guest_gid == 69
    assert request.telemetry_workspace_id == ws
    assert request.telemetry_agent_id == agent
    assert request.telemetry_identity_kind == 'agent'
    assert request.telemetry_run_id == invocation
    assert json.loads(request.stdin)['input']['value']=={'args':'untrusted'}
    assert request.argv==('/usr/bin/potbot-worker',)


def test_sandbox_cog_guest_cannot_swap_semantic_port(tmp_path: Path):
    ws, agent, principal=uuid4(),uuid4(),uuid4()
    root=tmp_path/'root';(root/'agent').mkdir(parents=True)
    executor=SandboxedCogExecutor.__new__(SandboxedCogExecutor)
    executor.runtime=SimpleNamespace(home=root,image_digest='c'*64)
    executor.ledger=SimpleNamespace()
    executor.homes=SimpleNamespace(lookup=lambda *_:SimpleNamespace(workspace_id=ws,agent_id=agent,
                                                                       directory='agent',uid=500,gid=69))
    executor._executable=lambda op:None
    executor.supervisor=SimpleNamespace(run=lambda request,**kw:SimpleNamespace(
        processes_stopped=True,outcome='completed',exit_code=0,stdout_truncated=False,
        stdout=b'{"schema_version":1,"type_ref":"syn://type/malicious","value":123}'))
    with pytest.raises(StateMismatchError,match='semantic output'):
        executor._run_sync(_op(),TypedCogValue('syn://type/in',{}),CogRunContext("a" * 64, uuid4(), uuid4(), uuid4()),
                           WorkspaceContext(ws,principal),agent,__import__('threading').Event())


def test_worker_never_dispatches_without_operator_syn_mesh():
    from potbot.domain.errors import CapabilityUnavailableError
    executor = SandboxedCogExecutor.__new__(SandboxedCogExecutor)
    op = _op()
    with pytest.raises(CapabilityUnavailableError, match="SYN node"):
        asyncio.run(executor.execute(op, TypedCogValue("syn://type/in", {}),
            CogRunContext("a" * 64, uuid4(), uuid4(), uuid4()),
            WorkspaceContext(uuid4(), uuid4()), uuid4()))
