"""Actual debugpy via registered Cogs; memory journals are NOT durable acceptance."""

import asyncio
import hashlib
import json
import os
import shutil
import signal
import sys
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.cogs.debugger import cog_ref, signature
from potbot.cogs.registry import BoundCogRegistry
from potbot.controller.cog_work import CogWorkMonitor
from potbot.debugging import DapAdapter, DebugIdentity, DebugSession
from potbot.debugging.cog_acceptance import python_cog_walkthrough
from potbot.debugging.cog_client import DebuggerCogClient
from potbot.debugging.cog_runtime import DebuggerCogRuntime
from potbot.debugging.python import DEBUGPY_VERSION, launch_configuration
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError


class FixtureArtifacts:
    def __init__(self):
        self.values = {}

    def put(self, actor, data, media_type):
        digest = hashlib.sha256(data).hexdigest()
        identifier = uuid4()
        self.values[str(identifier)] = json.loads(data)
        return {"id": identifier, "digest": digest, "media_type": media_type}


class FixtureEvents:
    def __init__(self):
        self.values = []

    def append(self, actor, event):
        identifier = uuid4()
        value = {
            **asdict(event),
            "id": str(identifier),
            "event_ref": f"syn://event/{identifier}",
            "sequence": len(self.values) + 1,
            "telemetry": json.loads(event.telemetry_json),
        }
        self.values.append(value)
        return value


def assert_lifecycles(events, outcomes):
    for outcome in outcomes:
        records = [e for e in events if str(e["run_id"]) == str(outcome.receipt.invocation_id)]
        assert records[0]["stage"] == "started"
        assert records[-1]["stage"] == "completed"
        assert sum(e["stage"] == "completed" for e in records) == 1
        assert any(e["stage"] == "updated" for e in records)
        for previous, current in zip(records, records[1:], strict=False):
            assert str(current["parent_event_id"]) == previous["id"]
            assert current["trigger_ref"] == previous["event_ref"]
            assert current["sequence"] > previous["sequence"]
        telemetry = records[-1]["telemetry"]
        assert telemetry["cog_ref"] == outcome.receipt.cog_ref
        assert telemetry["clock_basis"] == "monotonic"
        assert telemetry["finished_at"] >= telemetry["started_at"]
        wall = telemetry["metrics"]["elapsed_wall_ms"]
        assert wall["value"] >= 0 and wall["unit"] == "ms" and wall["provenance"] == "measured"
        assert telemetry["metrics"]["target_cpu_ms"]["provenance"] == "unavailable"
        assert telemetry["metrics"]["target_cpu_ms"]["value"] is None
        assert records[-1]["output_sha256"] and records[-1]["output_artifact_ref"]


