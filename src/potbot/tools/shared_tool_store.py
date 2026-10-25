"""Host-owned lifecycle for shared tool checkouts and per-agent edit clones.

The trusted operator supplies the checkout; agents never obtain direct ledger
credentials. A read lease covers normal use of one tool; maintenance/cleanup and
cloning require a write lease for that tool. These are cooperative leases, NOT
protection against an unbrokered Bash process writing the shared checkout.

Downloaded packages, built binaries and shared environments live OUTSIDE the
Git checkout, under the same tool's installation directory. Per-agent runtime
files, outputs and editable clones stay under the agent's /home directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import UUID

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.tools.workspace_concurrency import (
    AccessRequest,
    AccessResource,
    WorkspaceLeaseLedger,
)
from potbot.tools.workspace_supervision import WorkspaceLeaseGuard, admission_digest

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SLUG = re.compile(r"[a-z][a-z0-9-]{1,62}\Z")
_REPOSITORY = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?\Z")

# Files within the shared checkout are SOURCE, not the package cache or output
# store. Every cleanable path must be independently listed by the installer.
# Never include the .git directory, agent files, lockfiles, or environment dirs.
FORBIDDEN_CLEAN_PARTS = frozenset({".git", ".venv", "node_modules", "models", "environment",
                                   "static", "downloads", "packages", "assets"})
ArtifactClass = Literal[
    "source", "shared_environment", "static_binary", "download_archive",
    "model", "per_agent_runtime", "run_output", "ephemeral_cache",
]


@dataclass(frozen=True, slots=True)
class ArtifactPolicy:
    classification: ArtifactClass
    location: str
    reuse: str
    lock_scope: str


SHARED_ARTIFACT_POLICIES: tuple[ArtifactPolicy, ...] = (
    ArtifactPolicy("source", "/usr/local/tools/<tool>/checkout", "read-many; clean before edit", "tool"),
    ArtifactPolicy("shared_environment", "/usr/local/tools/<tool>/environment", "pinned ABI and lockfile", "tool-install"),
    ArtifactPolicy("static_binary", "/usr/local/tools/<tool>/static", "hash + architecture + build receipt", "tool-install"),
    ArtifactPolicy("download_archive", "/usr/local/tools/<tool>/downloads", "content-addressed package", "tool-install"),
    ArtifactPolicy("model", "/var/ai/models or /opt/potbot/models", "operator verified exact model", "model-store"),
    ArtifactPolicy("per_agent_runtime", "/home/<agent>/.local", "not reused across agents", "agent-home"),
    ArtifactPolicy("run_output", "/home/<agent>/runs/<id>", "output belongs to that invocation", "run"),
    ArtifactPolicy("ephemeral_cache", "/home/<agent>/.cache", "safe to regenerate", "agent-home"),
)


def _name(value: str) -> str:
    if not isinstance(value, str) or not _SLUG.fullmatch(value):
        raise InvalidInputError("tool names must be lowercase canonical slugs")
    return value


def _storage_relative(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 180
            or "\\" in value or "\x00" in value):
        raise InvalidInputError("invalid tool-owned relative path")
    p = PurePosixPath(value)
    if p.is_absolute() or any(part in {"", ".", ".."} or part.startswith(".git")
                              for part in value.split("/")):
        raise ScopeDeniedError("tool store path must be canonical and git-private")
    return value


def _relative(value: str) -> str:
    _storage_relative(value)
    if any(part in FORBIDDEN_CLEAN_PARTS for part in PurePosixPath(value).parts):
        raise ScopeDeniedError("cleanup may only remove declared ephemeral checkout paths")
    return value


@dataclass(frozen=True, slots=True)
class SharedToolDefinition:
    """Operator-approved, source-pinned tool installation contract."""

    slug: str
    repository: str
    commit: str
    cleanup_paths: tuple[str, ...]
    lockfiles: tuple[tuple[str, str], ...] = ()
    model_downloads: bool = False
    reusable_artifacts: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _name(self.slug)
        if not isinstance(self.repository, str) or not _REPOSITORY.fullmatch(self.repository):
            raise InvalidInputError("shared tool requires a canonical GitHub source URL")
        if not isinstance(self.commit, str) or not _SHA.fullmatch(self.commit):
            raise InvalidInputError("shared tool requires a pinned 40-digit source revision")
        if not isinstance(self.cleanup_paths, tuple) or not self.cleanup_paths or len(self.cleanup_paths) > 64:
            raise InvalidInputError("every tool requires a bounded clean manifest")
        for item in self.cleanup_paths:
            _relative(item)
        if len(set(self.cleanup_paths)) != len(self.cleanup_paths):
            raise InvalidInputError("duplicate cleanup path")
        for index, left in enumerate(self.cleanup_paths):
            if any(left.startswith(right + "/") or right.startswith(left + "/")
                   for right in self.cleanup_paths[index + 1:]):
                raise InvalidInputError("nested cleanup paths are ambiguous")
        if not isinstance(self.lockfiles, tuple) or len(self.lockfiles) > 24:
            raise InvalidInputError("too many lockfile declarations")
        seen: set[str] = set()
        for path, digest in self.lockfiles:
            _relative(path)
            if path in seen or not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                raise InvalidInputError("lockfile must have unique exact SHA-256")
            seen.add(path)
        if not isinstance(self.reusable_artifacts, tuple) or len(self.reusable_artifacts) > 48:
            raise InvalidInputError("too many reusable artifact declarations")
        artifact_seen: set[str] = set()
        for path, digest in self.reusable_artifacts:
            if (not isinstance(path, str) or not path.startswith(
                ("static/", "environment/", "downloads/")
            ) or path in artifact_seen or not isinstance(digest, str)
                    or not _DIGEST.fullmatch(digest)):
                raise InvalidInputError("reusable artifact requires an exact path and hash")
            _storage_relative(path)
            artifact_seen.add(path)
        if type(self.model_downloads) is not bool:
            raise InvalidInputError("model download classification must be boolean")

    def to_dict(self) -> dict[str, object]:
        return {"slug": self.slug, "repository": self.repository, "commit": self.commit,
                "cleanup_paths": list(self.cleanup_paths), "lockfiles": list(self.lockfiles),
                "model_downloads": self.model_downloads,
                "reusable_artifacts": list(self.reusable_artifacts)}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> SharedToolDefinition:
        return cls(str(data["slug"]), str(data["repository"]), str(data["commit"]),
                   tuple(data["cleanup_paths"]),
                   tuple((str(a), str(b)) for a, b in data.get("lockfiles", [])),
                   data.get("model_downloads", False),
                   tuple((str(a), str(b)) for a, b in data.get("reusable_artifacts", [])))


@dataclass(frozen=True, slots=True)
class ToolCleanReceipt:
    slug: str
    commit: str
    removed: tuple[str, ...]
    source_clean: bool
    static_artifacts_preserved: bool


class SharedToolStore:
    """Host-only operator entrypoint; never a sandbox-exposed general git tool."""

    def __init__(
        self, *, ledger: WorkspaceLeaseLedger, shared_root: Path,
        authorize: Callable[[UUID, UUID], bool], git_executable: str = "git",
    ) -> None:
        if not callable(authorize):
            raise ScopeDeniedError("shared tool store requires host authorization")
        if shared_root.is_symlink() or not shared_root.is_dir():
            raise ScopeDeniedError("shared tool store must be a pre-provisioned real directory")
        root = shared_root.resolve(strict=True)
        if root == ledger.home or root.is_relative_to(ledger.home) or ledger.home.is_relative_to(root):
            raise ScopeDeniedError("shared tools must be a separate persistent mount")
        self.root, self.ledger, self.authorize = root, ledger, authorize
        self.git = git_executable
        with ledger._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS trusted_shared_tools (
                workspace_id TEXT NOT NULL, slug TEXT NOT NULL,
                definition_json TEXT NOT NULL, PRIMARY KEY(workspace_id,slug)
            )""")

    def _require(self, workspace_id: UUID, agent_id: UUID) -> str:
        if self.authorize(workspace_id, agent_id) is not True:
            raise ScopeDeniedError("agent is not allowed to use workspace tools")
        pinned = self.ledger.image_status(workspace_id)
        if pinned is None:
            raise StateMismatchError("shared tool requires pinned workspace image")
        return str(pinned["digest"])

    def _paths(self, slug: str) -> tuple[Path, Path]:
        name = _name(slug)
        parent = self.root / name
        if parent.is_symlink() or not parent.is_dir() or parent.parent != self.root:
            raise StateMismatchError("shared tool was not provisioned")
        checkout = parent / "checkout"
        if (checkout.is_symlink() or not checkout.is_dir()
                or (checkout / ".git").is_symlink()
                or not (checkout / ".git").is_dir()):
            raise StateMismatchError("shared tool checkout is unavailable")
        return parent, checkout

    def _git(self, cwd: Path, *args: str) -> str:
        """Read-only git introspection; no hooks, repo scripts, fetch or filters."""
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C", "LC_ALL": "C",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
        argv = [self.git, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                "-c", "submodule.recurse=false", "-c", "protocol.allow=never", *args]
        completed = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                                   timeout=20, check=False)
        if completed.returncode or len(completed.stdout) > 1_000_000:
            raise StateMismatchError("shared tool Git readback failed")
        return completed.stdout.decode("utf-8", errors="strict").strip()

    def _verify(self, definition: SharedToolDefinition, *, require_clean: bool = True) -> Path:
        _, checkout = self._paths(definition.slug)
        if self._git(checkout, "rev-parse", "--verify", "HEAD") != definition.commit:
            raise StateMismatchError("shared tool source revision differs from operator pin")
        if Path(self._git(checkout, "rev-parse", "--show-toplevel")).resolve() != checkout:
            raise StateMismatchError("shared checkout Git root differs from installed tool")
        if self._git(checkout, "remote", "get-url", "origin") != definition.repository:
            raise StateMismatchError("shared tool remote origin differs from operator pin")
        parent = checkout.parent
        for relative, digest in definition.reusable_artifacts:
            artifact = parent / relative
            if artifact.is_symlink() or not artifact.is_file():
                raise StateMismatchError("reusable tool artifact is missing or redirected")
            value = hashlib.sha256()
            with artifact.open("rb") as reader:
                while part := reader.read(1024 * 1024):
                    value.update(part)
            if value.hexdigest() != digest:
                raise StateMismatchError("reusable tool artifact digest changed")
        for relative, digest in definition.lockfiles:
            path = checkout / relative
            if (path.is_symlink() or not path.is_file()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
                raise StateMismatchError("tool dependency lockfile changed")
        if require_clean and self._git(checkout, "status", "--porcelain=v1", "--untracked-files=all"):
            raise StateMismatchError("shared tool has uncommitted or generated source changes")
        return checkout

    def register(
        self, *, workspace_id: UUID, agent_id: UUID, definition: SharedToolDefinition,
        idempotency_key: str, install_probe: dict[str, object],
    ) -> None:
        """Operator approves an existing checkout after the mandatory install gate."""
        review_manifest({"schema": "potbot-shared-tool-preflight/v1",
                         "definition": definition.to_dict(), "probe": install_probe})
        image = self._require(workspace_id, agent_id)
        request = AccessRequest(workspace_id, agent_id, "tool.register", idempotency_key,
                                "write", (AccessResource("shared_tool", definition.slug, "write"),), image)
        lease = self.ledger.claim(request, replay_existing=False)
        try:
            checkout = self._verify(definition)
            # An unknown ignored residue is not acceptable at registration.
            if self._git(checkout, "clean", "-ndx"):
                raise StateMismatchError("shared tool requires a clean source tree and external caches")
            with self.ledger._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute(
                    "SELECT definition_json FROM trusted_shared_tools WHERE workspace_id=? AND slug=?",
                    (str(workspace_id), definition.slug),
                ).fetchone()
                serialized = json.dumps(definition.to_dict(), sort_keys=True)
                if existing and existing["definition_json"] != serialized:
                    raise StateMismatchError("registered tool differs; explicit upgrade required")
                db.execute("INSERT OR IGNORE INTO trusted_shared_tools VALUES (?,?,?)",
                           (str(workspace_id), definition.slug, serialized))
            self.ledger.finish(lease, result_sha256=hashlib.sha256(serialized.encode()).hexdigest())
        except BaseException:
            self.ledger.finish(lease, result_sha256=admission_digest("tool-register-refused"))
            raise

    def definition(self, workspace_id: UUID, slug: str) -> SharedToolDefinition:
        with self.ledger._connect() as db:
            # When S9 is installed, an interrupted directory exchange must
            # block every brokered use until an operator has reconciled it.
            upgrade_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='trusted_tool_upgrades'"
            ).fetchone()
            if upgrade_table is not None:
                unsettled = db.execute(
                    "SELECT 1 FROM trusted_tool_upgrades WHERE workspace_id=? AND slug=? "
                    "AND state IN ('switching','rolling_back','indeterminate') LIMIT 1",
                    (str(workspace_id), _name(slug)),
                ).fetchone()
                if unsettled is not None:
                    raise StateMismatchError("Shared tool upgrade requires operator reconciliation")
            item = db.execute(
                "SELECT definition_json FROM trusted_shared_tools WHERE workspace_id=? AND slug=?",
                (str(workspace_id), _name(slug)),
            ).fetchone()
        if item is None:
            raise StateMismatchError("tool not installed or not approved for this workspace")
        return SharedToolDefinition.from_dict(json.loads(item["definition_json"]))

    @contextmanager
    def use(self, *, workspace_id: UUID, agent_id: UUID, slug: str,
            idempotency_key: str) -> Iterator[Path]:
        """Lease remains active until the caller confirms the supervised run ended."""
        image = self._require(workspace_id, agent_id)
        definition = self.definition(workspace_id, slug)
        guard = WorkspaceLeaseGuard(self.ledger, AccessRequest(
            workspace_id, agent_id, "tool.use", idempotency_key, "read",
            (AccessResource("shared_tool", slug, "read"),), image,
        ))
        guard.start()
        uncertain = False
        try:
            checkout = self._verify(definition)
            yield checkout
            guard.ensure_live()
            self._verify(definition)
            if self._git(checkout, "clean", "-ndx"):
                raise StateMismatchError("normal tool execution wrote inside shared checkout")
        except BaseException:
            uncertain = True
            raise
        finally:
            if uncertain:
                # The caller may have launched a command, so it MUST confirm
                # termination before marking success. Preserve the conflict.
                guard.finish(evidence_sha256=admission_digest("tool-use-unknown"), uncertain=True)
            else:
                guard.finish(evidence_sha256=admission_digest("tool-use-ended"))

    @staticmethod
    def _remove_declared(checkout: Path, relative: str) -> bool:
        """FD-relative, no-symlink removal of one explicitly declared output.

        This does not defeat an *unbrokered* malicious writer racing with the
        operation; the host must separately enforce mediation for hostile code.
        """
        parts = PurePosixPath(_relative(relative)).parts
        fd = os.open(checkout, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=fd)
                except FileNotFoundError:
                    return False
                os.close(fd)
                fd = child
            try:
                info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            if stat.S_ISLNK(info.st_mode):
                raise ScopeDeniedError("cleanup path contains a symbolic link")
            if stat.S_ISDIR(info.st_mode):
                if not shutil.rmtree.avoids_symlink_attacks:
                    raise ScopeDeniedError("fd-protected recursive cleanup is unavailable")
                shutil.rmtree(parts[-1], dir_fd=fd)
            elif stat.S_ISREG(info.st_mode):
                os.unlink(parts[-1], dir_fd=fd)
            else:
                raise ScopeDeniedError("cleanup refuses device, FIFO or socket")
            os.fsync(fd)
            return True
        finally:
            os.close(fd)

    def _clean_tree(self, definition: SharedToolDefinition) -> tuple[str, ...]:
        checkout = self._verify(definition, require_clean=False)
        # A manifest must never authorize deleting a tracked Git source path,
        # even when the caller misclassifies it as disposable output.
        for relative in definition.cleanup_paths:
            if self._git(checkout, "ls-files", "--cached", "--", relative):
                raise ScopeDeniedError("cleanup manifest overlaps tracked source files")
        status = self._git(checkout, "status", "--porcelain=v1", "--untracked-files=all")
        for row in status.splitlines():
            if not row.startswith("?? ") or not any(
                row[3:] == path or row[3:].startswith(path + "/")
                for path in definition.cleanup_paths
            ):
                raise StateMismatchError("source contains changes outside declared output paths")
        unknown = self._git(checkout, "clean", "-ndx")
        for row in unknown.splitlines():
            if not row.startswith("Would remove ") or not any(
                row.removeprefix("Would remove ").rstrip("/") == path
                or row.removeprefix("Would remove ").startswith(path + "/")
                for path in definition.cleanup_paths
            ):
                raise StateMismatchError("unclassified ignored output requires review")
        removed: list[str] = []
        for relative in definition.cleanup_paths:
            if self._remove_declared(checkout, relative):
                removed.append(relative)
        self._verify(definition)
        if self._git(checkout, "clean", "-ndx"):
            raise StateMismatchError("shared checkout still has ignored generated residue")
        return tuple(removed)

    def clean_for_edit(
        self, *, workspace_id: UUID, agent_id: UUID, slug: str, idempotency_key: str,
    ) -> ToolCleanReceipt:
        """Remove only registered run outputs. No git reset, checkout or -fdx."""
        image = self._require(workspace_id, agent_id)
        definition = self.definition(workspace_id, slug)
        guard = WorkspaceLeaseGuard(self.ledger, AccessRequest(
            workspace_id, agent_id, "tool.clean", idempotency_key, "write",
            (AccessResource("shared_tool", slug, "write"),), image,
        ))
        guard.start()
        # A clean may have changed files even when it raises. Any uncertainty
        # stays blocking; operator reconciles rather than retrying blindly.
        try:
            removed = self._clean_tree(definition)
            guard.ensure_live()
            guard.finish(evidence_sha256=admission_digest("clean:" + definition.commit))
            return ToolCleanReceipt(slug, definition.commit, removed, True, True)
        except BaseException:
            guard.finish(evidence_sha256=admission_digest("tool-clean-unknown"), uncertain=True)
            raise

    def edit_checkout_plan(self, *, workspace_id: UUID, agent_id: UUID, slug: str,
                           agent_home: Path, idempotency_key: str) -> dict[str, str]:
        """A safe, explicit clone command plan; execution needs a sandboxed Git Cog.

        An agent's edit clone is *never* linked to the shared checkout's .git
        object store. An authorized executor must first call clean_for_edit,
        then run this plan under a new read lease and verify the new HEAD.
        """
        self._require(workspace_id, agent_id)
        definition = self.definition(workspace_id, slug)
        root = self.ledger.home
        home = agent_home.resolve(strict=True)
        if home.is_symlink() or home == root or not home.is_relative_to(root):
            raise ScopeDeniedError("agent clone destination must be a registered agent home")
        projects = home / "projects"
        if projects.is_symlink() or not projects.is_dir():
            raise StateMismatchError("agent projects directory must exist")
        target = projects / slug
        if target.exists() or target.is_symlink():
            raise StateMismatchError("agent already has a checkout at this destination")
        self._verify(definition)
        # Use --no-local and no alternates to avoid sharing mutable objects.
        return {
            "source": f"/usr/local/tools/{slug}/checkout",
            "destination": f"/home/{home.name}/projects/{slug}",
            "expected_commit": definition.commit,
            "git_command": "clone --no-local --no-hardlinks --no-checkout -- "
                           f"/usr/local/tools/{slug}/checkout /home/{home.name}/projects/{slug}",
            "note": "Use only via the supervised Git tool, with hooks/filters disabled, "
                    "then checkout expected commit and verify no shared git alternates",
            "idempotency_key": idempotency_key,
        }

    def clone_for_edit(
        self, *, workspace_id: UUID, agent_id: UUID, slug: str,
        agent_home: Path, idempotency_key: str,
        sandboxed_clone: Callable[[dict[str, str]], None],
    ) -> dict[str, str]:
        """Clean and clone under ONE exclusive lease with caller-supplied sandbox runner.

        ``sandboxed_clone`` must use the admitted Git Cog/ProcessSupervisor, not
        host subprocess.run. The operator checks its output and pinned HEAD.
        Do not automatically retry an interrupted clone into an uncertain path.
        """
        if not callable(sandboxed_clone):
            raise ScopeDeniedError("agent clone requires an admitted isolated Git executor")
        image = self._require(workspace_id, agent_id)
        definition = self.definition(workspace_id, slug)
        guard = WorkspaceLeaseGuard(self.ledger, AccessRequest(
            workspace_id, agent_id, "tool.clone_for_edit", idempotency_key, "write",
            (AccessResource("shared_tool", slug, "write"),), image,
        ))
        guard.start()
        try:
            self._clean_tree(definition)
            plan = self.edit_checkout_plan(
                workspace_id=workspace_id, agent_id=agent_id, slug=slug,
                agent_home=agent_home, idempotency_key=idempotency_key,
            )
            target = agent_home.resolve(strict=True) / "projects" / slug
            guard.ensure_live()
            sandboxed_clone(plan)
            if target.is_symlink() or not target.is_dir() or (target / ".git").is_symlink():
                raise StateMismatchError("agent clone was not created as an independent directory")
            if self._git(target, "rev-parse", "--verify", "HEAD") != definition.commit:
                raise StateMismatchError("agent clone does not match the pinned source revision")
            if Path(self._git(target, "rev-parse", "--show-toplevel")).resolve() != target:
                raise StateMismatchError("agent edit checkout lacks an independent Git root")
            alternates = target / ".git/objects/info/alternates"
            if alternates.exists() or alternates.is_symlink():
                raise StateMismatchError("agent clone shares mutable Git object storage")
            guard.ensure_live()
            guard.finish(evidence_sha256=admission_digest("clone:" + definition.commit))
            return plan
        except BaseException:
            guard.finish(evidence_sha256=admission_digest("agent-clone-uncertain"), uncertain=True)
            raise


