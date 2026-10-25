from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from potbot.domain.errors import ScopeDeniedError
from potbot.tools.model_cache_routes import MODEL_ENV


def contract() -> dict[str, object]:
    return {
        "schema": "potbot-shared-tool-preflight/v1",
        "definition": {
            "slug": "rastermoves",
            "repository": "https://github.com/kieransimkin/RasterMoves",
            "commit": "a" * 40,
            "cleanup_paths": ["build", ".pytest_cache"],
            "lockfiles": [], "reusable_artifacts": [],
            "model_downloads": True,
        },
        "probe": {
            "tool": "rastermoves", "network_used": False, "observed": True,
            "environment": dict(MODEL_ENV),
            "model_destinations": ["/var/ai/cache/huggingface/hub/weights.safetensors"],
            "writes": [
                "/usr/local/tools/rastermoves/environment/bin/rastermoves",
                "/usr/local/tools/rastermoves/downloads/wheel.whl",
                "/var/ai/cache/huggingface/hub/weights.safetensors",
                "/home/alice/.tmp/setup.log",
            ],
            "cleanup_test_passed": True, "normal_use_test_passed": True,
        },
    }


def test_installer_gate_rejects_unrouted_writes_and_missing_clean() -> None:
    from scripts.check_shared_tool_install import review_manifest

    assert review_manifest(contract())["approved_to_install"] is False
    bad = contract()
    bad["probe"]["writes"] = ["/usr/local/tools/other/checkout/steal"]
    with pytest.raises(ScopeDeniedError):
        review_manifest(bad)
    bad = contract()
    bad["probe"]["model_destinations"] = ["/home/alice/models/weights.safetensors"]
    with pytest.raises(ScopeDeniedError):
        review_manifest(bad)
    bad = contract()
    bad["probe"]["cleanup_test_passed"] = False
    with pytest.raises(ScopeDeniedError):
        review_manifest(bad)
    bad = contract()
    bad["probe"]["writes"] = ["/usr/local/tools/rastermoves/checkout/tmp.py"]
    with pytest.raises(ScopeDeniedError):
        review_manifest(bad)


def test_cli_tool_gate_accepts_offline_witness_and_is_not_authorization(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    probe = tmp_path / "probe.json"
    probe.write_text(json.dumps(contract()), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(root / "scripts/check_shared_tool_install.py"), "--probe", str(probe)],
        capture_output=True, text=True, env={"PYTHONPATH": str(root / "src")},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["approved_to_install"] is False
