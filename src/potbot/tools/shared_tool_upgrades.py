"""S9: lease-fenced, tested and reversible shared tool upgrades.

All staging, promotion and recovery decisions are OPERATOR-OWNED. A model or
workspace shell must never receive this service, the ledger, or a write grant.
Staged tools are supplied by a separately qualified builder; this service does
not fetch remote code, execute hooks on the host or silently install packages.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from potbot.domain.errors import (
    CapabilityUnavailableError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.tools.agent_homes import AgentHomeRegistry
from potbot.tools.shared_tool_store import SharedToolDefinition, SharedToolStore
from potbot.tools.shared_workspace_isolation import (
    SharedWorkspaceBubblewrapBackend,
    SharedWorkspaceRuntime,
)
from potbot.tools.supervisor import ProcessRequest, ProcessSupervisor
from potbot.tools.workspace_concurrency import AccessRequest, AccessResource
from potbot.tools.workspace_supervision import (
    WorkspaceLeaseGuard,
    admission_digest,
    validate_runtime_pin,
)

if TYPE_CHECKING:
    from potbot.tools.ai_config_history import AiConfigHistoryMonitor

_SLUG = re.compile(r"[a-z][a-z0-9-]{1,62}\Z")
_KIND = frozenset({"smoke", "unit", "integration", "regression"})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_RENAME_EXCHANGE = 2  # Linux renameat2, atomic exchange of directory entries.
_MAX_CASES = 16


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _sha(value: object) -> str:
    return hashlib.sha256(_json(value).encode("ascii")).hexdigest()


def _no_link(path: Path, *, directory: bool = True) -> Path:
    if path.is_symlink() or not path.exists():
        raise ScopeDeniedError("S9 source is missing or a symbolic link")
    info = path.stat(follow_symlinks=False)
    if directory and not stat.S_ISDIR(info.st_mode):
        raise ScopeDeniedError("S9 source is not a real directory")
    return path.resolve(strict=True)


def _artifact_tree_identity(parent: Path) -> dict[str, object]:
    """Bounded content evidence for *all* reusable runtime assets, not only pins.

    Hash symlink text without following it; reject hardlinks which could share
    an inode with a different installation. This is NOT race-resistant against
    a privileged unbrokered host writer; that requires host filesystem policy.
    """
    count = total = 0
    digest = hashlib.sha256()

    def record(path: Path) -> None:
        nonlocal count, total
        if count >= 100_000:
            raise ScopeDeniedError("S9 runtime artifact inventory exceeds file count")
        count += 1
        info = path.lstat()
        relative = path.relative_to(parent).as_posix()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            value = [relative, "symlink", mode, os.readlink(path)]
        elif stat.S_ISDIR(info.st_mode):
            value = [relative, "directory", mode]
        elif stat.S_ISREG(info.st_mode):
            if info.st_nlink > 1:
                raise ScopeDeniedError("S9 runtime artifact shares a hardlinked inode")
            total += info.st_size
            if total > 64 * 1024**3:
                raise ScopeDeniedError("S9 runtime artifact inventory exceeds byte budget")
            contents = hashlib.sha256()
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(descriptor, "rb") as stream:
                identity = os.fstat(stream.fileno())
                if (identity.st_dev, identity.st_ino) != (info.st_dev, info.st_ino):
                    raise StateMismatchError("S9 asset changed during evidence capture")
                while chunk := stream.read(1024 * 1024):
                    contents.update(chunk)
            value = [relative, "file", mode, info.st_size, contents.hexdigest()]
        else:
            raise ScopeDeniedError("S9 asset store contains unsupported special files")
        digest.update((_json(value) + "\n").encode("ascii"))

    for area in ("environment", "static", "downloads"):
        top = parent / area
        for current, directories, files in os.walk(top, followlinks=False):
            base = Path(current)
            directories.sort()
            files.sort()
            if base == top:
                record(top)
            for name in list(directories):
                child = base / name
                record(child)
                if child.is_symlink():
                    directories.remove(name)
            for name in files:
                record(base / name)
    return {"files": count, "bytes": total, "sha256": digest.hexdigest()}


def stage_name(slug: str, operation_id: UUID) -> str:
    """Stable operator-side placement of one already-built candidate."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or not isinstance(operation_id, UUID):
        raise InvalidInputError("S9 stage requires a canonical tool and UUID")
    return f".potbot-s9-stage-{slug}-{operation_id.hex}"


