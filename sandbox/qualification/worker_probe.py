#!/usr/bin/python3
"""Pinned, stdlib-only diagnostic worker; never a general code/command runner.

Install in a newly qualified immutable image. It cannot choose an executable,
read arbitrary files or make outbound connections. --wire-only is for protocol
unit tests and deliberately cannot produce a qualified inspection response.
"""

from __future__ import annotations

import errno
import json
import os
import re
import socket
import sys
from pathlib import Path
from uuid import UUID

INPUT = "syn://type/potbot-worker-probe-input-v1"
OUTPUT = "syn://type/potbot-worker-probe-output-v1"
OPERATION = "syn://thing/potbot-worker-probe-v1"
LIMIT = 16 * 1024
NONCE = re.compile(r"[0-9a-f]{64}\Z")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def decode(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= LIMIT:
        raise ValueError("wire size")
    request = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite number")),
    )
    if (
        not isinstance(request, dict)
        or set(request) != {"schema_version", "operation_ref", "input", "invocation_id"}
        or type(request["schema_version"]) is not int
        or request["schema_version"] != 1
        or request["operation_ref"] != OPERATION
    ):
        raise ValueError("wire envelope")
    invocation = request["invocation_id"]
    if not isinstance(invocation, str) or str(UUID(invocation)) != invocation:
        raise ValueError("invocation identity")
    source = request["input"]
    if (
        not isinstance(source, dict)
        or set(source) != {"type_ref", "value"}
        or source["type_ref"] != INPUT
    ):
        raise ValueError("semantic input")
    value = source["value"]
    if (
        not isinstance(value, dict)
        or set(value) != {"nonce"}
        or not isinstance(value["nonce"], str)
        or not NONCE.fullmatch(value["nonce"])
    ):
        raise ValueError("nonce")
    return request


def root_is_readonly(mountinfo: str) -> bool:
    # mountinfo fields: id parent major:minor root mountpoint mount-options ...
    roots = [
        line.split()
        for line in mountinfo.splitlines()
        if len(line.split()) >= 7 and line.split()[4] == "/"
    ]
    return len(roots) == 1 and "ro" in roots[0][5].split(",")


def socket_denied(family: int) -> bool:
    # No connect/send and no remote address. EAFNOSUPPORT is not evidence of
    # seccomp denial: a missing address family must not qualify this check.
    try:
        with socket.socket(family, socket.SOCK_STREAM):
            return False
    except OSError as exc:
        return exc.errno in {errno.EPERM, errno.EACCES}


def inspect() -> dict:
    return {
        "uid": os.getuid(),
        "gid": os.getgid(),
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "root_readonly": root_is_readonly(Path("/proc/self/mountinfo").read_text()),
        "inet_denied": socket_denied(socket.AF_INET),
        "inet6_denied": socket_denied(socket.AF_INET6),
    }


def response(request: dict, observation: dict | None) -> dict:
    return {
        "schema_version": 1,
        "type_ref": OUTPUT,
        "value": {
            "nonce": request["input"]["value"]["nonce"],
            "invocation_id": request["invocation_id"],
            "mode": "inspect" if observation is not None else "wire_only",
            "observation": observation,
        },
    }


def main(argv=None) -> int:
    selected = sys.argv[1:] if argv is None else argv
    if selected not in ([], ["--wire-only"]):
        return 2
    try:
        request = decode(sys.stdin.buffer.read(LIMIT + 1))
        result = response(request, None if selected else inspect())
        raw = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(raw.encode()) > LIMIT:
            return 2
        print(raw)
        return 0
    except (OSError, ValueError, TypeError, RecursionError):
        # Never reflect input or host/sandbox paths into an error response.
        print("worker qualification probe rejected", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
