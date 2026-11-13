"""Opt-in debugpy qualification over one inherited socket into a private WSL namespace."""

import asyncio
import hashlib
import json
import os
import signal
import socket
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.cogs.registry import BoundCogRegistry
from potbot.controller.cog_work import CogWorkMonitor
from potbot.debugging import DapAdapter, DebugIdentity, DebugSession
from potbot.debugging.cog_acceptance import python_cog_walkthrough
from potbot.debugging.cog_client import DebuggerCogClient
from potbot.debugging.cog_runtime import DebuggerCogRuntime
from potbot.debugging.contracts import DebugAttachTarget, RemoteDebugTarget
from potbot.debugging.python import DEBUGPY_VERSION
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError
from potbot.domain.serialization import content_hash

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("POTBOT_RUN_PRIVATE_DEBUGPY") != "1",
    reason="requires an explicitly provisioned disposable Linux network namespace",
)

# This runs inside the owned target process. Record suspension before debugpy
# emits DAP events so the controller reads an independent target-side revision.
TARGET = """
import debugpy
import json
import os
import sys
from pathlib import Path

def publish(revision):
    temporary = snapshot.with_suffix(".new")
    temporary.write_text(str(revision))
    temporary.replace(snapshot)

snapshot = Path(sys.argv.pop(1))
publish(1)
debugpy.configure(subProcess=False)
host, port = debugpy.listen(("127.0.0.1", 0))
from _pydevd_bundle.pydevd_constants import get_global_debugger

notifications = type(get_global_debugger()._threads_suspended_single_notification)
original = notifications.send_suspend_notification
revision = 1

def suspended(self, thread_id, thread, stop_reason):
    global revision
    revision += 1
    publish(revision)
    return original(self, thread_id, thread, stop_reason)

notifications.send_suspend_notification = suspended
print(json.dumps({"port": port, "pid": os.getpid()}), flush=True)
debugpy.wait_for_client()
"""

# The relay serves one inherited descriptor and one target it spawned itself.
# No controller input can select a host, process, or port.
RELAY = """
import asyncio
import json
import os
import socket
import sys
async def relay():
    assert [name for _, name in socket.if_nameindex()] == ["lo"]
    channel = socket.socket(fileno=int(sys.argv[1]))
    target = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-B", "-Xfrozen_modules=off", sys.argv[2], sys.argv[3],
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    )
    try:
        endpoint = json.loads(await asyncio.wait_for(target.stdout.readline(), 30))
        assert endpoint["pid"] == target.pid
        print(json.dumps({"pid": target.pid, "relay": os.getpid()}), flush=True)
        client_reader, client_writer = await asyncio.open_connection(sock=channel)
        peer_reader, peer_writer = await asyncio.open_connection("127.0.0.1", endpoint["port"])

        async def copy(reader, writer):
            while chunk := await reader.read(16384):
                writer.write(chunk)
                await writer.drain()

        tasks = (
            asyncio.create_task(copy(client_reader, peer_writer)),
            asyncio.create_task(copy(peer_reader, client_writer)),
        )
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for writer in (client_writer, peer_writer):
                writer.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert await asyncio.wait_for(target.stdout.readline(), 10) == b"fixture result: 33\\n"
        assert await asyncio.wait_for(target.stdout.readline(), 10) == b"owner-gate\\n"
        assert target.returncode is None
        target.stdin.write(b"owner exit\\n")
        await target.stdin.drain()
        assert await asyncio.wait_for(target.wait(), 10) == 0
        print("owner-confirmed: result=33, target alive after detach, exit=0", flush=True)
    finally:
        if target.returncode is None:
            target.kill()
            await target.wait()

asyncio.run(relay())
"""


def proc_start(pid):
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


class FixtureStore:
    def __init__(self):
        self.events = []

    def put(self, actor, data, media_type):
        return {"id": uuid4(), "digest": hashlib.sha256(data).hexdigest()}

    def append(self, actor, event):
        identifier = uuid4()
        self.events.append(event)
        return {"id": str(identifier), "event_ref": f"syn://event/{identifier}"}


