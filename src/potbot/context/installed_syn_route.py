"""Lifecycle of the installed, same-account Syn Context endpoint."""

from __future__ import annotations

import errno
import hashlib
import os
import socket
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.context.syn_context_route import METHOD, SynContextRoute, SynRoundTrip
from potbot.domain.errors import CapabilityUnavailableError, DomainError, ScopeDeniedError
from potbot.syn_release.contracts import Policy, canonical, digest, document, fields
from potbot.syn_release.operator import SynOperatorDeployment
from potbot.syn_release.store import SynReleaseStore, _sync_directory, _write_new, private_read

PROFILE = "syn.operator-local-rpc/1"
ROUTE_TABLE_PROFILE = "syn.local-rpc-routes/1"
BROWSER_METHOD = "browser.command"
_BROWSER_SOCKET = "browser-rpc.sock"
_ROUTE_CONFIG = "context-rpc.json"
_ROUTE_SOCKET = "context-rpc.sock"

# Execute the public client from the verified installed Syn environment. The
# data uses pipes so payloads and secrets never appear in arguments or logs.
_CLIENT = """
import sys
from pathlib import Path
from syn_client.local_rpc import call

path, uid, key, version, config, route, workspace, method, request = sys.argv[1:]
data = sys.stdin.buffer.read(2097153)
result = call(data, socket_path=Path(path), owner_uid=int(uid), expected_hash=key,
              expected_version=version, configuration_sha256=config,
              route_sha256=route, workspace_id=workspace, method=method, request_id=request)
sys.stdout.buffer.write(bytes.fromhex(result.receipt["body"]["boot_id"]) + result.payload)
"""


def _route_config(deployment: SynOperatorDeployment) -> Path:
    name = (
        "application-rpc.json"
        if ROUTE_TABLE_PROFILE in deployment.policy.value["required_profiles"]
        else _ROUTE_CONFIG
    )
    return deployment.root / name


def _route_bytes(deployment: SynOperatorDeployment, workspace_id: UUID) -> bytes:
    if sys.platform != "linux" or os.getuid() == 0:
        raise ScopeDeniedError("Context RPC requires the native Linux service account")
    if PROFILE not in deployment.policy.value["required_profiles"]:
        raise CapabilityUnavailableError("The Syn policy must pin directed local RPC")

    def destination(method: str, name: str) -> dict[str, str | int]:
        endpoint = deployment.root / name
        if len(os.fsencode(endpoint)) > 103:
            raise ScopeDeniedError("Directed RPC service socket path is too long")
        return {
            "method": method,
            "service_socket": str(endpoint),
            "service_uid": os.getuid(),
            "workspace_id": str(workspace_id),
        }

    context = destination(METHOD, _ROUTE_SOCKET)
    if ROUTE_TABLE_PROFILE in deployment.policy.value["required_profiles"]:
        return canonical(
            {
                "profile": ROUTE_TABLE_PROFILE,
                "routes": [
                    context,
                    destination(BROWSER_METHOD, _BROWSER_SOCKET),
                ],
            }
        )
    return canonical({"profile": "syn.local-rpc-route/1", **context})


def provision_context_route(deployment: SynOperatorDeployment, workspace_id: UUID) -> Path:
    """Prepare the private Syn route without changing the operator identity."""
    raw = _route_bytes(deployment, workspace_id)
    deployment._installation()
    deployment._registered()
    path = _route_config(deployment)
    try:
        _write_new(path, raw)
        _sync_directory(deployment.root)
    except FileExistsError:
        if private_read(path) != raw:
            raise ScopeDeniedError("Existing Context RPC route selects another workspace") from None
    return path


def operator_route_path(deployment: SynOperatorDeployment) -> Path:
    """Resolve the path prepared for the explicitly enabled operator service."""
    path = _route_config(deployment)
    raw = private_read(path)
    try:
        config = document(raw)
        row = config["routes"][0] if config.get("profile") == ROUTE_TABLE_PROFILE else config
        workspace = UUID(row["workspace_id"])
    except (KeyError, IndexError, ValueError, TypeError, AttributeError):
        raise ScopeDeniedError("Invalid installed Context route workspace") from None
    if raw != _route_bytes(deployment, workspace):
        raise ScopeDeniedError("Context route does not match the installed operator")
    return path


def _deployment(
    selection: dict[str, Any], *, protected_mounts: tuple[Path, ...]
) -> SynOperatorDeployment:
    selected = fields(selection, {"store", "operator_root", "policy", "policy_sha256"})
    paths = tuple(Path(selected[key]) for key in ("store", "operator_root", "policy"))
    for path in paths:
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise ScopeDeniedError("Context RPC requires canonical installed paths")
        if any(
            path.is_relative_to(mount) or mount.is_relative_to(path) for mount in protected_mounts
        ):
            raise ScopeDeniedError("Context RPC installation overlaps a worker mount")
    store, root, policy_path = paths
    policy_bytes = private_read(policy_path)
    if hashlib.sha256(policy_bytes).hexdigest() != digest(selected["policy_sha256"]):
        raise ScopeDeniedError("Context RPC trust policy changed")
    return SynOperatorDeployment(
        SynReleaseStore(store), Policy.parse(policy_bytes), root, policy_path=policy_path
    )