def tool_process_environment(slug: str, agent_name: str, *, installing: bool = False) -> dict[str, str]:
    """Per-agent execution state; only admitted installs write package caches.

    Model frameworks retain /var/ai download routes from model_environment().
    Tool-specific package archives are outside the clean checkout and reusable
    across agents after the installer has finalized them under an exclusive
    tool lease. This does not install packages or create directories.
    """
    name = _name(slug)
    if not isinstance(agent_name, str) or not re.fullmatch(r"[a-z0-9_-]{2,64}", agent_name):
        raise InvalidInputError("tool runtime needs an enrolled agent-home name")
    guest_home = f"/home/{agent_name}"
    tool = f"/usr/local/tools/{name}"
    cache = f"{tool}/downloads" if installing else f"{guest_home}/.cache/tools/{name}"
    from potbot.tools.model_cache_routes import model_environment

    return {
        **model_environment(),
        "HOME": guest_home,
        "POTBOT_SHARED_TOOLS": "/usr/local/tools",
        "POTBOT_TOOL_CHECKOUT": f"{tool}/checkout",
        "POTBOT_TOOL_ENVIRONMENT": f"{tool}/environment",
        "VIRTUAL_ENV": f"{tool}/environment",
        "PATH": f"{tool}/environment/bin:/usr/bin:/bin",
        "TMPDIR": f"{guest_home}/.tmp",
        "XDG_CACHE_HOME": f"{guest_home}/.cache",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_CACHE_DIR": f"{cache}/pip",
        "UV_CACHE_DIR": f"{cache}/uv",
        "npm_config_cache": f"{cache}/npm",
        "CARGO_HOME": f"{cache}/cargo",
    }


