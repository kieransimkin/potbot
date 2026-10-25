"""System image identity manifests contain build facts, not runtime attestation."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "sandbox_image_manifest.py"
    spec = importlib.util.spec_from_file_location("sandbox_image_manifest", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_immutable_image_manifest_no_false_qualification() -> None:
    make = _module().manifest
    entry = make("ghcr.io/example/potbot-sandbox", "a" * 64, "b" * 40, "linux/arm64")
    assert entry["image_ref"] == "ghcr.io/example/potbot-sandbox@sha256:" + "a" * 64
    assert entry["guest_home"] == "/home"
    assert entry["guest_writable_persistent_mounts"] == ["/home", "/usr/local/tools"]
    assert entry["guest_shared_tool_writable_mount"] == "/usr/local/tools"
    assert entry["guest_writable_ephemeral_mounts"] == ["/var/ai"]
    assert entry["guest_shared_model_readonly_mount"] == "/opt/potbot/models"
    assert entry["rootfs_read_only_required"] is True
    assert entry["runtime_qualified"] is False
    assert entry["signature_verified_by_host"] is False
    assert len(entry["manifest_sha256"]) == 64
    for args in (
        ("../../bad", "a" * 64, "b" * 40, "linux/amd64"),
        ("ghcr.io/example/repo", "bad", "b" * 40, "linux/amd64"),
        ("ghcr.io/example/repo", "a" * 64, "bad", "linux/amd64"),
        ("ghcr.io/example/repo", "a" * 64, "b" * 40, "windows/amd64"),
    ):
        with pytest.raises(ValueError):
            make(*args)