@contextmanager
def _listener(endpoint: Path) -> Iterator[socket.socket]:
    """Own only the private service socket created for this worker lifetime."""
    import fcntl

    lock_path = endpoint.with_suffix(".lock")
    lock = os.open(lock_path, os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_RDWR, 0o600)
    try:
        info = os.fstat(lock)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise ScopeDeniedError("Context RPC socket lock is unsafe")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            entry = endpoint.lstat()
        except FileNotFoundError:
            pass
        else:
            if (
                not stat.S_ISSOCK(entry.st_mode)
                or entry.st_uid != os.getuid()
                or entry.st_mode & 0o077
            ):
                raise ScopeDeniedError("Existing Context RPC endpoint is unsafe")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                if probe.connect_ex(str(endpoint)) != errno.ECONNREFUSED:
                    raise ScopeDeniedError("Context RPC socket already belongs to a listener")
            if not os.path.samestat(entry, endpoint.lstat()):
                raise ScopeDeniedError("Context RPC socket changed during recovery")
            endpoint.unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(endpoint))
            owned = endpoint.lstat()
            try:
                endpoint.chmod(0o600)
                listener.listen(8)
                listener.settimeout(0.2)
                yield listener
            finally:
                try:
                    if os.path.samestat(owned, endpoint.lstat()):
                        endpoint.unlink()
                except FileNotFoundError:
                    pass
    finally:
        os.close(lock)


def installed_directed_rpc(
    selection: dict[str, Any],
    workspace_id: UUID,
    *,
    protected_mounts: tuple[Path, ...],
    method: str,
    max_reply: int,
) -> tuple[Path, str, SynRoundTrip]:
    deployment = _deployment(selection, protected_mounts=protected_mounts)
    if method not in {METHOD, BROWSER_METHOD}:
        raise ScopeDeniedError("Unknown installed Syn RPC method")
    if (
        method == BROWSER_METHOD
        and ROUTE_TABLE_PROFILE not in deployment.policy.value["required_profiles"]
    ):
        raise CapabilityUnavailableError("Browser RPC requires a pinned Syn route table")
    expected = _route_bytes(deployment, workspace_id)
    if private_read(_route_config(deployment)) != expected:
        raise ScopeDeniedError("Configured Syn RPC route differs from this workspace")
    route_sha = hashlib.sha256(expected).hexdigest()
    receipt, python = deployment._installation()
    registered = deployment._registered()
    if registered["installation"] != receipt["directory"]:
        raise ScopeDeniedError("Context route installation differs from operator registration")
    deployment._observed_runtime = receipt["observed"]["runtime"]
    status = deployment._status(python, registered)["status"]
    profiles = status["runtime"]["api_profiles"]
    if PROFILE not in profiles or (
        method == BROWSER_METHOD and ROUTE_TABLE_PROFILE not in profiles
    ):
        raise CapabilityUnavailableError("Installed Syn does not advertise directed local RPC")
    lifecycle = document(private_read(deployment.root / "lifecycle.json"), canonical_only=True)
    if (
        lifecycle.get("state") != "ready"
        or lifecycle.get("pid") != status["pid"]
        or lifecycle.get("manifest_sha256") != deployment.policy.manifest_sha256
        or lifecycle.get("context_route_sha256") != route_sha
        or lifecycle.get("attestation", {}).get("status", {}).get("boot_id") != status["boot_id"]
    ):
        raise CapabilityUnavailableError("Operator supervisor has no matching active Context route")
    endpoint = deployment.root / (_BROWSER_SOCKET if method == BROWSER_METHOD else _ROUTE_SOCKET)
    uid = os.getuid()
    args = [
        str(python),
        "-I",
        "-B",
        "-c",
        _CLIENT,
        str(deployment.state_dir / "operator.sock"),
        str(uid),
        registered["base_public_key_hash"],
        deployment.policy.value["version"],
        registered["configuration_sha256"],
        route_sha,
        str(workspace_id),
        method,
    ]

    def round_trip(request_id: UUID, data: bytes) -> tuple[bytes, str]:
        result = subprocess.run(
            [*args, str(request_id)],
            cwd=deployment.root,
            env=deployment._environment(),
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
        )
        if result.returncode or not 17 <= len(result.stdout) <= max_reply + 16:
            raise CapabilityUnavailableError(
                "Verified Syn client did not return a bounded RPC reply"
            )
        return result.stdout[16:], result.stdout[:16].hex()

    return endpoint, status["boot_id"], round_trip


@contextmanager
def serve_directed_rpc(endpoint: Path, serve: Callable[[socket.socket], None]) -> Iterator[None]:
    stopped = threading.Event()
    with _listener(endpoint) as listener:

        def accept() -> None:
            try:
                while not stopped.is_set():
                    try:
                        conn, _ = listener.accept()
                    except TimeoutError:
                        continue
                    with conn:
                        try:
                            serve(conn)
                        except (DomainError, OSError, ValueError):
                            continue
            finally:
                stopped.set()

        worker = threading.Thread(target=accept, name=f"potbot-{endpoint.stem}")
        worker.start()
        try:
            yield
        finally:
            stopped.set()
            worker.join()


@contextmanager
def installed_context_route(
    selection: dict[str, Any], workspace_id: UUID, *, protected_mounts: tuple[Path, ...]
) -> Iterator[SynContextRoute]:
    from potbot.context.rpc_contract import MAX_FRAME_BYTES

    endpoint, boot, round_trip = installed_directed_rpc(
        selection,
        workspace_id,
        protected_mounts=protected_mounts,
        method=METHOD,
        max_reply=MAX_FRAME_BYTES,
    )
    uid = os.getuid()
    route = SynContextRoute(
        workspace_id=workspace_id, operator_boot=boot, caller_uid=uid, round_trip=round_trip
    )
    with serve_directed_rpc(endpoint, lambda conn: route.serve_connection(conn, operator_uid=uid)):
        yield route