def review_manifest(payload: dict[str, object]) -> dict[str, object]:
    if not isinstance(payload, dict) or payload.get("schema") != "potbot-shared-tool-preflight/v1":
        raise InvalidInputError("expected versioned shared tool installation probe")
    declared = payload.get("definition")
    probe = payload.get("probe")
    if not isinstance(declared, dict) or not isinstance(probe, dict):
        raise InvalidInputError("tool preflight needs exact definition and instrumented probe")
    definition = SharedToolDefinition.from_dict(declared)
    if probe.get("network_used") is not False or probe.get("observed") is not True:
        raise ScopeDeniedError("installation probe must have observed offline paths")
    if probe.get("tool") != definition.slug:
        raise InvalidInputError("installation probe refers to a different tool")
    env = probe.get("environment")
    paths = probe.get("model_destinations")
    writes = probe.get("writes")
    if (not isinstance(env, dict) or not all(isinstance(a, str) and isinstance(b, str)
                                             for a, b in env.items())
            or not isinstance(paths, list) or not all(isinstance(p, str) for p in paths)
            or not isinstance(writes, list) or not all(isinstance(p, str) for p in writes)):
        raise InvalidInputError("tool preflight needs captured environment and write paths")
    from potbot.tools.model_cache_routes import ModelDownloadingTool, validate_tool_installation

    validate_tool_installation(
        ModelDownloadingTool(definition.slug, definition.model_downloads,
                             ("HF_HOME",) if definition.model_downloads else ()),
        env=env, destinations=tuple(paths), probe_performed=True,
    )
    # Package download caches are reusable inside the shared tool directory;
    # models MUST go to /var/ai, runtime outputs to /home. Never send tool
    # installation files to global model bytes, rootfs, or other checkouts.
    for path in writes:
        if (not path.startswith("/") or "\\" in path or "\x00" in path
                or "//" in path or any(piece in {".", "..", ""}
                                     for piece in path.split("/")[1:])):
            raise ScopeDeniedError("installation wrote an ambiguous or relative path")
        allowed = (
            path.startswith(f"/usr/local/tools/{definition.slug}/environment/"),
            path.startswith(f"/usr/local/tools/{definition.slug}/static/"),
            path.startswith(f"/usr/local/tools/{definition.slug}/downloads/"),
            path.startswith("/var/ai/cache/"),
            path.startswith("/var/ai/downloads/"),
            path.startswith("/home/"),
        )
        if not any(allowed):
            raise ScopeDeniedError("unclassified installation write needs a new storage contract")
        # Generic package managers must not silently leave builds in checkout;
        # a separate clean fixture validates any explicit source output paths.
    if probe.get("cleanup_test_passed") is not True:
        raise ScopeDeniedError("every shared tool needs a successful isolated clean acceptance")
    if probe.get("normal_use_test_passed") is not True:
        raise ScopeDeniedError("shared tool needs a working normal-use smoke")
    return {"tool": definition.slug, "revision": definition.commit,
            "model_writes_routed": True, "classified_writes": len(writes),
            "clean_declared": list(definition.cleanup_paths),
            "approved_to_install": False,
            "note": "Offline probe metadata only; operator must independently run all checks"}
