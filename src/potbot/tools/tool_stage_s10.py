"""S10: operator-only sandbox-built, offline candidate staging for S9 upgrades.

No host execution of repository content, hooks, package installers, or shell.
Build runs through the S7 monitored ProcessSupervisor and immutable rootfs;
copying operates on exact directory descriptors with no symlink following.
The old installation remains untouched. An incomplete incoming directory or
unrecorded published stage requires operator reconciliation, never auto-retry.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.domain.errors import (
    CapabilityUnavailableError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)

if TYPE_CHECKING:
    from potbot.tools.agent_homes import AgentHomeRegistry
    from potbot.tools.ai_workspace_operator import AiWorkspaceOperator
    from potbot.tools.shared_tool_store import SharedToolStore

_SLUG = re.compile(r"[a-z][a-z0-9-]{1,62}\Z")
_HEX = re.compile(r"[a-f0-9]{64}\Z")
_MAX_ENTRIES = 50_000
_MAX_BYTES = 16 * 1024**3
_MAX_FILE = 4 * 1024**3


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True) + "\n").encode("ascii")


def command_digest(argv: tuple[str, ...]) -> str:
    """Pin an exact argv; never string-join and invoke a shell."""
    if not isinstance(argv, tuple) or not 1 <= len(argv) <= 64 or any(
        not isinstance(x, str) or not x or "\x00" in x or "\r" in x
        or "\n" in x or len(x.encode()) > 8192 for x in argv
    ) or not argv[0].startswith("/") or argv[0].startswith(
        ("/home/", "/usr/local/tools/", "/var/ai/")
    ):
        raise InvalidInputError("S10 requires an exact rootfs build command")
    return hashlib.sha256(_canonical(list(argv))).hexdigest()


def stage_name(slug: str, operation_id: UUID) -> str:
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or not isinstance(operation_id, UUID):
        raise InvalidInputError("S10 stage needs an exact tool and operation ID")
    return f".potbot-s9-stage-{slug}-{operation_id.hex}"


def _directory(path: Path) -> Path:
    if path.is_symlink() or not path.is_dir() or path.resolve(strict=True) != path.absolute():
        raise ScopeDeniedError("S10 path must be an existing physical directory")
    return path


def _regular_root(path: Path, *, writable: bool = False) -> Path:
    root = _directory(path)
    if root.stat().st_mode & 0o002:
        raise ScopeDeniedError("S10 operator directory is world writable")
    if not writable and root.stat().st_mode & 0o022:
        raise ScopeDeniedError("S10 operator evidence directory must not be group writable")
    return root


@dataclass(frozen=True, slots=True)
class ToolBuildPlan:
    workspace_id: UUID
    agent_id: UUID
    operation_id: UUID
    slug: str
    build_argv: tuple[str, ...]
    approved_argv_sha256: str
    expected_old_commit: str
    source_commit: str
    timeout_ms: int = 600_000

    def __post_init__(self) -> None:
        if not all(isinstance(x, UUID) for x in (
            self.workspace_id, self.agent_id, self.operation_id
        )) or not _SLUG.fullmatch(self.slug):
            raise InvalidInputError("S10 requires trusted operation identities")
        if not all(isinstance(x, str) and re.fullmatch(r"[a-f0-9]{40}", x)
                   for x in (self.expected_old_commit, self.source_commit)):
            raise InvalidInputError("S10 requires exact Git revisions")
        if not isinstance(self.approved_argv_sha256, str) or not _HEX.fullmatch(
            self.approved_argv_sha256
        ) or command_digest(self.build_argv) != self.approved_argv_sha256:
            raise ScopeDeniedError("S10 build command differs from operator approval")
        if type(self.timeout_ms) is not int or not 100 <= self.timeout_ms <= 900_000:
            raise InvalidInputError("S10 build deadline is outside policy")


class _SafeCopy:
    """Copy only regular files/physical directories via no-follow descriptors.

    The candidate is untrusted; this is bounded *data* transfer only. No copied
    file is executed by the operator. The S9 test suite independently verifies
    Git HEAD, clean worktree, lockfiles and build artifact hashes afterward.
    """

    def __init__(self) -> None:
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise CapabilityUnavailableError("S10 staging requires Linux directory descriptors")
        self.entries = 0
        self.bytes = 0
        self.digest = hashlib.sha256()

    def copy(self, source: Path, destination: Path) -> dict[str, object]:
        _directory(source)
        _directory(destination)
        src_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        dst_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self._walk(src_fd, dst_fd, "", 0)
            os.fsync(dst_fd)
        finally:
            os.close(src_fd)
            os.close(dst_fd)
        return {"files_and_directories": self.entries, "bytes": self.bytes,
                "tree_sha256": self.digest.hexdigest()}

    def _walk(self, source_fd: int, dest_fd: int, prefix: str, depth: int) -> None:
        if depth > 28:
            raise ScopeDeniedError("S10 source tree is too deep")
        names = sorted(os.listdir(source_fd))
        for name in names:
            if name in {".", ".."} or not name or "/" in name or "\x00" in name \
                    or len(name.encode()) > 240:
                raise ScopeDeniedError("S10 source has an unsafe component")
            path = f"{prefix}/{name}" if prefix else name
            self.entries += 1
            if self.entries > _MAX_ENTRIES:
                raise ScopeDeniedError("S10 source exceeds entry limit")
            info = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            mode = stat.S_IMODE(info.st_mode)
            if mode & 0o6000:
                raise ScopeDeniedError("S10 source includes privileged mode bits")
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=source_fd)
                try:
                    if os.fstat(child).st_ino != info.st_ino or os.fstat(child).st_dev != info.st_dev:
                        raise StateMismatchError("S10 directory changed while staging")
                    os.mkdir(name, 0o700, dir_fd=dest_fd)
                    new_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                     dir_fd=dest_fd)
                    try:
                        self.digest.update(_canonical([path, "dir", mode]))
                        self._walk(child, new_fd, path, depth + 1)
                        os.fchmod(new_fd, 0o775 if mode & stat.S_IWGRP else 0o755)
                        os.fsync(new_fd)
                    finally:
                        os.close(new_fd)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1 or info.st_size > _MAX_FILE or self.bytes + info.st_size > _MAX_BYTES:
                    raise ScopeDeniedError("S10 source file exceeds bounds or is hardlinked")
                from_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_fd)
                try:
                    before = os.fstat(from_fd)
                    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                            info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
                        raise StateMismatchError("S10 file changed before transfer")
                    to_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                    0o600, dir_fd=dest_fd)
                    hasher = hashlib.sha256()
                    count = 0
                    try:
                        while part := os.read(from_fd, 1024 * 1024):
                            count += len(part)
                            if count > info.st_size:
                                raise StateMismatchError("S10 file size changed mid-copy")
                            hasher.update(part)
                            view = memoryview(part)
                            while view:
                                written = os.write(to_fd, view)
                                if written <= 0:
                                    raise StateMismatchError("S10 staged file write failed")
                                view = view[written:]
                        if count != info.st_size:
                            raise StateMismatchError("S10 file was shortened")
                        after = os.fstat(from_fd)
                        if (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns) != (
                                before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns):
                            raise StateMismatchError("S10 file changed during transfer")
                        os.fchmod(to_fd, 0o755 if mode & 0o111 else 0o644)
                        os.fsync(to_fd)
                    finally:
                        os.close(to_fd)
                    self.bytes += count
                    self.digest.update(_canonical([path, "file", count, hasher.hexdigest(), bool(mode & 0o111)]))
                finally:
                    os.close(from_fd)
            else:
                raise ScopeDeniedError("S10 source includes a symlink or special file")


class OfflineToolStager:
    """Operator-only transition from *observed sandbox build* to an S9 test stage."""

    def __init__(self, *, shared_tools: Path, evidence_dir: Path) -> None:
        self.shared_tools = _directory(shared_tools)
        self.evidence_dir = _regular_root(evidence_dir)
        if self.shared_tools == self.evidence_dir or (
            self.shared_tools.is_relative_to(self.evidence_dir)
            or self.evidence_dir.is_relative_to(self.shared_tools)
        ):
            raise ScopeDeniedError("S10 evidence must not share the writable tool mount")

    def _receipt_path(self, operation_id: UUID) -> Path:
        return self.evidence_dir / (f"s10-{operation_id.hex}.json")

    def inspect(self, operation_id: UUID) -> dict[str, Any]:
        path = self._receipt_path(operation_id)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16000:
            raise StateMismatchError("S10 stage receipt is unavailable")
        try:
            record = json.loads(path.read_text(encoding="ascii"))
        except (OSError, ValueError, UnicodeError):
            raise StateMismatchError("S10 stage receipt is damaged") from None
        if not isinstance(record, dict) or record.get("operation_id") != str(operation_id):
            raise StateMismatchError("S10 stage receipt identity is invalid")
        return record

    def stage(
        self, *, plan: ToolBuildPlan, built_tree: Path,
        result: object, expected_source_commit: str,
    ) -> dict[str, Any]:
        """Accept only a real supervisor result; stage is atomic, not an upgrade.

        The outer orchestration MUST first own a per-tool write lease and S7
        monitor; this method must never be directly exposed to models or HTTP.
        """
        if expected_source_commit != plan.source_commit:
            raise StateMismatchError("S10 build source revision differs from plan")
        if not (getattr(result, "dispatched", None) is True
                and getattr(result, "processes_stopped", None) is True
                and getattr(result, "outcome", None) == "completed"
                and getattr(result, "exit_code", None) == 0
                and isinstance(getattr(result, "isolation_identity", None), str)
                and len(result.isolation_identity) == 64
                and _HEX.fullmatch(result.isolation_identity)):
            raise StateMismatchError("S10 build lacks a qualified completed process receipt")
        root = self.shared_tools
        output = _directory(built_tree)
        if root == output or root.is_relative_to(output) or output.is_relative_to(root):
            raise ScopeDeniedError("S10 build must originate outside the shared tool store")
        target = stage_name(plan.slug, plan.operation_id)
        incoming = f".potbot-s10-incoming-{plan.operation_id.hex}"
        if ((root / target).exists() or (root / target).is_symlink()
                or (root / incoming).exists() or (root / incoming).is_symlink()):
            raise StateMismatchError("S10 previous stage or interrupted transfer needs reconciliation")
        if self._receipt_path(plan.operation_id).exists():
            raise StateMismatchError("S10 operation ID is already recorded")
        os.mkdir(root / incoming, 0o700)
        # If copying fails the residue remains quarantined for operator review.
        evidence = _SafeCopy().copy(output, root / incoming)
        for part in ("checkout", "environment", "static", "downloads"):
            if not (root / incoming / part).is_dir() or (root / incoming / part).is_symlink():
                raise StateMismatchError("S10 candidate lacks an S9 installation area")
        # The in-sandbox test runner must be able to read, but never write, the
        # stage through its read-only shared-tools mount.
        os.chmod(root / incoming, 0o755)
        # Commit filesystem placement before recording approval-independent evidence.
        os.rename(root / incoming, root / target)
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        record = {
            "schema": "potbot-tool-stage-s10/v1",
            "workspace_id": str(plan.workspace_id), "agent_id": str(plan.agent_id),
            "operation_id": str(plan.operation_id), "slug": plan.slug,
            "expected_old_commit": plan.expected_old_commit,
            "new_commit": plan.source_commit,
            "command_sha256": plan.approved_argv_sha256,
            "isolation_identity": result.isolation_identity,
            "stage_name": target,
            "inventory": evidence,
            "recorded_ns": time.time_ns(),
        }
        path = self._receipt_path(plan.operation_id)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600)
        try:
            encoded = _canonical(record)
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise StateMismatchError("S10 stage receipt could not be persisted")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        descriptor = os.open(self.evidence_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return record


def build_and_stage(
    *, plan: ToolBuildPlan, operator: AiWorkspaceOperator,
    store: SharedToolStore, homes: AgentHomeRegistry,
    evidence_dir: Path, built_tree: Path,
) -> dict[str, Any]:
    """Run one pinned offline builder under the operator, then stage its output.

    Caller MUST supply an already-provisioned, agent-owned build directory;
    external source acquisition and model downloads are never inferred.
    """
    from potbot.tools.supervisor import ProcessRequest
    from potbot.tools.workspace_concurrency import AccessRequest, AccessResource
    from potbot.tools.workspace_supervision import WorkspaceLeaseGuard, admission_digest

    if plan.workspace_id != operator.workspace_id:
        raise ScopeDeniedError("S10 workspace differs from active operator")
    runtime = operator.runtime
    if runtime.shared_tools is None or runtime.shared_tools.resolve(strict=True) != store.root:
        raise ScopeDeniedError("S10 stage root differs from registered tool store")
    agent = homes.lookup(plan.workspace_id, plan.agent_id)
    if agent is None:
        raise ScopeDeniedError("S10 builder requires a registered agent")
    agent_home = runtime.home / agent.directory
    # Require caller-supplied output underneath the real agent home, not
    # mutable checkout aliases or any other agent's directories.
    build_root = _directory(built_tree)
    if agent_home.is_symlink() or not build_root.is_relative_to(agent_home):
        raise ScopeDeniedError("S10 build output must be inside approved agent home")
    if runtime.ai_config_dir is None or runtime.ai_history_dir is None:
        raise CapabilityUnavailableError("S10 requires the managed S7 AI history watcher")
    operator.monitor.require_active()
    guard = WorkspaceLeaseGuard(store.ledger, AccessRequest(
        plan.workspace_id, plan.agent_id, "tool.stage_s10", "s10-" + plan.operation_id.hex,
        "write", (AccessResource("shared_tool", plan.slug, "write"),), runtime.image_digest,
    ))
    guard.start()
    uncertain = True
    try:
        request = ProcessRequest(
            argv=plan.build_argv, cwd=build_root,
            workspace_root=runtime.home, sandbox_root=runtime.home,
            timeout_ms=plan.timeout_ms, max_output_bytes=262_144,
            memory_bytes=8 * 1024**3,
            cpu_seconds=min(900, max(1, (plan.timeout_ms + 999) // 1000)),
            guest_uid=agent.uid, guest_gid=agent.gid,
            telemetry_workspace_id=plan.workspace_id,
            telemetry_agent_id=plan.agent_id,
        )
        result = operator.supervisor.run(request, cancelled=lambda: guard.fenced)
        guard.ensure_live()
        stager = OfflineToolStager(shared_tools=store.root, evidence_dir=evidence_dir)
        record = stager.stage(
            plan=plan, built_tree=build_root, result=result,
            expected_source_commit=plan.source_commit,
        )
        guard.finish(evidence_sha256=admission_digest("s10-staged:" + record["inventory"]["tree_sha256"]))
        uncertain = False
        return record
    finally:
        if uncertain:
            try:
                guard.finish(evidence_sha256=admission_digest("s10-staging-uncertain"), uncertain=True)
            except StateMismatchError:
                guard.abandon()