def test_live_debugpy_on_private_network_target(tmp_path):
    body = (Path(__file__).resolve().parents[2] / "examples/debugging/nested.py").read_text()
    program = tmp_path / "owned_target.py"
    program.write_text(
        TARGET + body + '\nprint("owner-gate", flush=True)\nsys.stdin.readline()\n',
        encoding="utf-8",
    )
    relay = tmp_path / "relay.py"
    relay.write_text(RELAY, encoding="utf-8")
    snapshot = tmp_path / "halt"

    async def scenario():
        parent, child = socket.socketpair()
        with parent, child:
            launched = await asyncio.create_subprocess_exec(
                "/usr/bin/unshare",
                "--net",
                "/bin/sh",
                "-ec",
                'ip link set lo up; exec "$@"',
                "private-debugger",
                sys.executable,
                "-I",
                "-B",
                str(relay),
                str(child.fileno()),
                str(program),
                str(snapshot),
                pass_fds=(child.fileno(),),
                stdout=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            child.close()
            session = None
            try:
                ready = json.loads(await asyncio.wait_for(launched.stdout.readline(), 40))
                pid = ready["pid"]
                namespace = os.readlink(f"/proc/{pid}/ns/net")
                assert namespace == os.readlink(f"/proc/{ready['relay']}/ns/net")
                assert namespace != os.readlink("/proc/self/ns/net")
                started = proc_start(pid)
                executable = Path(f"/proc/{pid}/exe")
                executable_stat = executable.stat()
                source_digest = hashlib.sha256(program.read_bytes()).hexdigest()
                config = {"justMyCode": True, "subProcess": False}
                actor = WorkspaceContext(uuid4(), uuid4())
                identity = DebugIdentity(
                    actor.workspace_id,
                    uuid4(),
                    tmp_path,
                    ((program.name, source_digest),),
                    "debugpy",
                    DEBUGPY_VERSION,
                    f"Python {sys.version.split()[0]}",
                    "synny://type/workflow-execution",
                )
                approved = {
                    "schema_version": 1,
                    "protocol": "python_remote",
                    "target_ref": content_hash(
                        {"namespace": namespace, "pid": pid, "start": started}
                    ),
                    "probe_ref": f"debugpy-{DEBUGPY_VERSION}",
                    "core_ref": "main-thread",
                    "firmware_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                    "symbols_sha256": source_digest,
                    "halt_revision": 1,
                    "endpoint_protected": True,
                    "effects": dict.fromkeys(("reset", "flash", "erase", "unlock"), False),
                    "capabilities": {
                        "breakpoints": "supported",
                        "step": "supported",
                        "stack": "supported",
                        "registers": "unsupported",
                        "memory": "unsupported",
                    },
                    "qualification": "qualified",
                }
                selected = dict(approved)

                def current():
                    try:
                        return (
                            launched.returncode is None
                            and proc_start(pid) == started
                            and os.readlink(f"/proc/{pid}/ns/net") == namespace
                            and os.path.samestat(executable.stat(), executable_stat)
                        )
                    except OSError:
                        return False

                def observe():
                    if not current():
                        raise OSError("owned target exited")
                    return {**selected, "halt_revision": int(snapshot.read_text())}

                reader, writer = await asyncio.open_connection(sock=parent)
                session = DebugSession(
                    DapAdapter(reader, writer),
                    identity,
                    admit=lambda *_: None,
                    record=lambda _: None,
                    attach_target=DebugAttachTarget(
                        pid, content_hash(config), current, RemoteDebugTarget(approved, observe)
                    ),
                )
                fixture = FixtureStore()
                registry = BoundCogRegistry.debugger()
                runtime = DebuggerCogRuntime(
                    session,
                    CogWorkMonitor(
                        fixture,
                        fixture,
                        actor,
                        emitter_run_id=identity.emitter_run_id,
                        event_type_ref=identity.event_type_ref,
                    ),
                    task_revision_id=uuid4(),
                    repository_state_id=uuid4(),
                    catalog_hash=registry.snapshot.fingerprint,
                    admit=lambda *_: None,
                )
                client = DebuggerCogClient(registry, runtime)
                await client.call("initialize")
                selected["probe_ref"] = "wrong-probe"
                with pytest.raises(ScopeDeniedError, match="approved identity"):
                    await client.call("attach", configuration=config)
                assert len(client.history) == 1
                selected["probe_ref"] = approved["probe_ref"]
                observations = await python_cog_walkthrough(
                    client, program=program, configuration=config, attached=True
                )
                actions = {row["action"]: row for row in observations}
                assert actions["step_into"]["frames"][0]["function"] == "leaf"
                assert actions["step_into"]["frames"][0]["locals"]["value"] == "10"
                assert "expired_frame_rejected" in actions
                assert actions["detached"]
                assert int(snapshot.read_text()) >= 6
                assert session.target_process_id == pid
                assert await asyncio.wait_for(launched.stdout.readline(), 15) == (
                    b"owner-confirmed: result=33, target alive after detach, exit=0\n"
                )
                assert await asyncio.wait_for(launched.wait(), 5) == 0
                runs = []
                for outcome in client.history:
                    events = [
                        event
                        for event in fixture.events
                        if event.run_id == outcome.receipt.invocation_id
                    ]
                    assert events[0].stage == "started" and events[-1].stage == "completed"
                    runs.append(
                        {
                            "operation": outcome.receipt.cog_ref.removeprefix(
                                "syn://cog/potbot-debug-"
                            ),
                            "progress_count": sum(event.stage == "updated" for event in events),
                            "elapsed_ms": events[-1].elapsed_ms,
                            "terminal": events[-1].stage,
                        }
                    )
                # Emit only the authored functions and selected synthetic values.
                for row in observations:
                    for frame in row.get("frames", []):
                        frame["locals"] = {
                            key: value
                            for key, value in frame["locals"].items()
                            if key in {"seed", "adjusted", "result", "value"}
                        }
                print(
                    json.dumps(
                        {
                            "schema": "potbot-public-cog-recording-v1",
                            "qualification": "Private WSL network namespace",
                            "adapter": f"debugpy {DEBUGPY_VERSION}",
                            "runtime": identity.runtime,
                            "source_sha256": source_digest,
                            "event_count": len(fixture.events),
                            "runs": runs,
                            "observations": [
                                {"action": "wrong_probe_refused"},
                                *observations,
                                {"action": "natural_exit", "exit_code": 0},
                            ],
                            "target_result": 33,
                            "target_alive_after_detach": True,
                        },
                        sort_keys=True,
                    )
                )
            finally:
                try:
                    if session is not None:
                        await session.close()
                finally:
                    if launched.returncode is None:
                        os.killpg(launched.pid, signal.SIGKILL)
                        await launched.wait()

    asyncio.run(scenario())
