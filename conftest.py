"""Isolated project-local scratch paths for every pytest file and directory."""

from __future__ import annotations

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
    parent = (
        Path(supplied).absolute() if supplied else Path(__file__).parent / ".potbot" / "temporary"
    )
    owner = TemporaryDirectory(prefix="potbot-pytest-", dir=parent)
    base = create_directory(Path(owner.name) / "run")
    config.stash[_FACTORY] = WorkspaceTempFactory(base)
    # Cache writes must inherit the same tested parent ACL, including v/cache.
    cache = create_directory(Path(owner.name) / "cache" / "v" / "cache").parents[1]
    config.inicfg["cache_dir"] = str(cache)
    config.add_cleanup(owner.cleanup)


@pytest.fixture(scope="session")
def tmp_path_factory(request: pytest.FixtureRequest) -> WorkspaceTempFactory:
    return request.config.stash[_FACTORY]


@pytest.fixture
def tmp_path(tmp_path_factory: WorkspaceTempFactory, request: pytest.FixtureRequest) -> Path:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", request.node.name)[:32] or "test"
    return tmp_path_factory.mktemp(name)
