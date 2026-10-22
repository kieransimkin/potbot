"""Durable workspace sandbox roots and filesystem confinement helpers.

A configured workspace owns exactly one canonical host directory. Potbot-controlled
repository copies, candidates, model assets, tools, caches, artifacts, receipts and
temporary files must stay below that directory. The selected execution backend is
still responsible for OS-level process isolation; path checks are an additional
fail-closed admission boundary, not a substitute for Bubblewrap/seccomp.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError

if TYPE_CHECKING:
    from potbot.persistence.database import Database

POLICY_VERSION = 1
_LAYOUT = (
    "repository",
    "candidates",
    "models",
    "tools",
    "dependencies",
    "environments",
    "builds",
    "tests",
    "artifacts",
    "receipts",
    "cache",
    "tmp",
    "logs",
)


def _linklike(path: Path) -> bool:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse)


def _canonical_root(value: str | os.PathLike[str] | Path, *, create: bool) -> Path:
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise InvalidInputError("Workspace sandbox root must be an explicit absolute path.")
    root = Path(os.path.abspath(root))
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not root.exists() or not root.is_dir():
        raise InvalidInputError("Workspace sandbox root must be an existing directory.")
    try:
        if _linklike(root):
            raise ScopeDeniedError("Workspace sandbox root cannot be a symlink or reparse point.")
    except OSError as error:
        raise ScopeDeniedError("Workspace sandbox root cannot be inspected safely.") from error
    resolved = root.resolve(strict=True)
    if resolved != root:
        raise ScopeDeniedError("Workspace sandbox root cannot traverse links or junctions.")
    if resolved.parent == resolved:
        raise ScopeDeniedError("A filesystem root cannot be used as a Potbot workspace sandbox.")
    return resolved


def root_key(root: Path) -> str:
    normalized = os.path.normcase(str(root)).encode("utf-8", "surrogatepass")
    return hashlib.sha256(normalized).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkspaceSandbox:
    root: Path
    policy_version: int = POLICY_VERSION

    @classmethod
    def prepare(cls, value: str | os.PathLike[str] | Path) -> WorkspaceSandbox:
        sandbox = cls(_canonical_root(value, create=True))
        sandbox.ensure_layout()
        return sandbox

    @classmethod
    def existing(cls, value: str | os.PathLike[str] | Path) -> WorkspaceSandbox:
        return cls(_canonical_root(value, create=False))

    @property
    def key(self) -> str:
        return root_key(self.root)

    def path(self, area: str) -> Path:
        if area not in _LAYOUT:
            raise InvalidInputError("Unknown workspace sandbox area.")
        return self.root / area

    def ensure_layout(self) -> None:
        for area in _LAYOUT:
            directory = self.root / area
            if directory.exists():
                if not directory.is_dir() or _linklike(directory):
                    raise ScopeDeniedError("Workspace sandbox layout contains an unsafe path.")
            else:
                directory.mkdir(mode=0o700)
            if directory.resolve(strict=True).parent != self.root:
                raise ScopeDeniedError("Workspace sandbox layout cannot traverse links.")

    def confine(
        self,
        value: str | os.PathLike[str] | Path,
        *,
        area: str | None = None,
        must_exist: bool = False,
    ) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise InvalidInputError("Sandbox-managed paths must be explicit absolute paths.")
        path = Path(os.path.abspath(path))
        boundary = self.root if area is None else self.path(area)
        try:
            resolved = path.resolve(strict=must_exist)
        except OSError as error:
            raise ScopeDeniedError("Sandbox-managed path cannot be resolved safely.") from error
        try:
            resolved.relative_to(boundary)
        except ValueError:
            label = area or "workspace"
            raise ScopeDeniedError(f"Path is outside the workspace {label} sandbox area.") from None
        # Existing link/junction endpoints are never accepted as privileged roots.
        if path.exists() and _linklike(path):
            raise ScopeDeniedError("Sandbox-managed paths cannot be links or reparse points.")
        return resolved

    def runtime_environment(self) -> dict[str, str]:
        self.ensure_layout()
        home = self.path("environments") / "home"
        tmp = self.path("tmp") / "process"
        cache = self.path("cache")
        for directory in (home, tmp, cache):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.confine(directory, must_exist=True)
        return {
            "HOME": str(home),
            "TMPDIR": str(tmp),
            "TMP": str(tmp),
            "TEMP": str(tmp),
            "XDG_CACHE_HOME": str(cache / "xdg"),
            "HF_HOME": str(cache / "huggingface"),
            "HUGGINGFACE_HUB_CACHE": str(cache / "huggingface" / "hub"),
            "TRANSFORMERS_CACHE": str(cache / "transformers"),
            "TORCH_HOME": str(cache / "torch"),
            "UV_CACHE_DIR": str(cache / "uv"),
            "PIP_CACHE_DIR": str(cache / "pip"),
            "CARGO_HOME": str(self.path("dependencies") / "cargo"),
            "npm_config_cache": str(cache / "npm"),
            "POTBOT_WORKSPACE_SANDBOX": str(self.root),
        }


def load_workspace_sandbox(database: Database, context: WorkspaceContext) -> WorkspaceSandbox:
    with database.transaction(context) as connection:
        row = connection.execute(
            "SELECT root_path,root_key,policy_version FROM workspace_sandboxes "
            "WHERE workspace_id=%s",
            (context.workspace_id,),
        ).fetchone()
    if row is None:
        raise ScopeDeniedError(
            "Workspace has no configured filesystem sandbox; configure it before local work."
        )
    if row["policy_version"] != POLICY_VERSION:
        raise ScopeDeniedError("Workspace sandbox policy version is not supported by this runtime.")
    sandbox = WorkspaceSandbox.existing(row["root_path"])
    if sandbox.key != row["root_key"]:
        raise ScopeDeniedError(
            "Workspace sandbox root identity no longer matches its registration."
        )
    sandbox.ensure_layout()
    return sandbox


def sandbox_record(sandbox: WorkspaceSandbox) -> dict[str, Any]:
    return {
        "root_path": str(sandbox.root),
        "root_key": sandbox.key,
        "policy_version": sandbox.policy_version,
        "areas": {name: str(sandbox.path(name)) for name in _LAYOUT},
    }