def _exchange(parent: Path, left: str, right: str) -> None:
    """Refuse non-Linux/fallback copy; atomic switch must be kernel-supported."""
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise CapabilityUnavailableError("S9 activation requires Linux renameat2")
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, "renameat2", None)
    if rename is None:
        raise CapabilityUnavailableError("S9 activation requires atomic directory exchange")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                       ctypes.c_uint]
    rename.restype = ctypes.c_int
    root_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for name in (left, right):
            if not name or "/" in name or name in {".", ".."}:
                raise ScopeDeniedError("Invalid S9 exchange entry")
            info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise ScopeDeniedError("S9 exchange entry was replaced")
        if rename(root_fd, left.encode(), root_fd, right.encode(), _RENAME_EXCHANGE) != 0:
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number))
        os.fsync(root_fd)
    finally:
        os.close(root_fd)


@dataclass(frozen=True, slots=True)
class ToolTestCase:
    """An operator-reviewed argv, executed only by the sandbox supervisor."""

    name: str
    kind: str
    argv: tuple[str, ...]
    timeout_ms: int = 90_000

    def __post_init__(self) -> None:
        if (not isinstance(self.name, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{1,63}", self.name)
                or self.kind not in _KIND or not isinstance(self.argv, tuple)
                or not 1 <= len(self.argv) <= 64
                or any(not isinstance(s, str) or not s or "\x00" in s or "\n" in s
                       or "\r" in s or len(s.encode()) > 4096 for s in self.argv)
                or not self.argv[0].startswith("/")
                or self.argv[0].startswith(("/home/", "/usr/local/tools/", "/var/ai/"))
                or type(self.timeout_ms) is not int or not 100 <= self.timeout_ms <= 180_000):
            raise InvalidInputError("S9 test must be a bounded operator-pinned rootfs argv")
        for arg in self.argv:
            if "{" in arg or "}" in arg:
                for piece in re.findall(r"\{[^{}]+\}", arg):
                    if piece not in {"{checkout}", "{tool}", "{agent_home}"}:
                        raise InvalidInputError("S9 test has an unknown path placeholder")
                if arg.replace("{checkout}", "").replace("{tool}", "").replace(
                    "{agent_home}", ""
                ).find("{") >= 0 or "}" in arg.replace("{checkout}", "").replace(
                    "{tool}", ""
                ).replace("{agent_home}", ""):
                    raise InvalidInputError("S9 test placeholder is malformed")

    def render(self, *, slug: str, stage: str, agent: str) -> tuple[str, ...]:
        checkout = f"/usr/local/tools/{stage}/checkout"
        tool = f"/usr/local/tools/{stage}"
        home = f"/home/{agent}"
        return tuple(s.replace("{checkout}", checkout).replace("{tool}", tool)
                     .replace("{agent_home}", home) for s in self.argv)

    def public(self) -> dict[str, object]:
        return {"name": self.name, "kind": self.kind, "argv": list(self.argv),
                "timeout_ms": self.timeout_ms}


@dataclass(frozen=True, slots=True)
class ToolTestSuite:
    cases: tuple[ToolTestCase, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.cases, tuple) or not 2 <= len(self.cases) <= _MAX_CASES
                or any(not isinstance(c, ToolTestCase) for c in self.cases)
                or len({c.name for c in self.cases}) != len(self.cases)
                or not {"smoke", "unit"}.issubset({c.kind for c in self.cases})
                or sum(c.timeout_ms for c in self.cases) > 1_200_000):
            raise InvalidInputError("S9 suite needs distinct smoke + unit tests within budget")

    @property
    def digest(self) -> str:
        return _sha([c.public() for c in self.cases])


class TestExecutor(Protocol):
    def run(self, *, case: ToolTestCase, argv: tuple[str, ...], workspace_id: UUID,
            agent_id: UUID, agent_dir: Path, uid: int, gid: int,
            cancelled: Callable[[], bool]) -> dict[str, object]: ...


