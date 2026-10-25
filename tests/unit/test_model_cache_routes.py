"""S3 model route and installer preflight tests: no network or weights."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.tools.model_cache_routes import (
    MODEL_ENV,
    MODEL_HOME_ALIASES,
    TOOL_DOWNLOAD_POLICIES,
    validate_model_destination,
    validate_tool_installation,
)


def test_skeleton_aliases_are_in_installed_agent_homes(tmp_path: Path) -> None:
    from potbot.tools.agent_skeleton import install_agent_skeleton

    home = tmp_path / "agent"
    home.mkdir()
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
    try:
        install_agent_skeleton(fd, workspace_name="danceflow")
    finally:
        os.close(fd)
    for relative, target in MODEL_HOME_ALIASES.items():
        assert (home / relative).is_symlink()
        assert os.readlink(home / relative) == target
    metadata = json.loads((home / ".config/potbot/skeleton.json").read_text())
    assert metadata["model_cache_aliases"] == MODEL_HOME_ALIASES


def test_installer_rejects_unrouted_and_unobserved_downloads() -> None:
    tool = next(t for t in TOOL_DOWNLOAD_POLICIES if t.name == "PixelCue")
    with pytest.raises(InvalidInputError):
        validate_tool_installation(tool, env=dict(MODEL_ENV), destinations=(), probe_performed=True)
    with pytest.raises(ScopeDeniedError):
        validate_tool_installation(tool, env={}, destinations=("/var/ai/cache/check/model.gguf",),
                                   probe_performed=True)
    for value in (
        "/home/agent/models/file.gguf", "/opt/potbot/models/fake.gguf",
        "/var/ai/../home/foo", "/var/ai/cache/../../home/foo",
        "/var/ai/models", "var/ai/downloads/x", "/var/ai/misc/file.gguf",
    ):
        with pytest.raises((InvalidInputError, ScopeDeniedError)):
            validate_model_destination(value)
    validate_tool_installation(
        tool, env=dict(MODEL_ENV),
        destinations=("/var/ai/cache/huggingface/hub/weight.safetensors",),
        probe_performed=True,
    )
    with pytest.raises(InvalidInputError):
        validate_tool_installation(tool, env=dict(MODEL_ENV), destinations=(), probe_performed=False)
    without = next(t for t in TOOL_DOWNLOAD_POLICIES if t.name == "DanceMoves")
    with pytest.raises(ScopeDeniedError):
        validate_tool_installation(without, env={},
                                   destinations=("/var/ai/downloads/file.gguf",),
                                   probe_performed=True)


def test_image_and_installation_gate(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    image = root / "sandbox/image/Dockerfile"
    gate = root / "scripts/check_model_cache_policy.py"
    from subprocess import run

    result = run([sys.executable, str(gate), "--image-contract", str(image)],
                 capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(root / "src")})
    assert result.returncode == 0, result.stderr
    probe = tmp_path / "probe.json"
    payload = {
        "schema": "potbot-tool-model-probe/v1", "tool": "RasterMoves", "network_used": False,
        "environment": MODEL_ENV,
        "paths": ["/var/ai/cache/huggingface/hub/model.safetensors"],
    }
    probe.write_text(json.dumps(payload))
    result = run([sys.executable, str(gate), "--tool", "RasterMoves", "--probe", str(probe)],
                 capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(root / "src")})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["qualified_for_model_routes"] is True
    payload["paths"] = ["/home/models/checkpoint.safetensors"]
    probe.write_text(json.dumps(payload))
    assert run([sys.executable, str(gate), "--tool", "RasterMoves", "--probe", str(probe)],
               capture_output=True, env={**os.environ, "PYTHONPATH": str(root / "src")}).returncode != 0
