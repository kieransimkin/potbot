"""Optional S9 admission pins do not break existing S8 operator manifests."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.tools.operator_cli import parse_manifest


def manifest(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    fields: dict[str, str] = {}
    for name in ("rootfs", "home", "model_staging", "shared_models", "shared_tools",
                 "ai_config_dir", "ai_history_dir"):
        directory = tmp_path / name
        directory.mkdir()
        fields[name] = str(directory)
    ledger = tmp_path / "leases.sqlite"
    ledger.touch()
    fields["controller_ledger"] = str(ledger)
    fields.update(
        workspace_id=str(uuid4()),
        image_digest="a" * 64,
        qualification_sha256="b" * 64,
    )
    path = tmp_path / "operator.json"
    path.write_text(json.dumps(fields))
    path.chmod(0o600)
    return path, fields


def test_legacy_manifest_still_supported(tmp_path: Path) -> None:
    path, _ = manifest(tmp_path)
    options = parse_manifest(path)
    assert options.tool_entrypoints == ()
    assert options.runtime().tool_entrypoints == ()


def test_operator_pins_are_bounded_and_pass_to_runtime(tmp_path: Path) -> None:
    path, record = manifest(tmp_path)
    item = "/usr/local/tools/stemlab/static/stemlab"
    record["tool_entrypoints"] = {item: "c" * 64}
    path.write_text(json.dumps(record))
    options = parse_manifest(path)
    assert options.tool_entrypoints == ((item, "c" * 64),)
    assert options.runtime().tool_entrypoints == options.tool_entrypoints


@pytest.mark.parametrize("value", [
    {"/etc/shadow": "a" * 64},
    {"/usr/local/tools/stemlab/checkout/main.py": "a" * 64},
    {"/usr/local/tools/stemlab/static/../escape": "a" * 64},
    {"/usr/local/tools/stemlab/static/app": "bad-digest"},
    ["not", "a", "map"],
])
def test_invalid_runtime_pins_rejected(tmp_path: Path, value) -> None:
    path, record = manifest(tmp_path)
    record["tool_entrypoints"] = value
    path.write_text(json.dumps(record))
    with pytest.raises(InvalidInputError):
        parse_manifest(path)