class SandboxedToolTestExecutor:
    """Tests staged sources in the read-only tools mount, never on the host.

    This does not compile new packages or grant staged binaries execution
    authority; the pinned system-image runner must already have dependencies.
    """

    def __init__(self, runtime: SharedWorkspaceRuntime, *,
                 monitor: object | None = None,
                 supervisor_factory: Callable[[SharedWorkspaceBubblewrapBackend], ProcessSupervisor]
                 = ProcessSupervisor) -> None:
        self.runtime = runtime
        self.monitor = monitor
        self.supervisor_factory = supervisor_factory

    def run(self, *, case: ToolTestCase, argv: tuple[str, ...], workspace_id: UUID,
            agent_id: UUID, agent_dir: Path, uid: int, gid: int,
            cancelled: Callable[[], bool]) -> dict[str, object]:
        backend = SharedWorkspaceBubblewrapBackend(self.runtime, ai_monitor=self.monitor)
        supervisor = self.supervisor_factory(backend)
        if not isinstance(supervisor, ProcessSupervisor):
            raise ScopeDeniedError("S9 requires Potbot's qualified ProcessSupervisor")
        request = ProcessRequest(
            argv=argv, cwd=agent_dir, workspace_root=self.runtime.home,
            sandbox_root=self.runtime.home, timeout_ms=case.timeout_ms,
            max_output_bytes=262_144, memory_bytes=8 * 1024**3,
            cpu_seconds=max(1, min(180, (case.timeout_ms + 999) // 1000)),
            guest_uid=uid, guest_gid=gid, telemetry_workspace_id=workspace_id,
            telemetry_agent_id=agent_id,
        )
        result = supervisor.run(request, cancelled=cancelled)
        output_hash = hashlib.sha256(result.stdout + b"\0" + result.stderr).hexdigest()
        return {
            "name": case.name, "kind": case.kind,
            "passed": result.dispatched and result.processes_stopped
            and result.outcome == "completed" and result.exit_code == 0
            and bool(result.isolation_identity),
            "outcome": result.outcome,
            "exit_code": result.exit_code,
            "elapsed_ms": result.elapsed_ms,
            "dispatched": result.dispatched,
            "stopped": result.processes_stopped,
            "isolation_identity": result.isolation_identity,
            "output_sha256": output_hash,
        }


class SharedToolUpgradeManager:
    """Persistent host-local journal with staged tests, atomic swap and rollback.

    Requires one controller-private SQLite lease ledger; it is *not* distributed
    transaction coordination. Any uncertain exchange stays pending until a
    separately approved and fenced reconciliation.
    """

    def __init__(
        self, *, store: SharedToolStore, runtime: SharedWorkspaceRuntime,
        homes: AgentHomeRegistry, authorize: Callable[[UUID, UUID], bool],
        ai_monitor: AiConfigHistoryMonitor | None = None,
        executor: TestExecutor | None = None,
    ) -> None:
        if not callable(authorize) or runtime.shared_tools is None:
            raise ScopeDeniedError("S9 requires an authorized shared-tool operator")
        root = _no_link(runtime.shared_tools)
        if root != store.root or root.is_relative_to(runtime.home.resolve(strict=True)):
            raise ScopeDeniedError("S9 runtime and registered tool store differ")
        self.store, self.runtime, self.homes = store, runtime, homes
        self.ai_monitor = ai_monitor
        self.authorize = authorize
        self.executor = executor or SandboxedToolTestExecutor(runtime, monitor=ai_monitor)
        with self.store.ledger._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS trusted_tool_upgrades (
                operation_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
                agent_id TEXT NOT NULL, slug TEXT NOT NULL,
                old_definition TEXT NOT NULL, new_definition TEXT NOT NULL,
                stage_name TEXT NOT NULL, suite_sha256 TEXT NOT NULL,
                evidence_json TEXT NOT NULL, evidence_sha256 TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN
                  ('ready','switching','active','rolling_back','rolled_back','aborted','indeterminate')),
                updated_ns INTEGER NOT NULL,
                UNIQUE(workspace_id,stage_name)
            )""")

    def _actor(self, workspace_id: UUID, agent_id: UUID) -> str:
        if not isinstance(workspace_id, UUID) or not isinstance(agent_id, UUID):
            raise InvalidInputError("S9 requires exact trusted identities")
        if self.authorize(workspace_id, agent_id) is not True:
            raise ScopeDeniedError("S9 tool upgrades require operator authorization")
        validate_runtime_pin(self.store.ledger, self.runtime, workspace_id)
        return str(self.store._require(workspace_id, agent_id))

    def _guard(self, workspace_id: UUID, agent_id: UUID, slug: str,
               key: str, operation: str) -> WorkspaceLeaseGuard:
        image = self._actor(workspace_id, agent_id)
        guard = WorkspaceLeaseGuard(self.store.ledger, AccessRequest(
            workspace_id, agent_id, operation, key, "write",
            (AccessResource("shared_tool", slug, "write"),), image,
        ))
        guard.start()
        return guard

    def _location(self, slug: str, operation_id: UUID) -> Path:
        return self.store.root / stage_name(slug, operation_id)

    def _verify_at(self, parent: Path, definition: SharedToolDefinition) -> dict[str, object]:
        root = self.store.root
        if parent.parent != root or not parent.name or parent.is_symlink():
            raise ScopeDeniedError("S9 installation must be directly beneath the tool root")
        _no_link(parent)
        for part in ("checkout", "environment", "static", "downloads"):
            _no_link(parent / part)
        checkout = parent / "checkout"
        _no_link(checkout / ".git")
        git = self.store._git
        if (git(checkout, "rev-parse", "--verify", "HEAD") != definition.commit
                or Path(git(checkout, "rev-parse", "--show-toplevel")).resolve() != checkout
                or git(checkout, "remote", "get-url", "origin") != definition.repository
                or git(checkout, "status", "--porcelain=v1", "--untracked-files=all")
                or git(checkout, "clean", "-ndx")):
            raise StateMismatchError("S9 checkout is not the exact clean pinned source")
        locks: dict[str, str] = {}
        for relative, digest in definition.lockfiles:
            path = checkout / relative
            if path.is_symlink() or not path.is_file():
                raise StateMismatchError("S9 dependency lockfile missing or redirected")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                raise StateMismatchError("S9 dependency lockfile differs from pin")
            locks[relative] = digest
        assets: dict[str, str] = {}
        for relative, digest in definition.reusable_artifacts:
            path = parent / relative
            if path.is_symlink() or not path.is_file() or not path.resolve(strict=True).is_relative_to(parent):
                raise StateMismatchError("S9 reused artifact missing or redirected")
            actual = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    actual.update(chunk)
            if actual.hexdigest() != digest:
                raise StateMismatchError("S9 reusable artifact digest mismatch")
            assets[relative] = digest
        for relative in definition.cleanup_paths:
            if git(checkout, "ls-files", "--cached", "--", relative):
                raise ScopeDeniedError("S9 clean manifest overlaps tracked source")
        return {"commit": definition.commit, "remote": definition.repository,
                "locks": locks, "reusable": assets,
                "artifact_tree": _artifact_tree_identity(parent),
                "source_digest": _sha({"commit": definition.commit, "locks": locks,
                                       "reusable": assets})}

    def _clean_probe(self, parent: Path, definition: SharedToolDefinition) -> str:
        """Exercise the *actual shared cleanup implementation* on disposable files.

        Only declared, initially absent checkout paths may be touched. This
        does not run an untrusted repository cleanup script on the host.
        """
        checkout = parent / "checkout"
        self._verify_at(parent, definition)
        for relative in definition.cleanup_paths:
            target = checkout / relative
            if target.exists() or target.is_symlink():
                raise StateMismatchError("S9 cleanup probe path is not initially empty")
            parents = list(target.parents)
            if any(item.is_symlink() for item in parents if item.is_relative_to(checkout)):
                raise ScopeDeniedError("S9 cleanup parent symlink denied")
            target.mkdir(parents=True, exist_ok=False)
            (target / "potbot-s9-clean-probe").write_bytes(b"potbot-s9-clean-probe\n")
        for relative in definition.cleanup_paths:
            if not self.store._remove_declared(checkout, relative):
                raise StateMismatchError("S9 cleanup routine failed to remove a declared path")
        self._verify_at(parent, definition)
        return _sha({"paths": definition.cleanup_paths, "commit": definition.commit})

    def _registered(self, db: Any, workspace_id: UUID, slug: str) -> str:
        row = db.execute(
            "SELECT definition_json FROM trusted_shared_tools WHERE workspace_id=? AND slug=?",
            (str(workspace_id), slug),
        ).fetchone()
        if row is None:
            raise StateMismatchError("S9 requires a registered existing installation")
        return str(row["definition_json"])

    def _row(self, db: Any, operation_id: UUID) -> dict[str, Any]:
        row = db.execute("SELECT * FROM trusted_tool_upgrades WHERE operation_id=?",
                         (str(operation_id),)).fetchone()
        if row is None:
            raise StateMismatchError("S9 upgrade transaction does not exist")
        return dict(row)

    def prepare(
        self, *, workspace_id: UUID, agent_id: UUID, operation_id: UUID,
        expected_old_commit: str, replacement: SharedToolDefinition,
        suite: ToolTestSuite,
    ) -> dict[str, object]:
        """Test a host-provisioned candidate. Leaves the active install alone."""
        if not isinstance(replacement, SharedToolDefinition) or not isinstance(suite, ToolTestSuite):
            raise InvalidInputError("S9 prepare requires a version-pinned definition and test suite")
        if not isinstance(operation_id, UUID) or not _SLUG.fullmatch(replacement.slug):
            raise InvalidInputError("S9 prepare requires a canonical upgrade identity")
        slug = replacement.slug
        if not isinstance(expected_old_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", expected_old_commit):
            raise InvalidInputError("S9 requires an exact expected predecessor commit")
        guard = self._guard(workspace_id, agent_id, slug, f"s9-prepare-{operation_id.hex}",
                            "tool.test_upgrade")
        uncertain = False
        try:
            old = self.store.definition(workspace_id, slug)
            if old.commit != expected_old_commit or old.commit == replacement.commit:
                raise StateMismatchError("S9 upgrade predecessor or candidate revision changed")
            if old.repository != replacement.repository:
                raise ScopeDeniedError("S9 cannot silently replace a tool's GitHub publisher")
            old_json = json.dumps(old.to_dict(), sort_keys=True)
            new_json = json.dumps(replacement.to_dict(), sort_keys=True)
            self._verify_at(self.store.root / slug, old)
            stage = self._location(slug, operation_id)
            verified = self._verify_at(stage, replacement)
            with self.store.ledger._connect() as db:
                if self._registered(db, workspace_id, slug) != old_json:
                    raise StateMismatchError("S9 registered tool changed since plan creation")
                existing = db.execute(
                    "SELECT 1 FROM trusted_tool_upgrades WHERE workspace_id=? AND slug=? "
                    "AND state IN ('ready','switching','rolling_back','indeterminate')",
                    (str(workspace_id), slug),
                ).fetchone()
                if existing is not None:
                    raise StateMismatchError("S9 has an uncompleted upgrade for this tool")
            agent = self.homes.lookup(workspace_id, agent_id)
            if agent is None:
                raise ScopeDeniedError("S9 requires a registered agent home")
            agent_dir = self.runtime.home / agent.directory
            _no_link(agent_dir)
            results: list[dict[str, object]] = []
            for case in suite.cases:
                guard.ensure_live()
                argv = case.render(slug=slug, stage=stage.name, agent=agent.directory)
                uncertain = True
                result = self.executor.run(
                    case=case, argv=argv, workspace_id=workspace_id,
                    agent_id=agent_id, agent_dir=agent_dir,
                    uid=agent.uid, gid=agent.gid, cancelled=lambda: guard.fenced,
                )
                if not isinstance(result, dict):
                    raise StateMismatchError("S9 test supervisor returned an invalid receipt")
                if result.get("stopped") is True:
                    # A confirmed stopped test process cannot leave a running writer.
                    # Validate the immutable stage before reporting a test failure.
                    self._verify_at(stage, replacement)
                    uncertain = False
                if result.get("name") != case.name or (
                    result.get("kind") != case.kind or result.get("passed") is not True
                    or not isinstance(result.get("output_sha256"), str)
                    or not _DIGEST.fullmatch(result["output_sha256"])
                    or not isinstance(result.get("isolation_identity"), str)
                    or not result["isolation_identity"]
                    or result.get("dispatched") is not True or result.get("stopped") is not True
                    or result.get("exit_code") != 0 or result.get("outcome") != "completed"
                ):
                    raise StateMismatchError("S9 sandboxed tool test did not pass")
                results.append({k: result[k] for k in (
                    "name", "kind", "outcome", "exit_code", "elapsed_ms", "dispatched",
                    "stopped", "isolation_identity", "output_sha256",
                )})
                if self._verify_at(stage, replacement) != verified:
                    raise StateMismatchError("S9 candidate changed during tests")
            uncertain = True
            clean_digest = self._clean_probe(stage, replacement)
            uncertain = False
            if self._verify_at(stage, replacement) != verified:
                raise StateMismatchError("S9 staged installation changed after cleanup acceptance")
            evidence = {
                "schema": "potbot-shared-tool-test/v1", "workspace_id": str(workspace_id),
                "agent_id": str(agent_id), "slug": slug, "operation_id": str(operation_id),
                "previous": old.commit, "candidate": replacement.commit,
                "image": self.runtime.image_digest, "suite_sha256": suite.digest,
                "candidate_identity": verified, "clean_probe_sha256": clean_digest,
                "cases": results,
            }
            evidence_digest = _sha(evidence)
            guard.ensure_live()
            with self.store.ledger._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if self._registered(db, workspace_id, slug) != old_json:
                    raise StateMismatchError("S9 tool changed during test admission")
                db.execute(
                    "INSERT INTO trusted_tool_upgrades VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(operation_id), str(workspace_id), str(agent_id), slug,
                     old_json, new_json, stage.name, suite.digest,
                     _json(evidence), evidence_digest, "ready", time.time_ns()),
                )
            guard.finish(evidence_sha256=evidence_digest)
            return {"id": str(operation_id), "state": "ready", "slug": slug,
                    "old_commit": old.commit, "new_commit": replacement.commit,
                    "suite_sha256": suite.digest, "evidence_sha256": evidence_digest,
                    "passed_tests": len(results), "clean_test_passed": True}
        except BaseException:
            try:
                guard.finish(evidence_sha256=admission_digest("s9-prepare-rejected"),
                             uncertain=uncertain)
            except StateMismatchError:
                guard.abandon()
            raise

    def _transaction(self, workspace_id: UUID, agent_id: UUID, operation_id: UUID, slug: str,
                     *, allowed: set[str]) -> tuple[dict[str, Any], SharedToolDefinition,
                                                         SharedToolDefinition]:
        with self.store.ledger._connect() as db:
            row = self._row(db, operation_id)
            if (row["workspace_id"] != str(workspace_id) or row["slug"] != slug
                    or row["agent_id"] != str(agent_id)):
                raise ScopeDeniedError("S9 transaction is not in the selected workspace")
            if row["state"] not in allowed:
                raise StateMismatchError("S9 upgrade state does not permit this operation")
            old = SharedToolDefinition.from_dict(json.loads(row["old_definition"]))
            new = SharedToolDefinition.from_dict(json.loads(row["new_definition"]))
            if (old.slug != slug or new.slug != slug or old.repository != new.repository
                    or stage_name(slug, operation_id) != row["stage_name"]):
                raise StateMismatchError("S9 transaction identity was altered")
            evidence = json.loads(row["evidence_json"])
            if row["evidence_sha256"] != _sha(evidence) or (
                evidence.get("candidate") != new.commit
                or evidence.get("previous") != old.commit
                or evidence.get("suite_sha256") != row["suite_sha256"]
            ):
                raise StateMismatchError("S9 upgrade test evidence was altered")
            return row, old, new

    def _state(self, operation_id: UUID, state: str, *, expect: str) -> None:
        with self.store.ledger._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                "UPDATE trusted_tool_upgrades SET state=?, updated_ns=? "
                "WHERE operation_id=? AND state=?",
                (state, time.time_ns(), str(operation_id), expect),
            )
            if result.rowcount != 1:
                raise StateMismatchError("S9 upgrade state changed concurrently")

    def _commit_swap(self, operation_id: UUID, workspace_id: UUID, slug: str,
                     *, old_record: str, new_record: str, state: str) -> None:
        with self.store.ledger._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                "UPDATE trusted_shared_tools SET definition_json=? "
                "WHERE workspace_id=? AND slug=? AND definition_json=?",
                (new_record, str(workspace_id), slug, old_record),
            )
            if result.rowcount != 1:
                raise StateMismatchError("S9 installed tool CAS rejected the exchanged candidate")
            result = db.execute(
                "UPDATE trusted_tool_upgrades SET state=?,updated_ns=? "
                "WHERE operation_id=? AND state=?",
                ("active" if state == "switching" else "rolled_back",
                 time.time_ns(), str(operation_id), state),
            )
            if result.rowcount != 1:
                raise StateMismatchError("S9 upgrade journal was altered during activation")

    def activate(self, *, workspace_id: UUID, agent_id: UUID,
                 operation_id: UUID, slug: str, expected_old_commit: str,
                 confirm: bool) -> dict[str, str]:
        if confirm is not True:
            raise ScopeDeniedError("S9 upgrade requires explicit operator approval")
        guard = self._guard(workspace_id, agent_id, slug, f"s9-activate-{operation_id.hex}",
                            "tool.activate_upgrade")
        uncertain = False
        try:
            row, old, new = self._transaction(workspace_id, agent_id, operation_id, slug, allowed={"ready"})
            if old.commit != expected_old_commit or old.commit == new.commit:
                raise StateMismatchError("S9 predecessor pin changed")
            with self.store.ledger._connect() as db:
                if self._registered(db, workspace_id, slug) != row["old_definition"]:
                    raise StateMismatchError("S9 active source changed since candidate tests")
            self._verify_at(self.store.root / slug, old)
            current = self._verify_at(self._location(slug, operation_id), new)
            if current != json.loads(row["evidence_json"])["candidate_identity"]:
                raise StateMismatchError("S9 stage changed since tested evidence")
            guard.ensure_live()
            self._state(operation_id, "switching", expect="ready")
            uncertain = True
            _exchange(self.store.root, slug, row["stage_name"])
            self._verify_at(self.store.root / slug, new)
            self._verify_at(self._location(slug, operation_id), old)
            self._commit_swap(operation_id, workspace_id, slug,
                              old_record=row["old_definition"],
                              new_record=row["new_definition"], state="switching")
            guard.ensure_live()
            guard.finish(evidence_sha256=admission_digest("s9-active:" + row["evidence_sha256"]))
            return {"id": str(operation_id), "slug": slug, "state": "active",
                    "old_commit": old.commit, "new_commit": new.commit}
        except BaseException:
            try:
                guard.finish(evidence_sha256=admission_digest("s9-switch-uncertain"),
                             uncertain=uncertain)
            except StateMismatchError:
                guard.abandon()
            raise

    def rollback(self, *, workspace_id: UUID, agent_id: UUID,
                 operation_id: UUID, slug: str, expected_current_commit: str,
                 confirm: bool) -> dict[str, str]:
        if confirm is not True:
            raise ScopeDeniedError("S9 rollback requires explicit operator approval")
        guard = self._guard(workspace_id, agent_id, slug, f"s9-rollback-{operation_id.hex}",
                            "tool.rollback_upgrade")
        uncertain = False
        try:
            row, old, new = self._transaction(workspace_id, agent_id, operation_id, slug, allowed={"active"})
            if new.commit != expected_current_commit:
                raise StateMismatchError("S9 current release pin changed")
            with self.store.ledger._connect() as db:
                if self._registered(db, workspace_id, slug) != row["new_definition"]:
                    raise StateMismatchError("S9 rollback would overwrite a newer upgrade")
            self._verify_at(self.store.root / slug, new)
            self._verify_at(self._location(slug, operation_id), old)
            guard.ensure_live()
            self._state(operation_id, "rolling_back", expect="active")
            uncertain = True
            _exchange(self.store.root, slug, row["stage_name"])
            self._verify_at(self.store.root / slug, old)
            self._verify_at(self._location(slug, operation_id), new)
            self._commit_swap(operation_id, workspace_id, slug,
                              old_record=row["new_definition"],
                              new_record=row["old_definition"], state="rolling_back")
            guard.ensure_live()
            guard.finish(evidence_sha256=admission_digest("s9-rollback:" + row["evidence_sha256"]))
            return {"id": str(operation_id), "slug": slug, "state": "rolled_back",
                    "old_commit": old.commit, "new_commit": new.commit}
        except BaseException:
            try:
                guard.finish(evidence_sha256=admission_digest("s9-rollback-uncertain"),
                             uncertain=uncertain)
            except StateMismatchError:
                guard.abandon()
            raise

    def reconcile(self, *, workspace_id: UUID, agent_id: UUID,
                  operation_id: UUID, slug: str, confirm: bool) -> dict[str, str]:
        """Host-only interrupted-exchange recovery, never automatic on startup.

        A failed/expired lease must FIRST be independently reconciled by the
        controller's lease operator. A second writer cannot bypass that fence.
        """
        if confirm is not True:
            raise ScopeDeniedError("S9 recovery requires operator confirmation")
        guard = self._guard(workspace_id, agent_id, slug, f"s9-recover-{operation_id.hex}",
                            "tool.reconcile_upgrade")
        uncertain = True
        try:
            row, old, new = self._transaction(
                workspace_id, agent_id, operation_id, slug,
                allowed={"switching", "rolling_back"},
            )
            with self.store.ledger._connect() as db:
                recorded = self._registered(db, workspace_id, slug)
            if recorded != (row["old_definition"] if row["state"] == "switching"
                            else row["new_definition"]):
                raise StateMismatchError("S9 installed definition inconsistent with recovery journal")
            active, staged = self.store.root / slug, self._location(slug, operation_id)
            # Identify a physically intact exchange using pinned source and
            # artifacts, not timestamps or a guessed process outcome.
            old_active = new_active = old_staged = new_staged = False
            for location, definition, flag in (
                (active, old, "old_active"), (active, new, "new_active"),
                (staged, old, "old_staged"), (staged, new, "new_staged"),
            ):
                try:
                    self._verify_at(location, definition)
                except (ScopeDeniedError, StateMismatchError, OSError):
                    continue
                if flag == "old_active":
                    old_active = True
                elif flag == "new_active":
                    new_active = True
                elif flag == "old_staged":
                    old_staged = True
                else:
                    new_staged = True
            if row["state"] == "switching":
                if old_active and new_staged:  # Swap never took place.
                    self._state(operation_id, "aborted", expect="switching")
                    final = "aborted"
                elif new_active and old_staged:  # Undo a completed physical exchange.
                    _exchange(self.store.root, slug, row["stage_name"])
                    self._verify_at(active, old)
                    self._verify_at(staged, new)
                    self._state(operation_id, "aborted", expect="switching")
                    final = "aborted"
                else:
                    raise StateMismatchError("S9 activation requires manual filesystem recovery")
            else:
                if new_active and old_staged:  # Rollback did not exchange.
                    self._state(operation_id, "active", expect="rolling_back")
                    final = "active"
                elif old_active and new_staged:  # Exchange occurred, DB commit did not.
                    self._commit_swap(
                        operation_id, workspace_id, slug,
                        old_record=row["new_definition"],
                        new_record=row["old_definition"], state="rolling_back",
                    )
                    final = "rolled_back"
                else:
                    raise StateMismatchError("S9 rollback requires manual filesystem recovery")
            guard.ensure_live()
            guard.finish(evidence_sha256=admission_digest("s9-recovered:" + final))
            uncertain = False
            return {"id": str(operation_id), "slug": slug, "state": final}
        except BaseException:
            if uncertain:
                try:
                    guard.finish(evidence_sha256=admission_digest("s9-recovery-unknown"),
                                 uncertain=True)
                except StateMismatchError:
                    guard.abandon()
            raise

    def runtime_entrypoint_updates(self, *, workspace_id: UUID, agent_id: UUID,
                                   operation_id: UUID) -> dict[str, object]:
        """Operator-only proposed executable pins; DOES NOT mutate live runtime.

        Consumers must independently repin the verified sandbox runtime after
        activation. A stale hash fails closed, never runs the new binary.
        """
        self._actor(workspace_id, agent_id)
        with self.store.ledger._connect() as db:
            row = self._row(db, operation_id)
        if row["workspace_id"] != str(workspace_id) or row["agent_id"] != str(agent_id):
            raise ScopeDeniedError("S9 runtime pin plan unavailable")
        if row["state"] not in {"ready", "active"}:
            raise StateMismatchError("S9 executable pins require a tested candidate")
        definition = SharedToolDefinition.from_dict(json.loads(row["new_definition"]))
        expected = dict(definition.reusable_artifacts)
        # Candidate remains at the stage location until activation; afterward
        # it resides at the active path. This method is only a *proposal*;
        # the host operator must reload the runtime with these hashes.
        stage = (self._location(definition.slug, operation_id)
                 if row["state"] == "ready" else self.store.root / definition.slug)
        self._verify_at(stage, definition)
        pins: dict[str, str] = {}
        for relative, digest in expected.items():
            if not relative.startswith(("static/", "environment/")):
                continue
            artifact = stage / relative
            if artifact.is_symlink() or not artifact.is_file():
                raise StateMismatchError("S9 executable pin candidate is unavailable")
            if artifact.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                pins[f"/usr/local/tools/{definition.slug}/{relative}"] = digest
        retired = sorted(path for path, _ in self.runtime.tool_entrypoints
                         if path.startswith(f"/usr/local/tools/{definition.slug}/")
                         and path not in pins)
        return {"slug": definition.slug, "candidate_commit": definition.commit,
                "pins": pins, "remove_prior_pins": retired,
                "requires_operator_runtime_repin": True,
                "tested_sha256": row["evidence_sha256"]}

    def inspect(self, *, workspace_id: UUID, agent_id: UUID,
                operation_id: UUID) -> dict[str, object]:
        """Sanitized audit projection; no private ledger capability or raw logs."""
        self._actor(workspace_id, agent_id)
        with self.store.ledger._connect() as db:
            row = self._row(db, operation_id)
        if row["workspace_id"] != str(workspace_id):
            raise ScopeDeniedError("S9 upgrade record unavailable")
        old, new = (json.loads(row[key]) for key in ("old_definition", "new_definition"))
        evidence = json.loads(row["evidence_json"])
        if _sha(evidence) != row["evidence_sha256"]:
            raise StateMismatchError("S9 audit evidence integrity mismatch")
        return {"id": str(operation_id), "slug": row["slug"], "state": row["state"],
                "old_commit": old["commit"], "new_commit": new["commit"],
                "suite_sha256": row["suite_sha256"],
                "test_evidence_sha256": row["evidence_sha256"],
                "test_results": [{"name": item["name"], "kind": item["kind"],
                                  "elapsed_ms": item["elapsed_ms"], "passed": True}
                                 for item in evidence["cases"]]}
