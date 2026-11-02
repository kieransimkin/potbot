"""Inventory and filesystem faults; native UID execution is qualified upstream."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.syn_release import agent_runtime
from potbot.syn_release.contracts import canonical
from potbot.syn_release.store import SynReleaseStore, _write_new

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX runtime-view permissions")


@pytest.fixture
def admitted(tmp_path, monkeypatch):
    # Unit fixture permits the current test owner; genuine root/Agent UID
    # boundaries are measured by the separate native official-release lane.
    monkeypatch.setattr(agent_runtime, "_require_native_controller", lambda: None)
    store = SynReleaseStore(tmp_path / "packages")
    candidate = store.root / "installs/runtime-fixture"
    source = candidate / "venv"
    (source / "bin").mkdir(parents=True)
    (source / "bin/python").write_bytes(b"fixture executable")
    (source / "bin/python").chmod(0o500)
    (source / "module.py").write_bytes(b"fixture source bytes")
    (candidate / "policy.json").write_bytes(b"not part of the execution view")

    def seal():
        for parent, directories, files in os.walk(candidate):
            for name in directories + files:
                path = Path(parent) / name
                if not path.is_symlink():
                    path.chmod(0o500 if path.is_dir() or path.stat().st_mode & 0o111 else 0o400)
        candidate.chmod(0o500)
        raw = canonical(store._inventory(candidate))
        identity = hashlib.sha256(raw).hexdigest()
        inventories = store.root / "inventories"
        inventories.mkdir(mode=0o700, exist_ok=True)
        _write_new(inventories / identity, raw)
        return {"directory": candidate.name, "files_sha256": identity}

    return store, source, seal, tmp_path / "views"


def test_private_original_is_unchanged_and_only_pinned_runtime_bytes_are_shared(admitted):
    store, source, seal, cache = admitted
    receipt = seal()
    before = store._inventory(source.parent)
    view = agent_runtime.runtime_view(store, receipt, cache)
    assert (view / "module.py").read_bytes() == (source / "module.py").read_bytes()
    assert stat.S_IMODE((source / "module.py").stat().st_mode) == 0o400
    assert stat.S_IMODE((view / "module.py").stat().st_mode) == 0o444
    assert stat.S_IMODE((view / "bin/python").stat().st_mode) == 0o555
    assert not (view / "policy.json").exists()
    assert store._inventory(source.parent) == before
    assert agent_runtime.runtime_view(store, receipt, cache) == view


def test_changed_or_partial_execution_view_is_never_silently_repaired(admitted):
    store, _, seal, cache = admitted
    receipt = seal()
    view = agent_runtime.runtime_view(store, receipt, cache)
    path = view / "module.py"
    path.chmod(0o600)
    path.write_bytes(b"changed")
    with pytest.raises(StateMismatchError):
        agent_runtime.runtime_view(store, receipt, cache)
    assert path.read_bytes() == b"changed"


def test_execution_view_refuses_links_to_private_nonruntime_files(admitted):
    store, source, seal, cache = admitted
    (source / "private-link").symlink_to("../policy.json")
    with pytest.raises(StateMismatchError, match="external link"):
        agent_runtime.runtime_view(store, seal(), cache)
    assert not cache.exists()


def test_changed_admitted_inventory_fails_before_view_creation(admitted):
    store, _, seal, cache = admitted
    receipt = seal()
    (store.root / "inventories" / receipt["files_sha256"]).write_bytes(b"{}")
    with pytest.raises(StateMismatchError, match="inventory changed"):
        agent_runtime.runtime_view(store, receipt, cache)
    assert not cache.exists()


def test_runtime_views_require_privileged_controller(monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    with pytest.raises(ScopeDeniedError, match="privileged native"):
        agent_runtime._require_native_controller()
