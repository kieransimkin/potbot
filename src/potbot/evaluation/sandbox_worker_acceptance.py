"""Fail-closed S46 worker-smoke evidence, not a production security certificate.

Pure validation is separate from the live runner so malformed, missing and
contradictory evidence can be tested without pretending to launch a sandbox.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any
from uuid import UUID

SCHEMA = "potbot-sandbox-worker-smoke-s46/v1"
INPUT = "syn://type/potbot-worker-probe-input-v1"
OUTPUT = "syn://type/potbot-worker-probe-output-v1"
OPERATION = "syn://thing/potbot-worker-probe-v1"
PROBE_SOURCE = "sandbox/qualification/worker_probe.py"
GUEST_EXECUTABLE = "/usr/local/libexec/potbot-worker-probe"
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_PATH_FIELDS = ("rootfs", "home", "model_staging", "shared_models", "shared_tools")
_OPTIONAL_PATHS = ("ai_config_dir", "ai_history_dir", "shared_skills")


def _uuid(value: Any) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("noncanonical identity")
    return value


def _digest(value: Any) -> str:
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise ValueError("invalid digest")
    return value


def _path(value: Any) -> Path:
    if (not isinstance(value, str) or "\0" in value or "\\" in value
            or not value.startswith("/") or len(value.encode()) > 4096
            or any(part in {"", ".", ".."} for part in value.split("/")[1:])):
        raise ValueError("expected a canonical absolute Linux path")
    return Path(value)


def unique_json(raw: bytes, *, maximum: int = 65536) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= maximum:
        raise ValueError("document size invalid")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate document field")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite number")),
        )
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("invalid document") from exc
    if not isinstance(value, dict):
        raise ValueError("document must be an object")
    return value


def policy(raw: dict[str, Any]) -> dict[str, Any]:
    """No provisioning, executable overrides, callbacks or untrusted argv."""
    fields = {"schema_version", "workspace_id", "principal_id", "agent_ids",
              "workspace_name", "ledger", "runtime"}
    if (not isinstance(raw, dict) or set(raw) != fields
            or type(raw["schema_version"]) is not int or raw["schema_version"] != 1):
        raise ValueError("unsupported qualification policy")
    _uuid(raw["workspace_id"])
    _uuid(raw["principal_id"])
    agents = raw["agent_ids"]
    if (not isinstance(agents, list) or not 1 <= len(agents) <= 4
            or len({_uuid(agent) for agent in agents}) != len(agents)):
        raise ValueError("expected one to four distinct registered agents")
    if (not isinstance(raw["workspace_name"], str)
            or not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", raw["workspace_name"])):
        raise ValueError("invalid registered workspace name")
    ledger = _path(raw["ledger"])
    runtime = raw["runtime"]
    required = {*_PATH_FIELDS, "image_digest", "qualification_sha256"}
    if (not isinstance(runtime, dict) or not required <= set(runtime)
            or set(runtime) - required - set(_OPTIONAL_PATHS)):
        raise ValueError("runtime policy fields differ")
    _digest(runtime["image_digest"])
    _digest(runtime["qualification_sha256"])
    paths = [_path(runtime[name]) for name in (*_PATH_FIELDS, *_OPTIONAL_PATHS)
             if name in runtime]
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
           for i, a in enumerate(paths) for b in paths[i + 1:]):
        raise ValueError("runtime mounts must be disjoint")
    if any(ledger == base or ledger.is_relative_to(base) or base.is_relative_to(ledger)
           for base in paths):
        raise ValueError("ledger must be outside all guest mounts")
    # Round-trip gives the caller an independent JSON-native value.
    return unique_json(json.dumps(raw, allow_nan=False).encode())


def read_private(path: Path) -> bytes:
    """Open a bounded ordinary operator file without following its final link."""
    if os.name != "posix":
        raise ValueError("live qualification requires Linux/qualified WSL2")
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("operator file path redirected")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_mode & 0o077 or before.st_nlink != 1
                or not 0 < before.st_size <= 65536):
            raise ValueError("operator file must be private, owned and bounded")
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(65537)
        after = os.fstat(fd)
        if (len(raw) != before.st_size
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError("operator file changed during read")
        return raw
    finally:
        os.close(fd)


def check_probe(value: Any, *, nonce: str, run_id: str,
                uid: int, gid: int, guest_cwd: str) -> dict[str, Any]:
    _digest(nonce)
    _uuid(run_id)
    if (not isinstance(value, dict)
            or set(value) != {"nonce", "invocation_id", "mode", "observation"}
            or value["nonce"] != nonce or value["invocation_id"] != run_id
            or value["mode"] != "inspect"):
        raise ValueError("probe response does not bind the live invocation")
    observed = value["observation"]
    if (not isinstance(observed, dict)
            or set(observed) != {"uid", "gid", "pid", "cwd", "root_readonly",
                                 "inet_denied", "inet6_denied"}
            or type(observed["uid"]) is not int or observed["uid"] != uid
            or type(observed["gid"]) is not int or observed["gid"] != gid
            or type(observed["pid"]) is not int or not 0 < observed["pid"] < 2**31
            or observed["cwd"] != guest_cwd
            or any(observed[key] is not True for key in (
                "root_readonly", "inet_denied", "inet6_denied"
            ))):
        raise ValueError("guest execution/readonly/network probe failed")
    return {key: observed[key] for key in ("uid", "gid", "root_readonly", "inet_denied", "inet6_denied")}


def check_process(result: Any) -> None:
    # Only literal booleans and a measured integer exit code count.
    if (getattr(result, "dispatched", None) is not True
            or getattr(result, "processes_stopped", None) is not True
            or getattr(result, "outcome", None) != "completed"
            or type(getattr(result, "exit_code", None)) is not int or result.exit_code != 0
            or getattr(result, "stdout_truncated", None) is not False
            or getattr(result, "stderr_truncated", None) is not False
            or not isinstance(getattr(result, "isolation_identity", None), str)
            or not result.isolation_identity):
        raise ValueError("audited process has no confirmed bounded success")


def check_audit(audit: Any, *, workspace_id: str, run_id: str) -> dict[str, Any]:
    # Input must be returned by verify_gui_run(..., identity_kind='agent',
    # subject_id=<independently selected agent>), never by the guest.
    if (not isinstance(audit, dict)
            or audit.get("schema") != "potbot-gui-audit-integrity-s16/v1"
            or audit.get("qualified") is not True
            or audit.get("workspaceId") != workspace_id or audit.get("runId") != run_id
            or audit.get("identityKind") != "agent"):
        raise ValueError("kernel audit scope invalid")
    for name in ("tracedProcesses", "fileKeys", "fileEvents", "cpuUsec",
                 "readBytes", "writeBytes", "peakMemoryBytes"):
        value = audit.get(name)
        if type(value) is not int or not 0 <= value < 2**63:
            raise ValueError("missing measured audit counter")
    if audit["tracedProcesses"] < 1 or audit["fileEvents"] < 1:
        raise ValueError("no traced worker/file operations")
    return dict(audit)


def report(*, source_commit: str, source_hashes: dict[str, str],
           image_digest: str, qualification_sha256: str, probe_sha256: str,
           workspace_id: str, agents: list[str], attempts: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(source_commit, str) or not _COMMIT.fullmatch(source_commit):
        raise ValueError("committed source identity required")
    if not source_hashes or any(not isinstance(k, str) or not k for k in source_hashes):
        raise ValueError("source hashes required")
    for digest in (*source_hashes.values(), image_digest, qualification_sha256, probe_sha256):
        _digest(digest)
    if source_hashes.get(PROBE_SOURCE) != probe_sha256:
        raise ValueError("diagnostic executable differs from committed probe")
    _uuid(workspace_id)
    if (not 1 <= len(agents) <= 4 or len(set(agents)) != len(agents)
            or len(attempts) != len(agents)):
        raise ValueError("all selected agents must complete exactly once")
    runs = set()
    for agent, attempt in zip(agents, attempts, strict=True):
        _uuid(agent)
        if (attempt.get("agent_id") != agent or attempt.get("qualified") is not True
                or attempt.get("process_stopped") is not True):
            raise ValueError("missing successful worker attempt")
        run = _uuid(attempt.get("run_id"))
        if run in runs:
            raise ValueError("reused invocation identity")
        runs.add(run)
        _digest(attempt.get("isolation_sha256"))
        observed, expected = attempt.get("observations"), attempt.get("registered_identity")
        if (not isinstance(expected, dict) or set(expected) != {"uid", "gid"}
                or type(expected["uid"]) is not int or not 420 <= expected["uid"] <= 65533
                or type(expected["gid"]) is not int or expected["gid"] != 69
                or not isinstance(observed, dict)
                or set(observed) != {"uid", "gid", "root_readonly", "inet_denied", "inet6_denied"}
                or type(observed["uid"]) is not int or observed["uid"] != expected["uid"]
                or type(observed["gid"]) is not int or observed["gid"] != expected["gid"]
                or any(observed[key] is not True for key in (
                    "root_readonly", "inet_denied", "inet6_denied"
                ))):
            raise ValueError("probe observations are incomplete or contradict registration")
        check_audit(attempt.get("audit"), workspace_id=workspace_id, run_id=run)
    return {
        "schema": SCHEMA, "qualified_smoke": True,
        "qualification": "registered_worker_transport_and_kernel_audit_only",
        "source_commit": source_commit, "source_hashes": dict(source_hashes),
        "image_digest": image_digest, "image_qualification_sha256": qualification_sha256,
        "probe_sha256": probe_sha256, "workspace_id": workspace_id,
        "attempts": attempts,
        "not_qualified": ["nested_cog_workflows", "postgres_execution_receipts",
                          "xpra_browser_streams", "cross_agent_filesystem_isolation",
                          "hostile_code_completeness", "production_readiness"],
    }


def write_report(path: Path, value: dict[str, Any]) -> None:
    """Exclusive private output: an old successful report is never overwritten."""
    parent = path.parent
    if (not path.is_absolute() or parent.resolve(strict=True) != parent
            or not parent.is_dir()):
        raise ValueError("report requires an existing real operator directory")
    info = parent.stat()
    if os.name != "posix" or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("report directory must be operator-private")
    raw = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    if len(raw) > 65536:
        raise ValueError("report exceeds its bound")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    owned = os.fstat(fd)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        # Remove only this newly created output, never a replaced pathname.
        try:
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                path.unlink()
        except OSError:
            pass
        raise


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
