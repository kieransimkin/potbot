"""Pinned, offline agent-home skeleton installation.

No download, shell execution or model action occurs during provisioning.
The shipped contents are trusted image inputs; every copy uses no-follow,
create-only file descriptors and fails closed on a changed template.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from pathlib import Path
from typing import Final

from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.tools.model_cache_routes import MODEL_HOME_ALIASES

SKELETON_VERSION: Final = "potbot-agent-home/v4"
SKELETON_SOURCE: Final = "kieransimkin/update-motd.d-scripts"
SKELETON_SOURCE_COMMIT: Final = "2485c90143dae3dd336769e7ab68ebcca781fa8a"

# Hash the *shipped* bytes; the three upstream MOTD scripts are vendor-copied
# from the exact Git commit above. Updating any file requires a reviewed manifest.
_FILES: Final[dict[str, tuple[int, str]]] = {
    ".bashrc": (0o644, "60b95c0c1b22384d956022e21b5ee6debe536c59c8a0b6bdd9fe28e1b1dc4d8f"),
    ".profile": (0o644, "c6132e093cd3b61a8c688025e394cb2f48f4bc4e724477eeabbe73f2f8ad18e2"),
    ".config/potbot/banner.color": (0o644, "b0578f4319d0d3b8e6dab5b958c43e321680771caf9cf29e5e5755fe442327f0"),
    ".config/potbot/banner.font": (0o644, "161bd156bb16ca3083a08787e3a1aec94ae8418afb99b662a3c3c6c47811f324"),
    ".config/potbot/README": (0o644, "0ef58f64bb9af8623a7e826a518b3e42e591e3825e72b704e92d125efefe6ce9"),
    ".local/bin/potbot-welcome": (0o755, "0076d6ecce9e70a1540d4af04db77e0f9f3cc6d1c4e3c3f24d4ba4326b9bc19d"),
    ".local/share/potbot/motd.d/10-figbanner": (
        0o755, "7e29fd58722f58439126ef78eae6a5e91d0e5938e0ff2e5d6f20c27691803d2a",
    ),
    ".local/share/potbot/motd.d/20-fortune": (
        0o755, "76c2bf65334149778baa9d07ceb707609d80be59d22de7ac29d125a64d000c1a",
    ),
    ".local/share/potbot/motd.d/99-insult": (
        0o755, "787a4caccbf87595d2b35a426ddfb98a6a74d65fbd0fc5b6f3fb85927d847732",
    ),
}
_TEMPLATE_ROOT: Final = Path(__file__).with_name("agent_skeleton")


def skeleton_digest() -> str:
    """Stable identity of the exact shipped file names, modes, bytes and source."""
    descriptor = {
        "version": SKELETON_VERSION,
        "source": SKELETON_SOURCE,
        "source_commit": SKELETON_SOURCE_COMMIT,
        "files": [(name, mode, digest) for name, (mode, digest) in sorted(_FILES.items())],
        "model_aliases": sorted(MODEL_HOME_ALIASES.items()),
        "workdirs": [".tmp", "projects"],
        "shared_tool_alias": ("Tools", "/usr/local/tools"),
        "dynamic_workspace_banner": "workspace-name/v1",
    }
    return hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()


def _read_templates() -> dict[str, bytes]:
    if _TEMPLATE_ROOT.is_symlink() or not _TEMPLATE_ROOT.is_dir():
        raise StateMismatchError("agent skeleton directory is unavailable")
    result: dict[str, bytes] = {}
    total = 0
    for name, (_, expected) in _FILES.items():
        source = _TEMPLATE_ROOT / name
        if any(parent.is_symlink() for parent in (source, *source.parents)
               if parent == _TEMPLATE_ROOT or _TEMPLATE_ROOT in parent.parents):
            raise StateMismatchError("agent skeleton includes a symbolic link")
        details = source.stat(follow_symlinks=False)
        if not stat.S_ISREG(details.st_mode) or details.st_size > 65536:
            raise StateMismatchError("agent skeleton has an invalid file type or size")
        data = source.read_bytes()
        total += len(data)
        if total > 262144 or hashlib.sha256(data).hexdigest() != expected:
            raise StateMismatchError("agent skeleton contents do not match pinned hashes")
        result[name] = data
    return result


def _make_directory(root_fd: int, relative: str) -> None:
    fd = os.dup(root_fd)
    try:
        for piece in relative.split("/"):
            if piece in {"", ".", ".."}:
                raise InvalidInputError("invalid skeleton directory")
            try:
                os.mkdir(piece, 0o775, dir_fd=fd)
            except FileExistsError:
                pass
            next_fd = os.open(piece, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                              dir_fd=fd)
            os.close(fd)
            fd = next_fd
            os.fchmod(fd, 0o775)
    finally:
        os.close(fd)


def _create_file(root_fd: int, name: str, contents: bytes, mode: int) -> None:
    parts = name.split("/")
    fd = os.dup(root_fd)
    try:
        for piece in parts[:-1]:
            next_fd = os.open(piece, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                              dir_fd=fd)
            os.close(fd)
            fd = next_fd
        output = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW |
                         os.O_CLOEXEC, mode, dir_fd=fd)
        try:
            os.fchmod(output, mode)
            with os.fdopen(output, "wb", closefd=False) as writer:
                writer.write(contents)
                writer.flush()
            os.fsync(output)
        finally:
            os.close(output)
    finally:
        os.close(fd)


def _create_model_alias(root_fd: int, name: str, target: str) -> None:
    """Create only within a newly allocated agent home; never follow a parent link."""
    parts = name.split("/")
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                              os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        os.symlink(target, parts[-1], dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(fd)


def workspace_banner_label(workspace_name: str) -> str:
    """Derive an ASCII FIGlet label; presentation never blocks valid workspace names."""
    if not isinstance(workspace_name, str) or not workspace_name.strip():
        raise InvalidInputError("workspace name is required for agent skeleton")
    decomposed = unicodedata.normalize("NFKD", workspace_name)
    basic = decomposed.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9 ._-]+", " ", basic)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._-")
    return (cleaned[:48].rstrip(" ._-") or "Workspace")


def install_agent_skeleton(agent_dir_fd: int, *, workspace_name: str) -> str:
    """Populate a freshly created, empty agent directory; never overwrite files.

    Called under an exclusive workspace home lease by AgentHomeRegistry. A
    partial filesystem write is *not* automatically rolled back or replayed:
    the caller must mark the lease indeterminate for explicit reconciliation.
    """
    if type(agent_dir_fd) is not int or agent_dir_fd < 0:
        raise InvalidInputError("agent skeleton requires a trusted directory descriptor")
    banner_label = workspace_banner_label(workspace_name)
    files = _read_templates()  # Preflight *before* the first write.
    directories = {part for name in (*files, *MODEL_HOME_ALIASES) for part in _parents(name)}
    for name in sorted(directories | {".tmp", "projects"},
                       key=lambda value: (value.count("/"), value)):
        _make_directory(agent_dir_fd, name)
    for name, contents in files.items():
        _create_file(agent_dir_fd, name, contents, _FILES[name][0])
    _create_file(agent_dir_fd, ".config/potbot/workspace-name",
                 (banner_label + "\n").encode("utf-8"), 0o644)
    for alias, target in sorted(MODEL_HOME_ALIASES.items()):
        _create_model_alias(agent_dir_fd, alias, target)
    _create_model_alias(agent_dir_fd, "Tools", "/usr/local/tools")
    receipt = {
        "schema": SKELETON_VERSION,
        "template_sha256": skeleton_digest(),
        "source": SKELETON_SOURCE,
        "source_commit": SKELETON_SOURCE_COMMIT,
        "original_scripts": ["10-figbanner", "20-fortune", "99-insult"],
        "model_cache_aliases": MODEL_HOME_ALIASES,
        "shared_tool_alias": "/usr/local/tools",
        "workspace_display_name": banner_label,
        "banner_default": "workspace-until-named;agent-name-when-named",
    }
    _create_file(agent_dir_fd, ".config/potbot/skeleton.json",
                 (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode(), 0o644)
    os.fsync(agent_dir_fd)
    return receipt["template_sha256"]


def _parents(name: str) -> tuple[str, ...]:
    parts = name.split("/")[:-1]
    return tuple("/".join(parts[:n]) for n in range(1, len(parts) + 1))