@pytest.mark.parametrize("fail", [False, True])
def test_actual_python_cogs_break_step_stack_and_statistics(tmp_path, fail):
    import debugpy

    assert debugpy.__version__ == DEBUGPY_VERSION
    program = tmp_path / "nested.py"
    shutil.copyfile(Path(__file__).resolve().parents[2] / "examples/debugging/nested.py", program)
    actor = WorkspaceContext(uuid4(), uuid4())
    identity = DebugIdentity(
        actor.workspace_id,
        uuid4(),
        tmp_path,
        (("nested.py", hashlib.sha256(program.read_bytes()).hexdigest()),),
        "debugpy",
        DEBUGPY_VERSION,
        f"Python {sys.version.split()[0]}",
        "synny://type/workflow-execution",
    )
    events, artifacts = FixtureEvents(), FixtureArtifacts()
    registry = BoundCogRegistry.debugger()
    observed = []

    async def scenario():
        environment = {
            k: v
            for k, v in os.environ.items()
            if k in {"SystemRoot", "SYSTEMROOT", "WINDIR", "PATH"}
        }
        environment.update({"TEMP": str(tmp_path), "TMP": str(tmp_path), "TMPDIR": str(tmp_path)})
        logs = tmp_path / "adapter-logs"
        logs.mkdir()
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-B",
            "-m",
            "debugpy.adapter",
            "--log-dir",
            str(logs),
            cwd=tmp_path,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=os.name != "nt",
        )
        assert process.stdin is not None and process.stdout is not None
        session = DebugSession(
            DapAdapter(process.stdout, process.stdin, timeout_s=60),
            identity,
            admit=lambda *_: None,
            record=lambda _: None,
            timeout_s=60,
        )
        config = launch_configuration(
            identity,
            program=program,
            python=Path(sys.executable),
            arguments=("--fail",) if fail else (),
        )

        def admit(selected, source, context):
            if (
                selected.cog_ref == signature("attach").cog_ref
                or selected.cog_ref == signature("evaluate").cog_ref
            ):
                raise ScopeDeniedError("Fixture grants neither arbitrary attach nor evaluation")
            if (
                selected.cog_ref == signature("launch").cog_ref
                and source.value.get("configuration") != config
            ):
                raise ScopeDeniedError("Fixture admits only its exact authored target")
            observed.append(str(selected.cog_ref))

        runtime = DebuggerCogRuntime(
            session,
            CogWorkMonitor(
                events,
                artifacts,
                actor,
                emitter_run_id=identity.emitter_run_id,
                event_type_ref=identity.event_type_ref,
            ),
            task_revision_id=uuid4(),
            repository_state_id=uuid4(),
            catalog_hash=registry.snapshot.fingerprint,
            admit=admit,
        )
        client = DebuggerCogClient(registry, runtime)
        try:
            # Direct handler bypass and foreign task context fail before adapter work.
            with pytest.raises(ScopeDeniedError):
                await runtime.dispatch("initialize", {"schema_version": 1})
            context = CogRunContext(
                registry.snapshot.fingerprint, uuid4(), runtime.repository_state, uuid4()
            )
            with pytest.raises(ScopeDeniedError):
                await registry.invoke(
                    cog_ref("initialize"),
                    TypedCogValue(signature("initialize").input_type_ref, {"schema_version": 1}),
                    context,
                    controller=runtime,
                )
            assert not events.values and not observed
            observations = await python_cog_walkthrough(
                client, program=program, configuration=config, fail=fail
            )
            assert_lifecycles(events.values, client.history)
            assert {
                "breakpoint",
                "step_over",
                "step_into",
                "step_out",
                "expired_frame_rejected",
            } <= {x["action"] for x in observations}
            entered = next(x for x in observations if x["action"] == "step_into")["frames"]
            assert entered[0]["function"] == "leaf" and entered[0]["locals"]["value"] == "10"
            assert entered[1]["function"] == "outer" and entered[1]["locals"]["seed"] == "5"
            (tmp_path / "cog-walkthrough.json").write_text(
                json.dumps(
                    {
                        "result": "passed",
                        "qualification": "real_python_memory_journal_NOT_durable_or_sandbox",
                        "observations": observations,
                        "events": events.values,
                        "receipts": [x.receipt.to_dict() for x in client.history],
                        "artifacts": artifacts.values,
                    },
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
        finally:
            await session.close(terminate_launched=True)
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        assert process.returncode is not None

    asyncio.run(scenario())


def test_debug_cog_registry_rejects_handler_and_ember_drift():
    registry = BoundCogRegistry.debugger()
    selected = registry.snapshot.objects[-1]
    payload = json.loads(selected.payload_json)
    payload["implementationRevision"] += 1
    altered = replace(selected, payload_json=json.dumps(payload))
    snapshot = replace(registry.snapshot, objects=(*registry.snapshot.objects[:-1], altered))
    with pytest.raises(ScopeDeniedError):
        BoundCogRegistry(snapshot, execution_profile=True)
    payload["requiresControllerScope"] = False
    altered = replace(selected, payload_json=json.dumps(payload))
    with pytest.raises(ScopeDeniedError):
        BoundCogRegistry(
            replace(registry.snapshot, objects=(*registry.snapshot.objects[:-1], altered)),
            execution_profile=True,
        )


def test_actual_python_attach_detach_preserves_owned_target(tmp_path):
    """Already admitted loopback pipes; never scan/attach by arbitrary PID/endpoint."""
    body = (Path(__file__).resolve().parents[2] / "examples/debugging/nested.py").read_text()
    spawn_nonce = uuid4().hex
    prefix = (
        "import debugpy, json, os\n"
        "debugpy.configure(subProcess=False)\n"
        'host, port = debugpy.listen(("127.0.0.1", 0))\n'
        f'print(json.dumps({{"host": host, "port": port, "pid": os.getpid(), "nonce": "{spawn_nonce}"}}), flush=True)\n'
        "debugpy.wait_for_client()\n"
    )
    program = tmp_path / "owned-attached.py"
    program.write_text(
        prefix + body + '\nprint("owner-gate", flush=True)\nsys.stdin.readline()\n',
        encoding="utf-8",
    )
    actor = WorkspaceContext(uuid4(), uuid4())
    identity = DebugIdentity(
        actor.workspace_id,
        uuid4(),
        tmp_path,
        ((program.name, hashlib.sha256(program.read_bytes()).hexdigest()),),
        "debugpy",
        DEBUGPY_VERSION,
        f"Python {sys.version.split()[0]}",
        "synny://type/workflow-execution",
    )
    events, artifacts, registry = FixtureEvents(), FixtureArtifacts(), BoundCogRegistry.debugger()

    async def scenario():
        environment = {
            k: v
            for k, v in os.environ.items()
            if k in {"SystemRoot", "SYSTEMROOT", "WINDIR", "PATH"}
        }
        environment.update({"TEMP": str(tmp_path), "TMP": str(tmp_path), "TMPDIR": str(tmp_path)})
        target = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-B",
            str(program),
            cwd=tmp_path,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=os.name != "nt",
        )
        session = None
        try:
            endpoint = json.loads(await asyncio.wait_for(target.stdout.readline(), 60))
            # A Windows venv redirector can have a different PID from Python.
            assert endpoint["nonce"] == spawn_nonce and endpoint["host"] == "127.0.0.1"
            assert type(endpoint["pid"]) is int and endpoint["pid"] > 0
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(endpoint["host"], endpoint["port"]), 10
            )
            session = DebugSession(
                DapAdapter(reader, writer, timeout_s=60),
                identity,
                admit=lambda *_: None,
                record=lambda _: None,
                timeout_s=60,
            )
            config = {"justMyCode": True, "subProcess": False}

            def admit(selected, source, context):
                if target.returncode is not None or selected.cog_ref in {
                    signature("launch").cog_ref,
                    signature("terminate").cog_ref,
                    signature("evaluate").cog_ref,
                }:
                    raise ScopeDeniedError("Only live owned attach/detach is admitted")
                if (
                    selected.cog_ref == signature("attach").cog_ref
                    and source.value["configuration"] != config
                ):
                    raise ScopeDeniedError("Wrong authored attach configuration")

            runtime = DebuggerCogRuntime(
                session,
                CogWorkMonitor(
                    events,
                    artifacts,
                    actor,
                    emitter_run_id=identity.emitter_run_id,
                    event_type_ref=identity.event_type_ref,
                ),
                task_revision_id=uuid4(),
                repository_state_id=uuid4(),
                catalog_hash=registry.snapshot.fingerprint,
                admit=admit,
            )
            client = DebuggerCogClient(registry, runtime)
            observations = await python_cog_walkthrough(
                client, program=program, configuration=config, attached=True
            )
            assert target.returncode is None and observations[-1]["action"] == "detached"
            assert session.target_process_id == endpoint["pid"]
            assert b"fixture result: 33" in await asyncio.wait_for(target.stdout.readline(), 10)
            assert b"owner-gate" in await asyncio.wait_for(target.stdout.readline(), 10)
            assert target.returncode is None
            assert_lifecycles(events.values, client.history)
            target.stdin.write(b"owner exit\n")
            await target.stdin.drain()
            assert await asyncio.wait_for(target.wait(), 10) == 0
            (tmp_path / "attach-cog-walkthrough.json").write_text(
                json.dumps(
                    {
                        "result": "passed",
                        "observations": observations,
                        "events": events.values,
                        "detached_process_preserved": True,
                        "owner_exit_code": 0,
                    },
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
        finally:
            if session is not None:
                await session.close()
            if target.returncode is None:
                if os.name == "nt":
                    cleanup = await asyncio.create_subprocess_exec(
                        "taskkill",
                        "/PID",
                        str(target.pid),
                        "/T",
                        "/F",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await cleanup.wait()
                else:
                    os.killpg(target.pid, signal.SIGKILL)
                await target.wait()

    asyncio.run(scenario())
