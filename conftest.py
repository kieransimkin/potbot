"""Isolated user-owned scratch paths for every pytest file and directory."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import pytest

from potbot.tools.temporary import TemporaryDirectory, create_directory

_FACTORY = pytest.StashKey["WorkspaceTempFactory"]()


class WorkspaceTempFactory:
    """The public getbasetemp/mktemp fixture contract without mkdir(0700) on Windows."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def getbasetemp(self) -> Path:
        return self.root

    def mktemp(self, basename: str, numbered: bool = True) -> Path:
        if (
            not isinstance(basename, str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,48}", basename)
            or basename in {".", ".."}
        ):
            raise ValueError("Temporary basename must be a bounded relative path component")
        if numbered:
            return Path(TemporaryDirectory(prefix=basename + "-", dir=self.root).name)
        path = self.root / basename
        path.mkdir(mode=0o777 if os.name == "nt" else 0o700)
        return path


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    supplied = config.option.basetemp
    configured = os.environ.get("POTBOT_TEMP_ROOT")
    checkout = Path(__file__).parent.resolve()
    checkout_key = hashlib.sha256(str(checkout).casefold().encode("utf-8")).hexdigest()[:6]
    # Linux AF_UNIX paths are capped at 108 bytes. GitHub's long checkout path
    # plus two private fixture generations can exceed that bound. Keep labels
    # short while retaining the complete 128-bit allocation nonce at each level. On CI only,
    # place the still-unique, mode-0700 scratch tree directly under /tmp.
    # Explicit --basetemp and POTBOT_TEMP_ROOT always take precedence.
    parent = (
        Path(supplied).absolute()
        if supplied
        else Path(configured).absolute()
        if configured
        else Path("/tmp")
        if os.name == "posix" and os.environ.get("GITHUB_ACTIONS") == "true"
        else Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        / "potbot"
        / "pytest-tmp"
        if os.name == "nt"
        else checkout / ".potbot" / "temporary"
    )
    parent = create_directory(parent)
    # Windows needs the checkout identity because all worktrees share the
    # project LocalAppData parent. Linux CI already uses /tmp and AF_UNIX has a
    # 108-byte path ceiling, so keep that unique prefix deliberately short.
    owner_prefix = f"potbot-pytest-{checkout_key}-" if os.name == "nt" else "potbot-pytest-"
    owner = TemporaryDirectory(prefix=owner_prefix, dir=parent)
    base = create_directory(Path(owner.name) / "run")
    config.option.basetemp = str(Path(supplied).absolute() if supplied else base)
    config.stash[_FACTORY] = WorkspaceTempFactory(base)
    # Cache writes must inherit the same tested parent ACL, including v/cache.
    cache = create_directory(Path(owner.name) / "cache" / "v" / "cache").parents[1]
    config._inicache["cache_dir"] = str(cache)
    config.add_cleanup(owner.cleanup)


@pytest.fixture(scope="session")
def tmp_path_factory(request: pytest.FixtureRequest) -> WorkspaceTempFactory:
    return request.config.stash[_FACTORY]


@pytest.fixture
def tmp_path(tmp_path_factory: WorkspaceTempFactory, request: pytest.FixtureRequest) -> Path:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", request.node.name)[:6] or "test"
    return tmp_path_factory.mktemp(name)
