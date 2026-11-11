"""Dispatch an explicitly installed read-only Cog from the ordinary leased job worker."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.context.installed_syn_route import installed_context_route
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    IndeterminateExecutionError,
    OperationCancelledError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.jobs import CogJobInput, JobLease
from potbot.domain.serialization import canonical_json
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.syn_release.contracts import DOCUMENT_LIMIT, document, fields
from potbot.syn_release.mesh_endpoint import NativeMeshClient, root_path
from potbot.tools.agent_homes import AgentHomeRegistry
from potbot.tools.execution_contracts import ExecutionPolicy, WorkBinding, digest, identifier
from potbot.tools.operator_cli import parse_manifest
from potbot.tools.released_syn_nodes import ReleasedSynWorkerNodeMesh
from potbot.tools.sandbox_cog_worker import SandboxedCogExecutor, SandboxedCogOperation
from potbot.tools.shared_workspace_isolation import SharedWorkspaceBubblewrapBackend
from potbot.tools.supervisor import ProcessSupervisor
from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger
from potbot.workspace_sandbox import load_workspace_sandbox

if TYPE_CHECKING:
    from potbot.controller.jobs import JobService
    from potbot.persistence.database import Database


def read_profile(path: Path) -> tuple[dict[str, Any], str]:
    info = root_path(path)
    if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode):
        raise ScopeDeniedError("Native Cog job profile must be administrator-owned.")
    with path.open("rb") as reader:
        data = reader.read(DOCUMENT_LIMIT + 1)
    raw = document(data)
    version = raw.get("profile")
    if version not in ("potbot.hosted-native-cog/1", "potbot.hosted-native-cog/2"):
        raise ScopeDeniedError("Unknown native Cog job profile.")
    profile = fields(
        raw,
        {
            "profile",
            "workspace_id",
            "workspace_name",
            "socket",
            "manifest_sha256",
            "release_key_sha256",
            "group_id",
            "group_epoch",
            "agents",
            "operations",
            "image_digest",
            "qualification_sha256",
        }
        | ({"context_rpc"} if version == "potbot.hosted-native-cog/2" else set()),
    )
    if version == "potbot.hosted-native-cog/2":
        fields(profile["context_rpc"], {"store", "operator_root", "policy", "policy_sha256"})
    return profile, hashlib.sha256(data).hexdigest()


class NativeCogDispatcher:
    def __init__(
        self,
        executor: SandboxedCogExecutor,
        operations: tuple[SandboxedCogOperation, ...],
        agents: frozenset[UUID],
        profile_hash: str,
    ) -> None:
        self.executor = executor
        self.operations = {op.binding.operation_ref: op for op in operations}
        if not self.operations or len(self.operations) != len(operations):
            raise ScopeDeniedError("Native Cog installation requires distinct operations.")
        self.agents, self.profile_hash = agents, profile_hash

    def run(
        self, jobs: JobService, actor: WorkspaceContext, lease: JobLease, abort: threading.Event
    ) -> dict[str, str]:
        request = CogJobInput.parse(lease.input)
        operation = self.operations.get(request.operation_ref)
        if (
            request.profile_sha256 != self.profile_hash
            or request.agent_id not in self.agents
            or operation is None
            or request.type_ref != operation.binding.input_type_ref
        ):
            raise ScopeDeniedError("Job differs from the installed native Cog profile.")
        with jobs.database.transaction(actor) as connection:
            state = connection.execute(
                "SELECT content_digest FROM repository_states WHERE workspace_id=%s AND id=%s",
                (actor.workspace_id, request.repository_state_id),
            ).fetchone()
        if state is None or state["content_digest"] != operation.binding.policy.source_digest:
            raise ScopeDeniedError("Job repository state differs from the installed Cog pin.")
        value = TypedCogValue(request.type_ref, request.value)
        invocation = CogRunContext(
            self.profile_hash, lease.contract_revision_id, request.repository_state_id, lease.job_id
        )
        if abort.is_set() or jobs.cancelled(actor, lease):
            raise OperationCancelledError("Native Cog was cancelled before admission.")
        grant = self.executor.authorize_syn(operation, value, invocation, actor, request.agent_id)
        if grant is None:
            raise ScopeDeniedError("Native Cog dispatch requires a released Syn grant.")
        if abort.is_set() or jobs.cancelled(actor, lease):
            raise OperationCancelledError("Native Cog was cancelled before dispatch.")
        jobs.mark_dispatched(actor, lease, cog_max_wall_ms=operation.binding.policy.max_wall_ms)

        async def execute() -> TypedCogValue:
            pending = asyncio.create_task(
                self.executor.execute(
                    operation, value, invocation, actor, request.agent_id, syn_authorization=grant
                )
            )
            try:
                while not pending.done() and not abort.is_set():
                    await asyncio.wait((pending,), timeout=0.1)
                if abort.is_set() and not pending.done():
                    pending.cancel()
                return await pending
            except asyncio.CancelledError as error:
                raise IndeterminateExecutionError(
                    "Native Cog was cancelled after dispatch."
                ) from error
            finally:
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)

        result = asyncio.run(execute())
        if abort.is_set() or jobs.cancelled(actor, lease):
            raise IndeterminateExecutionError("Native Cog lease changed before result publication.")
        sandbox = load_workspace_sandbox(jobs.database, actor)
        artifacts = ArtifactStore(jobs.database, LocalArtifactBackend(sandbox.path("artifacts")))
        artifact = artifacts.put(
            actor,
            canonical_json({"type_ref": result.type_ref, "value": result.value}).encode(),
            "application/json",
        )
        return {"artifact_id": str(artifact["id"]), "operation_id": str(lease.job_id)}


@contextmanager
def configured_native_cogs(
    db: Database, actor: WorkspaceContext
) -> Iterator[NativeCogDispatcher | None]:
    selected = os.environ.get("POTBOT_NATIVE_COG_CONFIG")
    if selected is None:
        yield None
        return
    if os.environ.get("POTBOT_OPERATOR_REQUIRED") != "1" or not os.environ.get(
        "POTBOT_OPERATOR_SCOPED_LAUNCHER"
    ):
        raise ScopeDeniedError("Native Cog jobs require the installed scoped worker service.")
    profile, fingerprint = read_profile(Path(selected))
    paths = parse_manifest(Path(os.environ["POTBOT_OPERATOR_MANIFEST"]))
    if (
        identifier(profile["workspace_id"]) != actor.workspace_id
        or paths.workspace_id != actor.workspace_id
        or paths.image_digest != profile["image_digest"]
        or paths.qualification_sha256 != profile["qualification_sha256"]
    ):
        raise ScopeDeniedError("Native Cog profile differs from the installed workspace or image.")
    endpoint = Path(profile["socket"])
    if any(
        private_path.is_relative_to(mount)
        for private_path in (endpoint, Path(selected))
        for mount in (
            paths.rootfs,
            paths.home,
            paths.model_staging,
            paths.shared_models,
            paths.shared_tools,
            paths.ai_config_dir,
            paths.ai_history_dir,
        )
    ):
        raise ScopeDeniedError("Native Syn dispatcher cannot be mounted inside a worker.")
    if not isinstance(profile["agents"], list) or not 1 <= len(profile["agents"]) <= 8:
        raise ScopeDeniedError("Native Cog profile requires a bounded Agent cohort.")
    agents = frozenset(identifier(agent) for agent in profile["agents"])
    if (
        len(agents) != len(profile["agents"])
        or type(profile["group_epoch"]) is not int
        or profile["group_epoch"] <= 0
        or not isinstance(profile["operations"], list)
        or not 1 <= len(profile["operations"]) <= 32
    ):
        raise ScopeDeniedError("Native Cog profile has invalid Agent or operation bounds.")
    operations = []
    for raw in profile["operations"]:
        op = fields(
            raw,
            {
                "operation_ref",
                "entrypoint",
                "input_type_ref",
                "output_type_ref",
                "repository_digest",
                "max_wall_ms",
            },
        )
        image_pin = dict(paths.tool_entrypoints).get(op["entrypoint"])
        if (
            image_pin is None
            or type(op["max_wall_ms"]) is not int
            or not 1 <= op["max_wall_ms"] <= 300_000
        ):
            raise ScopeDeniedError("Native Cog operation is outside the installed executable pins.")
        for port in ("input_type_ref", "output_type_ref"):
            TypedCogValue(op[port], None)
        operations.append(
            SandboxedCogOperation(
                WorkBinding(
                    "tool",
                    op["operation_ref"],
                    op["input_type_ref"],
                    op["output_type_ref"],
                    image_pin,
                    "read_only",
                    ExecutionPolicy(
                        fingerprint,
                        paths.qualification_sha256,
                        digest(op["repository_digest"]),
                        profile["profile"].removeprefix("potbot."),
                        max_wall_ms=op["max_wall_ms"],
                    ),
                ),
                (op["entrypoint"],),
                image_pin,
                protocol="context-rpc/v1",
            )
        )
    runtime = paths.runtime()
    ledger = WorkspaceLeaseLedger(paths.controller_ledger, workspace_home=paths.home)
    homes = AgentHomeRegistry(ledger, workspace_name=profile["workspace_name"])
    if any(homes.lookup(actor.workspace_id, agent) is None for agent in agents):
        raise ScopeDeniedError("Native Cog cohort has an unregistered Agent workspace home.")
    backend = SharedWorkspaceBubblewrapBackend(runtime)
    if backend.operator_attachment is None:
        raise StateMismatchError("Native Cog jobs require a live installed operator.")
    backend.operator_attachment.require_healthy()
    transport = NativeMeshClient(
        endpoint,
        actor.workspace_id,
        manifest_sha256=digest(profile["manifest_sha256"]),
        release_key_sha256=digest(profile["release_key_sha256"]),
        group_id=identifier(profile["group_id"]),
        group_epoch=profile["group_epoch"],
        agents=agents,
    )
    try:
        workers = ReleasedSynWorkerNodeMesh(transport)
        route_context = (
            installed_context_route(
                profile["context_rpc"],
                actor.workspace_id,
                protected_mounts=tuple(
                    path.resolve(strict=True)
                    for path in (
                        runtime.rootfs,
                        runtime.home,
                        runtime.model_staging,
                        runtime.shared_models,
                        runtime.shared_tools,
                        runtime.shared_skills,
                        runtime.ai_config_dir,
                        runtime.ai_history_dir,
                    )
                    if path is not None
                ),
            )
            if profile["profile"] == "potbot.hosted-native-cog/2"
            else nullcontext(None)
        )
        with route_context as route, workers.running():
            executor = SandboxedCogExecutor(
                runtime, ledger, homes, ProcessSupervisor(backend), workers, syn_context_route=route
            )
            executor.bind_context(db, actor, tuple(operations))
            yield NativeCogDispatcher(executor, tuple(operations), agents, fingerprint)
    finally:
        transport.close()
