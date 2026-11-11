#!/usr/bin/python3 -I
"""Pinned guest executable for the local queue and its directed work exchange."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any, BinaryIO
from uuid import UUID, uuid4

EXECUTABLE = "/usr/local/libexec/potbot-queue-transport"
DB = ".potbot-transport-queue.sqlite3"
REQUEST = "syn://type/potbot-local-queue-request-v1"
RESULT = "syn://type/potbot-local-queue-receipt-v1"
MAX_FRAME = 8192
CONTEXT_SCHEMA = "potbot-context-rpc/v1"


def encode(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def frame(stream: BinaryIO, *, newline: bool = True) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = dict(items)
        if len(result) != len(items):
            raise ValueError("duplicate transport key")
        return result

    raw = stream.read(MAX_FRAME + 1)
    if len(raw) > MAX_FRAME or (newline and (not raw.endswith(b"\n") or raw.count(b"\n") != 1)):
        raise ValueError("expected exactly one bounded stdio frame")
    value = json.loads(raw, object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("invalid transport frame")
    return value


def identity(value: Any) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("invalid transport identity")
    return value


def operation_ref(target: str, mode: str = "apply") -> str:
    suffix = "" if mode == "apply" else "-resolve"
    return "syn://thing/potbot-local-queue-" + UUID(target).hex + suffix


def request(value: Any) -> tuple[str, str, str]:
    if (
        not isinstance(value, dict)
        or set(value) != {"effect_id", "message"}
        or not isinstance(value["message"], str)
        or not 1 <= len(value["message"]) <= 512
    ):
        raise ValueError("invalid queue request")
    return identity(value["effect_id"]), value["message"], hashlib.sha256(encode(value)).hexdigest()


def provision(target: str) -> None:
    """Initialize once in a dedicated operator-selected target directory."""
    identity(target)
    with os.fdopen(os.open(DB, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb"):
        pass
    with sqlite3.connect(DB) as conn:
        conn.execute("CREATE TABLE target(id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO target VALUES(?)", (target,))
        conn.execute(
            "CREATE TABLE queue(effect_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL, "
            "outcome TEXT NOT NULL CHECK(outcome IN ('applied','cancelled')), message TEXT)"
        )


def target_result(target: str, mode: str, value: object) -> dict[str, str]:
    effect, message, digest = request(value)
    info = Path(DB).lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        raise ValueError("queue target requires its private regular database")
    with sqlite3.connect("file:" + DB + "?mode=rw", uri=True, timeout=2) as conn:
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT id FROM target LIMIT 2").fetchall() != [(target,)]:
            raise ValueError("queue target identity changed")
        existing = conn.execute(
            "SELECT request_sha256,outcome FROM queue WHERE effect_id=?", (effect,)
        ).fetchone()
        if existing is None:
            outcome = "applied" if mode == "apply" else "cancelled"
            conn.execute(
                "INSERT INTO queue VALUES(?,?,?,?)",
                (effect, digest, outcome, message if outcome == "applied" else None),
            )
        else:
            if existing[0] != digest:
                raise ValueError("effect ID already belongs to different input")
            outcome = existing[1]
    return {
        "target_id": target,
        "effect_id": effect,
        "request_sha256": digest,
        "outcome": outcome,
    }


def run_effect(target: str, mode: str, incoming: dict[str, Any]) -> dict[str, Any]:
    from io import BytesIO

    if (
        set(incoming) != {"schema_version", "operation_ref", "input", "invocation_id"}
        or incoming["schema_version"] != 1
        or incoming["operation_ref"] != operation_ref(target, mode)
        or not isinstance(incoming["input"], dict)
        or set(incoming["input"]) != {"type_ref", "value"}
        or incoming["input"]["type_ref"] != REQUEST
    ):
        raise ValueError("invalid sandbox work envelope")
    identity(incoming["invocation_id"])
    value = incoming["input"]["value"]
    effect, _, digest = request(value)
    rpc = encode({"jsonrpc": "2.0", "id": 1, "method": mode, "params": value}) + b"\n"
    response = subprocess.run(
        [sys.executable, "-I", str(Path(__file__).resolve()), "serve", mode, target],
        input=rpc,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=4,
        check=True,
    )
    reply = frame(BytesIO(response.stdout))
    result = reply.get("result")
    if (
        set(reply) != {"jsonrpc", "id", "result"}
        or (reply["jsonrpc"], reply["id"]) != ("2.0", 1)
        or not isinstance(result, dict)
        or result
        != {
            "target_id": target,
            "effect_id": effect,
            "request_sha256": digest,
            "outcome": result.get("outcome"),
        }
        or result.get("outcome") not in {"applied", "cancelled"}
    ):
        raise ValueError("queue target receipt differs from the request")
    return {"schema_version": 1, "type_ref": RESULT, "value": result}


def context_exchange(
    source: BinaryIO, sink: BinaryIO, method: str, **fields: Any
) -> dict[str, Any]:
    from io import BytesIO

    ident = str(uuid4())
    outgoing = encode({"schema": CONTEXT_SCHEMA, "request_id": ident, "method": method, **fields})
    if not 1 <= len(outgoing) <= MAX_FRAME:
        raise ValueError("queue Context request exceeds its bound")
    sink.write(struct.pack("!I", len(outgoing)) + outgoing)
    sink.flush()
    prefix = source.read(4)
    if len(prefix) != 4:
        raise ValueError("queue Context reply was lost")
    (size,) = struct.unpack("!I", prefix)
    if not 1 <= size <= MAX_FRAME:
        raise ValueError("queue Context reply exceeds its bound")
    raw = source.read(size)
    if len(raw) != size:
        raise ValueError("queue Context reply was truncated")
    reply = frame(BytesIO(raw), newline=False)
    if (
        set(reply) != {"schema", "request_id", "ok", "result"}
        or reply["schema"] != CONTEXT_SCHEMA
        or reply["request_id"] != ident
        or reply["ok"] is not True
        or not isinstance(reply["result"], dict)
    ):
        raise ValueError("queue Context reply does not match its request")
    return reply["result"]


def main() -> int:
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "init":
        provision(args[1])
        return 0
    entry = args[0] if len(args) == 3 else None
    if entry is not None and entry not in {"serve", "directed"}:
        raise ValueError("unsupported queue entry point")
    mode, target = args[1:] if entry is not None else args
    identity(target)
    if mode not in {"apply", "resolve"}:
        raise ValueError("unsupported queue operation")
    if entry == "directed":
        incoming = context_exchange(sys.stdin.buffer, sys.stdout.buffer, "work.input")
        if (
            set(incoming) != {"revision", "work"}
            or type(incoming["revision"]) is not int
            or incoming["revision"] != 1
            or not isinstance(incoming["work"], dict)
        ):
            raise ValueError("queue Context input has an invalid shape")
        output = run_effect(target, mode, incoming["work"])
        receipt = context_exchange(
            sys.stdin.buffer, sys.stdout.buffer, "work.result", result=output
        )
        digest = hashlib.sha256(encode(output)).hexdigest()
        if receipt != {"revision": 1, "sha256": digest} or type(receipt["revision"]) is not int:
            raise ValueError("queue Context result receipt does not match")
        return 0
    incoming = frame(sys.stdin.buffer, newline=entry == "serve")
    if entry == "serve":
        if set(incoming) != {"jsonrpc", "id", "method", "params"} or (
            incoming["jsonrpc"],
            incoming["id"],
            incoming["method"],
        ) != ("2.0", 1, mode):
            raise ValueError("invalid queue RPC request")
        receipt = target_result(target, mode, incoming["params"])
        sys.stdout.buffer.write(encode({"jsonrpc": "2.0", "id": 1, "result": receipt}) + b"\n")
    else:
        sys.stdout.buffer.write(encode(run_effect(target, mode, incoming)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError):
        raise SystemExit("queue transport failed") from None
