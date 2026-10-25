"""Offline UI resource validation; no real model download is attempted."""
from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.api.ui_resources import TransferArtifact, validate_preferences
from potbot.api.ui_transfer_worker import _safe_hf_url, _owned_directory
from potbot.domain.errors import InvalidInputError
from potbot.persistence import ui_resources_schema


def artifact(**changes):
    value = {
        "model_id": "Qwen/Qwen3-0.6B",
        "artifact_repository": "Qwen/Qwen3-0.6B",
        "revision": "a" * 40,
        "filename": "weights.Q4_K_M.gguf",
        "quantization": "Q4_K_M",
        "sha256": hashlib.sha256(b"fake model bytes").hexdigest(),
        "size_bytes": len(b"fake model bytes"),
    }
    return TransferArtifact.parse({**value, **changes})


def test_preferences_whitelist_and_types():
    assert validate_preferences({"theme": "dark", "xpraMaxFps": 24}) == {"theme": "dark", "xpraMaxFps": 24}
    for field in ({"password": "secret"}, {"backendUrl": "http://elsewhere"},
                  {"xpraMaxFps": 0}, {"reducedMotion": 1}, {"theme": "neon"}):
        with pytest.raises(InvalidInputError):
            validate_preferences(field)


def test_artifact_pinning_and_path_boundaries():
    assert artifact().quantization == "Q4_K_M"
    for overrides in ({"revision": "main"}, {"filename": "../etc/passwd"},
                      {"artifact_repository": "bad//model"},
                      {"filename": "foo.txt"}, {"size_bytes": 0},
                      {"size_bytes": 128 * 1024**3 + 1},
                      {"quantization": "Q4_TRUST_ME"}, {"sha256": "bad"}):
        with pytest.raises(InvalidInputError):
            artifact(**overrides)


def test_huggingface_egress_allowlist():
    assert _safe_hf_url("https://huggingface.co/Qwen/Qwen3-0.6B/resolve/" + "a"*40 + "/x.gguf")
    assert _safe_hf_url("https://cas-bridge.xethub.hf.co/x")
    for unsafe in ("http://huggingface.co/repo", "https://127.0.0.1/file",
                   "https://user:pass@huggingface.co/x", "https://huggingface.co:8080/x",
                   "https://huggingface.co.evil.test/x", "file:///etc/passwd"):
        assert not _safe_hf_url(unsafe)


def test_model_storage_fails_closed_on_symlink(tmp_path: Path):
    root = tmp_path / "models"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (root / "Qwen--Qwen3-0.6B").symlink_to(external, target_is_directory=True)
    with pytest.raises(InvalidInputError):
        _owned_directory(root, ("Qwen--Qwen3-0.6B", "a"*40, "Q4_K_M"))


def test_migration_26_appended_without_editing_history():
    assert set(ui_resources_schema.WORKSPACE_TABLES) == {"ui_preferences", "ui_artifact_transfers"}
    assert not ui_resources_schema.APPEND_ONLY_TABLES
    assert all("DROP TABLE" not in statement for statement in ui_resources_schema.MIGRATION_STATEMENTS)


def test_real_filesystem_worker_only_publishes_hash_verified_artifacts(tmp_path, monkeypatch):
    from potbot.api.ui_transfer_worker import TransferWorker
    from potbot.domain.auth import WorkspaceContext
    from uuid import uuid4

    payload = b"synthetic-weights"
    token = uuid4()
    job = {
        "id": uuid4(), "lease_token": token,
        "model_id": "Qwen/Qwen3-0.6B", "artifact_repository": "Qwen/Qwen3-0.6B",
        "revision": "a" * 40, "quantization": "Q4_K_M",
        "filename": "weights.Q4_K_M.gguf", "expected_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    root = tmp_path / "models"
    root.mkdir()
    worker = TransferWorker(object(), WorkspaceContext(uuid4(), uuid4()), root,
                            downloader=lambda *_: iter([payload[:4], payload[4:]]))
    states = []
    monkeypatch.setattr(worker, "_update", lambda _job, **kw: states.append(kw))
    assert worker.run_claim(job) == {"status": "installed", "bytes": len(payload)}
    assert states[-1]["state"] == "installed"
    assert states[-1]["verified"] == len(payload)
    assert len(list(root.rglob("*.gguf"))) == 1

    failed_job = {**job, "sha256": "a" * 64, "revision": "b" * 40}
    states.clear()
    assert worker.run_claim(failed_job)["status"] == "failed"
    assert states[-1]["state"] == "failed"
    assert states[-1].get("verified", 0) == 0
    assert len(list(root.rglob("*.gguf"))) == 1
    assert not list(root.rglob("*.part"))
