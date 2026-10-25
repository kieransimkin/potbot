"""Host operator manifest rejects ambiguous, redirected and guest-controlled paths."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.tools.operator_cli import parse_manifest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Linux host manifest qualification")


def fixture(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    paths = {name: tmp_path / name for name in (
        "rootfs", "home", "model_staging", "shared_models", "shared_tools",
        "ai_config_dir", "ai_history_dir",
    )}
    for path in paths.values():
        path.mkdir()
    paths["controller_ledger"] = tmp_path / "ledger.sqlite"
    paths["controller_ledger"].write_bytes(b"")
    metadata = {
        "workspace_id": str(uuid4()), "image_digest": "a" * 64,
        "qualification_sha256": "b" * 64,
        **{name: str(path) for name, path in paths.items()},
    }
    manifest = tmp_path / "operator.json"
    manifest.write_text(json.dumps(metadata))
    manifest.chmod(0o600)
    return manifest, metadata


def test_valid_manifest_has_exact_paths_and_runtime(tmp_path: Path) -> None:
    file, data = fixture(tmp_path)
    paths = parse_manifest(file)
    assert paths.workspace_id.hex == data["workspace_id"].replace("-", "")
    runtime = paths.runtime()
    assert runtime.ai_config_dir == Path(data["ai_config_dir"])
    assert runtime.ai_history_dir == Path(data["ai_history_dir"])


def test_invalid_manifest_injection_and_traversal(tmp_path: Path) -> None:
    file, data = fixture(tmp_path)
    data["unexpected_credentials"] = "secret"
    file.write_text(json.dumps(data))
    with pytest.raises(InvalidInputError):
        parse_manifest(file)
    del data["unexpected_credentials"]
    data["model_staging"] = "relative/ai"
    file.write_text(json.dumps(data))
    with pytest.raises(InvalidInputError):
        parse_manifest(file)


def test_reject_manifest_symlink_and_world_writable_manifest(tmp_path: Path) -> None:
    file, _ = fixture(tmp_path)
    link = tmp_path / "manifest-alias.json"
    link.symlink_to(file)
    with pytest.raises(InvalidInputError):
        parse_manifest(link)
    file.chmod(0o666)
    with pytest.raises(ScopeDeniedError):
        parse_manifest(file)


def test_disallow_history_and_controller_ledger_under_writable_guest_home(tmp_path: Path) -> None:
    file, data = fixture(tmp_path)
    Path(data["controller_ledger"]).unlink()
    nested = Path(data["home"]) / "controller.sqlite"
    nested.touch()
    data["controller_ledger"] = str(nested)
    file.write_text(json.dumps(data))
    with pytest.raises(InvalidInputError):
        parse_manifest(file)


def test_rootfs_parent_symlink_components_are_denied(tmp_path: Path) -> None:
    file, data = fixture(tmp_path)
    alias = tmp_path / "rootfs-alias"
    alias.symlink_to(Path(data["rootfs"]), target_is_directory=True)
    data["rootfs"] = str(alias)
    file.write_text(json.dumps(data))
    with pytest.raises(InvalidInputError):
        parse_manifest(file)
