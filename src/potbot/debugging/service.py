"""Serve a single operator-admitted Syn debugger session from the installed service."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import socket
import stat
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from potbot.api.jobs_cli import add_scope_arguments, database_context, protected_values
from potbot.cogs.contracts import CogRunContext, CogSignature, TypedCogValue
from potbot.cogs.debugger import OPERATIONS, cog_ref
from potbot.cogs.registry import BoundCogRegistry
from potbot.controller.execution_events import ExecutionEventService
from potbot.debugging.bridge import ServiceDebuggerEndpoint, _boot
from potbot.debugging.cog_runtime import DebuggerCogRuntime
from potbot.debugging.contracts import DebugAttachTarget, DebugIdentity
from potbot.debugging.dap import DapAdapter
from potbot.debugging.session import DebugSession
from potbot.debugging.syn_forwarding import DebuggerInvocationClaims, SynDebuggerRoute
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.persistence.database import Database
from potbot.semantics.service import SynService
from potbot.service_credentials import ServiceCredentials
from potbot.syn_release.contracts import DOCUMENT_LIMIT, digest, document, fields
from potbot.syn_release.mesh_endpoint import _peer, root_path
from potbot.syn_release.store import _directory
from potbot.tools.execution_contracts import identifier
from potbot.tools.repository import CandidateWorkspaceManager, Lease
from potbot.tools.secrets import CaptureRedactor
from potbot.workspace_sandbox import load_workspace_sandbox


def _pid(value: object) -> int:
    if type(value) is not int or value <= 1:
        raise ScopeDeniedError("Debugger requires a pinned local process")
    return value


def _process_current(pid: int, start: int, uid: int) -> bool:
    try:
        proc = Path("/proc") / str(pid)
        state, *rest = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        return state not in {"Z", "X"} and int(rest[18]) == start and proc.stat().st_uid == uid
    except (OSError, ValueError, IndexError):
        return False


def read_registration(path: Path, workspace: UUID) -> dict[str, Any]:
    info = root_path(path)
    if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode):
        raise ScopeDeniedError("Debugger registration must be administrator-owned")
    with path.open("rb") as file:
        selected = fields(
            document(file.read(DOCUMENT_LIMIT + 1)),
            {
                "profile",
                "workspace_id",
                "bridge",
                "operator",
                "peer",
                "manifest_sha256",
                "session_id",
                "episode_id",
                "task_revision_id",
                "repository_state_id",
                "snapshot_id",
                "candidate_id",
                "lease_owner",
                "lease_generation",
                "operations",
                "target",
            },
        )
    if selected["profile"] != "potbot.debugger-service/1" or selected["workspace_id"] != str(
        workspace
    ):
        raise ScopeDeniedError("Debugger registration differs from the service workspace")
    for name in (
        "episode_id",
        "task_revision_id",
        "repository_state_id",
        "snapshot_id",
        "candidate_id",
    ):
        if not identifier(selected[name]).int:
            raise ScopeDeniedError("Debugger needs nonzero database references")
    for name in ("operator", "peer", "manifest_sha256"):
        digest(selected[name])
    _boot(selected["session_id"])
    operations = selected["operations"]
    if (
        not isinstance(operations, list)
        or not operations
        or any(
            not isinstance(op, str)
            or op not in OPERATIONS
            or op in {"launch", "terminate", "evaluate"}
            for op in operations
        )
        or len(operations) != len(set(operations))
    ):
        raise ScopeDeniedError("Debugger registration has an invalid operation grant")
    bridge = Path(selected["bridge"])
    root_path(bridge.parent)
    target = fields(
        selected["target"],
        {
            "socket",
            "owner_uid",
            "owner_pid",
            "owner_start",
            "pid",
            "start",
            "uid",
            "configuration_sha256",
            "isolation_sha256",
            "adapter_version",
            "runtime",
            "source",
        },
    )
    root_path(Path(target["socket"]).parent)
    _pid(target["owner_pid"])
    _pid(target["pid"])
    if type(target["owner_uid"]) is not int or target["owner_uid"] < 0:
        raise ScopeDeniedError("Invalid debugger socket owner identity")
    for name in ("owner_start", "uid", "start"):
        if type(target[name]) is not int or target[name] <= 0:
            raise ScopeDeniedError("Invalid debugger process identity")
    if target["uid"] == getattr(os, "getuid", lambda: -1)():
        raise ScopeDeniedError("Debugger target must be isolated from the service user")
    if target["owner_uid"] not in {0, getattr(os, "getuid", lambda: -1)()}:
        raise ScopeDeniedError("Debugger adapter must belong to a trusted local controller")
    for name in ("configuration_sha256", "isolation_sha256"):
        digest(target[name])
    if not isinstance(target["source"], dict) or not 1 <= len(target["source"]) <= 256:
        raise ScopeDeniedError("Debugger registration must pin a bounded source inventory")
    for value in target["source"].values():
        digest(value)
    return selected


async def _adapter(target: dict[str, Any]) -> DapAdapter:
    path = Path(target["socket"])
    entry = root_path(path)
    if entry.st_uid != target["owner_uid"] or not stat.S_ISSOCK(entry.st_mode):
        raise ScopeDeniedError("Debugger target socket differs from its approved owner")
    channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        channel.setblocking(False)
        async with asyncio.timeout(5):
            await asyncio.get_running_loop().sock_connect(channel, str(path))
        pid, uid, _ = _peer(channel)
        if (
            (pid, uid) != (target["owner_pid"], target["owner_uid"])
            or not _process_current(pid, target["owner_start"], uid)
            or not os.path.samestat(entry, path.lstat())
        ):
            raise ScopeDeniedError("Debugger target socket changed before attachment")
        reader, writer = await asyncio.open_connection(sock=channel)
    except BaseException:
        channel.close()
        raise
    return DapAdapter(reader, writer, timeout_s=15)


async def serve(
    registration: Path, state: Path, database: Database, actor: WorkspaceContext
) -> None:
    selected = read_registration(registration, actor.workspace_id)
    target = selected["target"]
    sandbox = load_workspace_sandbox(database, actor)
    for protected in (registration, state, Path(selected["bridge"]), Path(target["socket"])):
        if protected.is_relative_to(sandbox.root) or sandbox.root.is_relative_to(protected):
            raise ScopeDeniedError("Debugger control paths must be outside the workspace sandbox")
    _directory(state, create=True)
    sessions = state / "sessions"
    _directory(sessions, create=True)
    session_claims = DebuggerInvocationClaims(sessions, actor.workspace_id)
    invocation_claims = DebuggerInvocationClaims(state, actor.workspace_id)
    artifacts = ArtifactStore(database, LocalArtifactBackend(sandbox.path("artifacts") / "objects"))
    candidates = CandidateWorkspaceManager(database, artifacts, sandbox.path("candidates"))
    lease = Lease(
        identifier(selected["candidate_id"]),
        selected["lease_owner"],
        selected["lease_generation"],
        datetime.now(UTC),
    )
    episode, revision, repository_state, snapshot = (
        identifier(selected[name])
        for name in ("episode_id", "task_revision_id", "repository_state_id", "snapshot_id")
    )
    candidate = candidates.check_lease(actor, lease)
    if candidate["state_id"] != repository_state:
        raise ScopeDeniedError("Debugger candidate differs from its approved repository state")
    baseline = candidates.states.get(actor, repository_state)
    expected_sources = {entry["path"]: entry["sha256"] for entry in baseline["manifest"]["files"]}
    if any(expected_sources.get(name) != sha for name, sha in target["source"].items()):
        raise ScopeDeniedError(
            "Debugger source inventory differs from the admitted repository state"
        )

    def process_is_current() -> bool:
        return _process_current(target["pid"], target["start"], target["uid"]) and _process_current(
            target["owner_pid"], target["owner_start"], target["owner_uid"]
        )

    def check_admission() -> None:
        if (
            read_registration(registration, actor.workspace_id) != selected
            or not process_is_current()
        ):
            raise ScopeDeniedError("Debugger session registration or target changed")
        current = candidates.check_lease(actor, lease)
        if current["state_id"] != repository_state:
            raise ScopeDeniedError("Debugger candidate state changed")
        with database.transaction(actor) as connection:
            row = connection.execute(
                "SELECT active_contract_id, repository_id, status FROM episodes "
                "WHERE workspace_id=%s AND id=%s",
                (actor.workspace_id, episode),
            ).fetchone()
        if (
            row is None
            or row["active_contract_id"] != revision
            or row["repository_id"] != current["repository_id"]
            or row["status"] not in {"running", "paused"}
        ):
            raise ScopeDeniedError("Debugger task revision is no longer active")

    check_admission()
    with database.transaction(actor) as connection:
        if (
            SynService._snapshot(connection, actor, snapshot)["repository_id"]
            != candidate["repository_id"]
        ):
            raise ScopeDeniedError("Debugger catalog belongs to another repository")
    identity = DebugIdentity(
        actor.workspace_id,
        uuid4(),
        Path(candidate["path"]),
        tuple(sorted(target["source"].items())),
        "debugpy",
        target["adapter_version"],
        target["runtime"],
        "synny://type/workflow-execution",
    )
    target_pin = DebugAttachTarget(
        target["pid"], target["configuration_sha256"], process_is_current
    )
    endpoint = ServiceDebuggerEndpoint(Path(selected["bridge"]))
    try:
        scope = endpoint.scope
        if (
            scope["session_id"] != selected["session_id"]
            or scope["operator"] != selected["operator"]
            or scope["workspace_id"] != str(actor.workspace_id)
            or endpoint.peer != selected["peer"]
            or endpoint.manifest_sha256 != selected["manifest_sha256"]
        ):
            raise ScopeDeniedError("Debugger bridge differs from the approved Syn session")
        route = SynDebuggerRoute(
            endpoint,
            operator=selected["operator"],
            peer=endpoint.peer,
            peer_boot=endpoint.peer_boot,
        )
        if not session_claims.claim(UUID(hex=selected["session_id"])):
            raise ScopeDeniedError("A new debugger session is required after service restart")
        redactor = CaptureRedactor(protected_values(database, ServiceCredentials()))
        events = ExecutionEventService(database)

        allowed = {cog_ref(operation) for operation in selected["operations"]}

        def admit_cog(
            signature: CogSignature, source: TypedCogValue, context: CogRunContext
        ) -> None:
            if str(signature.cog_ref) not in allowed:
                raise ScopeDeniedError("This debugger session only controls an owned target")
            check_admission()

        def admit_adapter(operation: str, _: Mapping[str, Any]) -> None:
            check_admission()

        adapter = await _adapter(target)
        try:
            session = DebugSession(
                adapter,
                identity,
                admit=admit_adapter,
                record=lambda event: events.append(actor, event),
                redactor=redactor,
                timeout_s=15,
                attach_target=target_pin,
            )
            session.isolation_identity = target["isolation_sha256"]
            controller = DebuggerCogRuntime.from_workspace(
                session,
                database=database,
                artifacts=artifacts,
                actor=actor,
                snapshot_id=snapshot,
                task_revision_id=revision,
                repository_state_id=repository_state,
                admit=admit_cog,
            )
            print("Debugger forwarding ready for its approved Syn session", flush=True)
            await route.serve(BoundCogRegistry.debugger(), controller, claims=invocation_claims)
        finally:
            # Closing the channel does not send another debugger command. The target
            # owner reconciles the retained lease and decides whether to resume it.
            await adapter.close()
    finally:
        endpoint.close()


async def _run(args: argparse.Namespace) -> None:
    from potbot.service import service_home

    database, actor = database_context(args)
    task = asyncio.create_task(
        serve(args.registration, service_home() / "debugger", database, actor)
    )
    for signum in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(signum, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_scope_arguments(parser)
    parser.add_argument("--registration", required=True, type=Path)
    args = parser.parse_args()
    try:
        asyncio.run(_run(args))
        return 0
    except Exception as error:
        print(f"Debugger forwarding stopped: {type(error).__name__}", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
