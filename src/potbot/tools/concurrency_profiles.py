"""Conservative, source-informed shared-resource claims for typed tool operations.

A footprint is an *admission contract*, not an OS access-control list. Untrusted
arbitrary shell processes must still take a workspace-exclusive lease; without
a mediated filesystem the guest could write outside its declared footprint.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from potbot.domain.errors import InvalidInputError
from potbot.tools.workspace_concurrency import AccessResource, Mode

OperationKind = Literal[
    "file_read", "file_edit", "file_create", "file_remove", "file_rename",
    "package_install", "package_lock_update", "model_install", "cache_update",
    "sqlite_transaction", "memory_update", "media_render", "build", "git_worktree",
    "github_file_read", "github_file_write", "unclassified_command",
]

# A read of an SQLite database normally goes through SQLite's own snapshot
# protocol, not through a direct read of -wal, -shm, or a copied db file.
_DATABASE_SUFFIXES = ("-wal", "-shm", "-journal")
_LOCK_NAMES = frozenset({
    "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock",
    "uv.lock", "poetry.lock", "pdm.lock", "pipfile.lock", "cargo.lock",
    "composer.lock", "bun.lock", "bun.lockb", "flake.lock", "pubspec.lock",
    "packages.lock.json", "gemfile.lock", "song-lock.json", "asset-lock.json",
})
_ENV_NAMES = frozenset({"node_modules", ".venv", "venv", ".tox", ".next", ".npm"})
_CACHE_NAMES = frozenset({
    ".cache", ".npm-cache", ".uv-cache", ".huggingface", "models",
    "model-cache", "motion-packs", "optional-motion-packs", "cache",
})
_BUILD_NAMES = frozenset({
    "build", "dist", "release", "release-output", "generated", "outputs",
    "results", "library", "temp", "obj", "logs", "builds", ".build",
})
_MEMORY_NAMES = frozenset({"memory", "memories", "embeddings", "vectors", "vector-index"})
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _path(value: str) -> str:
    return AccessResource("home", value, "read").name


def _sqlite_key(path: str) -> str:
    for suffix in _DATABASE_SUFFIXES:
        if path.endswith(suffix):
            return path[: -len(suffix)]
    return path


def classify_home_path(path: str) -> str:
    """Classify known high-contention DanceFlow/Potbot paths without writing."""
    canonical = _path(path)
    parts = tuple(x.casefold() for x in PurePosixPath(canonical).parts[2:])
    leaf = parts[-1] if parts else ""
    if leaf in _LOCK_NAMES:
        return "package-lock"
    if leaf.endswith((".sqlite", ".sqlite3", ".db", ".sqlite-wal", ".sqlite-shm", ".db-wal")) or any(
        leaf.endswith(suffix) for suffix in _DATABASE_SUFFIXES
    ):
        return "sqlite-store"
    if ".git" in parts:
        return "git-metadata"
    if any(x in _MEMORY_NAMES for x in parts) or leaf.endswith((".faiss", ".index")):
        return "memory-index"
    if any(x in _CACHE_NAMES for x in parts):
        return "shared-cache-or-model"
    if any(x in _ENV_NAMES for x in parts):
        return "package-environment"
    if any(x in _BUILD_NAMES for x in parts):
        return "build-output"
    if leaf.endswith((".part", ".partial", ".lock")):
        return "transient-write-lock"
    return "ordinary-file"


@dataclass(frozen=True, slots=True)
class Footprint:
    kind: OperationKind
    resources: tuple[AccessResource, ...]
    raw_shell_compatible: bool = False

    def __post_init__(self) -> None:
        if not self.resources or len(self.resources) > 32:
            raise InvalidInputError("footprint must contain 1-32 exact resources")
        if self.kind == "unclassified_command" and self.resources != (
            AccessResource("workspace", "*", "write"),
        ):
            raise InvalidInputError("unknown executable effects require exclusive workspace")


def file_footprint(
    kind: Literal["file_read", "file_edit", "file_create", "file_remove", "file_rename"],
    paths: tuple[str, ...],
) -> Footprint:
    """Typed file operations allow disjoint concurrent writes and shared reads.

    Directory-entry mutations must claim their parent as well as the file. This
    makes rename/create/delete on the same parent conservative while permitting
    independent edits under different directories. Callers MUST use openat2/
    dirfd + O_NOFOLLOW and revalidate inputs before commit.
    """
    if kind not in {"file_read", "file_edit", "file_create", "file_remove", "file_rename"}:
        raise InvalidInputError("unsupported file operation")
    if not paths or len(paths) > 12:
        raise InvalidInputError("file operation requires 1-12 paths")
    mode: Mode = "read" if kind == "file_read" else "write"
    resources: set[AccessResource] = set()
    for path in paths:
        canonical = _path(path)
        if canonical == "/home":
            raise InvalidInputError("typed file operation must name a concrete entry")
        if classify_home_path(canonical) == "sqlite-store":
            canonical = _sqlite_key(canonical)
        resources.add(AccessResource("home", canonical, mode))
        if kind in {"file_create", "file_remove", "file_rename"}:
            resources.add(AccessResource("home", str(PurePosixPath(canonical).parent), "write"))
    return Footprint(kind, tuple(sorted(resources, key=lambda r: (r.kind, r.name, r.mode))))


def package_footprint(
    root: str, *, manager: Literal["npm", "pnpm", "yarn", "uv", "pip", "cargo"],
    change_lock: bool = False, shared_cache: str | None = None,
) -> Footprint:
    """Installation writes environment but reads lockfile unless resolving it.

    A global cache has to use an *external* resource name; cache scope is host
    owned, never inferred from environment variables passed by an agent.
    """
    base = _path(root)
    mapping = {
        "npm": ("package-lock.json", "node_modules"),
        "pnpm": ("pnpm-lock.yaml", "node_modules"),
        "yarn": ("yarn.lock", "node_modules"),
        "uv": ("uv.lock", ".venv"),
        "pip": ("requirements.txt", ".venv"),
        "cargo": ("Cargo.lock", "target"),
    }
    if manager not in mapping:
        raise InvalidInputError("unrecognized package manager")
    lock, env = mapping[manager]
    resources: set[AccessResource] = {
        AccessResource("home", base + "/" + lock, "write" if change_lock else "read"),
        AccessResource("home", base + "/" + env, "write"),
    }
    if shared_cache is not None:
        resources.add(AccessResource("external", shared_cache, "write"))
    return Footprint("package_lock_update" if change_lock else "package_install",
                     tuple(sorted(resources, key=lambda r: (r.kind, r.name, r.mode))))


def model_artifact_footprint(
    location: str, *, digest: str, writer: bool, cache_scope: str | None = None,
) -> Footprint:
    """Independent model versions can download concurrently, identical ones cannot.

    The artifact must be staged to a unique .part file and atomically published
    only after hashing. A model's load/read uses a reader lease.
    """
    if not isinstance(digest, str) or not _SHA.fullmatch(digest):
        raise InvalidInputError("model artifact needs immutable SHA-256")
    root = _path(location)
    if root == "/home":
        raise InvalidInputError("model store needs a concrete path")
    resource = AccessResource("home", f"{root}/{digest}", "write" if writer else "read")
    resources = [resource]
    if cache_scope is not None:
        resources.append(AccessResource("external", cache_scope + "/" + digest,
                                        "write" if writer else "read"))
    return Footprint("model_install" if writer else "file_read", tuple(resources))


def sqlite_footprint(db_path: str, *, write: bool) -> Footprint:
    """Coordinate SQLite schema/backup writers on the same logical DB family."""
    root = _sqlite_key(_path(db_path))
    return Footprint("sqlite_transaction" if write else "file_read", (
        AccessResource("home", root, "write" if write else "read"),
    ))


def directory_footprint(path: str, *, kind: Literal["cache_update", "memory_update", "media_render", "build"] = "cache_update") -> Footprint:
    if kind not in {"cache_update", "memory_update", "media_render", "build"}:
        raise InvalidInputError("unknown directory operation")
    canonical = _path(path)
    if canonical == "/home":
        raise InvalidInputError("directory operation must specify the actual target")
    return Footprint(kind, (AccessResource("home", canonical, "write"),))


def unknown_shell_footprint() -> Footprint:
    """An unrestricted Bash process can touch any shared /home path."""
    return Footprint("unclassified_command", (AccessResource("workspace", "*", "write"),))
