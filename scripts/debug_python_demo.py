"""Run only Potbot's authored Python fixture on the explicitly trusted local host.

This developer acceptance harness is NOT an application execution adapter or a
sandbox qualification. Production composition uses SandboxedDebugger. No option
accepts an arbitrary program, PID, remote endpoint or debugger executable.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import signal
import sys
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from potbot.debugging import DapAdapter, DebugIdentity, DebugSession
from potbot.debugging.acceptance import python_walkthrough
from potbot.debugging.python import DEBUGPY_VERSION
from potbot.domain.errors import ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash


async def demonstrate(output: Path, *, fail: bool = False) -> dict:
    import debugpy

    assert debugpy.__version__ == DEBUGPY_VERSION
    root = Path(__file__).resolve().parents[1]
    work = output / ("exception-target" if fail else "success-target")
    work.mkdir(parents=True, exist_ok=False)
    program = work / "nested.py"
    shutil.copyfile(root / "examples/debugging/nested.py", program)
    identity = DebugIdentity(
        uuid4(),
        uuid4(),
        work,
        (("nested.py", hashlib.sha256(program.read_bytes()).hexdigest()),),
        "debugpy",
        DEBUGPY_VERSION,
        f"Python {sys.version.split()[0]}",
        "synny://type/workflow-execution",
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"SystemRoot", "SYSTEMROOT", "WINDIR", "PATH"}
    }
    environment.update(
        {
            "TEMP": str(output),
            "TMP": str(output),
            "TMPDIR": str(output),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    logs = work / "adapter-logs"
    logs.mkdir()
    kwargs = {"start_new_session": True} if os.name != "nt" else {}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-B",
        "-m",
        "debugpy.adapter",
        "--log-dir",
        str(logs),
        cwd=work,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=2 * 1024**2,
        **kwargs,
    )
    assert process.stdout is not None and process.stdin is not None
    events = []
    artifacts = output / "artifacts"
    artifacts.mkdir(exist_ok=True)

    def capture(data):
        digest = content_hash(data)
        (artifacts / f"{digest}.json").write_text(canonical_json(data), encoding="utf-8")
        return f"fixture-artifact:sha256:{digest}"

    def admit(operation, arguments):
        if operation in {"debug.attach", "debug.evaluate"}:
            raise ScopeDeniedError("Acceptance fixture does not grant attach/evaluation")
        if operation == "debug.launch" and arguments.get("program") != str(program):
            raise ScopeDeniedError("Only the exact authored fixture is admitted")

    session = DebugSession(
        DapAdapter(process.stdout, process.stdin),
        identity,
        admit=admit,
        record=events.append,
        capture_output=capture,
    )
    completed = False
    try:
        await session.initialize()
        observations = await python_walkthrough(
            session, program=program, python=Path(sys.executable), fail=fail
        )
        completed = True
    finally:
        try:
            await session.close(terminate_launched=True)
        finally:
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                await process.wait()
                completed = False
    result = {
        "schema": "potbot-debugger-acceptance-v1",
        "result": "passed" if completed else "failed",
        "mode": "trusted_local_authored_fixture_NOT_sandbox_qualification",
        "runtime": identity.runtime,
        "adapter": f"debugpy {debugpy.__version__}",
        "source_sha256": dict(identity.source_sha256),
        "adapter_exited": process.returncode is not None,
        "target_process_id": session.target_process_id,
        "target_exit_code": session.exit_code,
        "observations": observations,
        "execution_events": [asdict(e) for e in events],
    }
    (output / ("exception.json" if fail else "walkthrough.json")).write_text(
        json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trusted-local-fixture", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    for fail in (False, True):
        result = asyncio.run(demonstrate(output, fail=fail))
        print(
            json.dumps({k: result[k] for k in ("result", "runtime", "adapter", "adapter_exited")})
        )
        if result["result"] != "passed":
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
