"""Exercise the real guest queue process and its one-use Context exchanges."""

import hashlib
import json
import select
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.tools import queue_transport_worker as worker


@pytest.mark.skipif(sys.platform != "linux", reason="Queue worker requires native Linux ownership")
def test_lost_directed_result_is_reconciled_with_the_same_effect(tmp_path):
    executable = Path(worker.__file__).resolve()
    target, effect = str(uuid4()), str(uuid4())
    subprocess.run(
        [sys.executable, "-I", str(executable), "init", target],
        cwd=tmp_path,
        check=True,
        timeout=8,
    )
    intent = {"effect_id": effect, "message": "one"}

    def drive(mode, *, acknowledge):
        with subprocess.Popen(
            [sys.executable, "-I", str(executable), "directed", mode, target],
            cwd=tmp_path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as process:
            assert process.stdin and process.stdout

            def receive():
                assert select.select([process.stdout], [], [], 8)[0]
                (length,) = struct.unpack("!I", process.stdout.read(4))
                assert 0 < length <= worker.MAX_FRAME
                return json.loads(process.stdout.read(length))

            def respond(request, value):
                raw = worker.encode(
                    {
                        "schema": worker.CONTEXT_SCHEMA,
                        "request_id": request["request_id"],
                        "ok": True,
                        "result": value,
                    }
                )
                process.stdin.write(struct.pack("!I", len(raw)) + raw)
                process.stdin.flush()

            try:
                first = receive()
                assert first["method"] == "work.input"
                respond(
                    first,
                    {
                        "revision": 1,
                        "work": {
                            "schema_version": 1,
                            "operation_ref": worker.operation_ref(target, mode),
                            "invocation_id": str(uuid4()),
                            "input": {"type_ref": worker.REQUEST, "value": intent},
                        },
                    },
                )
                second = receive()
                assert second["method"] == "work.result"
                assert second["request_id"] != first["request_id"]
                result = second["result"]
                if acknowledge:
                    respond(
                        second,
                        {
                            "revision": 1,
                            "sha256": hashlib.sha256(worker.encode(result)).hexdigest(),
                        },
                    )
                process.stdin.close()
                assert (process.wait(timeout=8) == 0) is acknowledge
                return result
            finally:
                if process.poll() is None:
                    process.kill()

    applied = drive("apply", acknowledge=False)
    recovered = drive("resolve", acknowledge=True)
    assert recovered == applied
    assert applied["value"]["effect_id"] == effect and applied["value"]["outcome"] == "applied"
    with sqlite3.connect(tmp_path / worker.DB) as db:
        assert db.execute("SELECT effect_id, outcome, message FROM queue").fetchall() == [
            (effect, "applied", "one"),
        ]
