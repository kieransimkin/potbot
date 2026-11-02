"""A checked read-only execution view of one private admitted Syn environment.

The sealed installer store remains private and unchanged. Restricted native
Agents share only public runtime bytes with normalized read/execute permissions.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path
from typing import Any

from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.tools.temporary import TemporaryDirectory

from .contracts import canonical, digest, document
from .store import SynReleaseStore, _directory, _write_new, private_read


def _require_native_controller() -> None:
    if os.name != "posix" or os.getuid() != 0:
        raise ScopeDeniedError("Syn runtime views require the privileged native controller")


def runtime_view(store: SynReleaseStore, receipt: dict[str, Any], cache: Path) -> Path:
    """Copy exact inventory-pinned bytes; refuse changed or partial prior views."""
    _require_native_controller()
    name = receipt["directory"]
    if not isinstance(name, str) or Path(name).name != name or not name.startswith("runtime-"):
        raise StateMismatchError("Invalid admitted Syn environment identity")
    candidate = store.root / "installs" / name
    source = candidate / "venv"
    if source.resolve(strict=True) != source or source.is_symlink():
        raise StateMismatchError("Syn runtime source is not the canonical admitted environment")
    identity = digest(receipt["files_sha256"])
    raw = private_read(store.root / "inventories" / identity, 16_777_216)
    if hashlib.sha256(raw).hexdigest() != identity:
        raise StateMismatchError("Syn runtime source inventory changed")
    inventory = document(raw, canonical_only=True)
    if canonical(store._inventory(candidate)) != raw:
        raise StateMismatchError("Syn runtime source differs from its admitted inventory")
    expected: dict[str, Any] = {}
    for key, value in inventory.items():
        if not key.startswith("venv/"):
            continue
        relative = key[len("venv/") :]
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise StateMismatchError("Escaping Syn runtime inventory path")
        if "link" in value:
            if Path(value["link"]).is_absolute() or not (source / relative).resolve(
                strict=True
            ).is_relative_to(source):
                raise StateMismatchError("Syn execution view cannot expose an external link")
            expected[relative] = value
        elif value.get("directory") is True:
            expected[relative] = {"directory": True, "mode": 0o555}
        else:
            expected[relative] = {
                **value,
                "mode": 0o555 if value["mode"] & 0o111 else 0o444,
            }
    if not expected or "bin/python" not in expected:
        raise StateMismatchError("Admitted Syn runtime is incomplete")
    key = hashlib.sha256(canonical(expected)).hexdigest()
    pin = {
        "profile": "potbot.syn-agent-runtime-view/1",
        "source_directory": str(candidate),
        "files_sha256": identity,
        "view_inventory_sha256": key,
    }
    _directory(cache, create=True)
    import fcntl

    fd = os.open(cache / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "rb") as lock:
        info = os.fstat(lock.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
        ):
            raise StateMismatchError("Unsafe Syn runtime-view lock")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise StateMismatchError("Another Syn runtime view is being admitted") from error
        target, record = cache / key, cache / (key + ".json")
        if target.exists() or record.exists() or target.is_symlink() or record.is_symlink():
            if (
                not target.is_dir()
                or target.is_symlink()
                or stat.S_IMODE(target.stat().st_mode) != 0o555
                or not record.is_file()
                or document(private_read(record), canonical_only=True) != pin
                or store._inventory(target) != expected
            ):
                raise StateMismatchError("Retained Syn execution view changed or is partial")
            return target
        with TemporaryDirectory(prefix="syn-runtime-view-", dir=cache) as temporary:
            staged = Path(temporary) / "runtime"
            shutil.copytree(source, staged, symlinks=True)
            for relative, value in expected.items():
                if "link" not in value:
                    (staged / relative).chmod(value["mode"])
            staged.chmod(0o555)
            if store._inventory(staged) != expected:
                raise StateMismatchError("Copied Syn execution bytes differ from admission")
            # Directory moves update '..'. Keep this transient write access
            # inside the private staging parent; no Agent is launched yet.
            staged.chmod(0o700)
            staged.rename(target)
            target.chmod(0o555)
            _write_new(record, canonical(pin))
        return target
