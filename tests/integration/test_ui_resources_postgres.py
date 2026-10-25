"""Authenticated PostgreSQL preferences, transfer lifecycle and workspace isolation."""
from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.api.ui_resources import TransferArtifact, UiResourceStore
from potbot.api.ui_transfer_worker import TransferWorker
from potbot.domain.errors import RevisionConflictError, ScopeDeniedError

pytestmark = pytest.mark.postgres


def sample_artifact(payload=b"synthetic-integration-checksum") -> TransferArtifact:
    return TransferArtifact.parse({
        "model_id": "Qwen/Qwen3-0.6B", "artifact_repository": "Qwen/Qwen3-0.6B",
        "revision": "a" * 40, "filename": "weights.Q4_K_M.gguf",
        "quantization": "Q4_K_M", "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    })


def test_preferences_revisions_are_workspace_and_principal_scoped(app_env):
    store = UiResourceStore(app_env.db)
    assert store.preferences(app_env.context) == {"revision": 0, "settings": {}}
    updated = store.update_preferences(app_env.context, 0, {"theme": "dark", "xpraMaxFps": 30})
    assert updated == {"revision": 1, "settings": {"theme": "dark", "xpraMaxFps": 30}}
    with pytest.raises(RevisionConflictError):
        store.update_preferences(app_env.context, 0, {"theme": "light"})
    assert UiResourceStore(app_env.other_db).preferences(app_env.other_context)["revision"] == 0
    assert store.update_preferences(app_env.context, 1, {"theme": "light"})["revision"] == 2


def test_real_download_worker_writes_only_verified_bytes(app_env, tmp_path: Path):
    store = UiResourceStore(app_env.db)
    artifact_bytes = b"synthetic-integration-checksum"
    item = sample_artifact(artifact_bytes)
    request_id = uuid4()
    first = store.add_transfer(app_env.context, request_id=request_id, artifact=item)
    assert first["created"] is True
    again = store.add_transfer(app_env.context, request_id=request_id, artifact=item)
    assert again == {**first, "created": False}
    assert len(store.transfers(app_env.context)) == 1
    assert UiResourceStore(app_env.other_db).transfers(app_env.other_context) == []
    model_root = tmp_path / "models"
    model_root.mkdir()
    worker = TransferWorker(app_env.db, app_env.context, model_root,
        downloader=lambda *_: iter([artifact_bytes[:11], artifact_bytes[11:]]))
    assert worker.once() == {"status": "installed", "bytes": len(artifact_bytes)}
    completed = store.transfers(app_env.context)[0]
    assert completed["state"] == "installed"
    assert completed["verifiedBytes"] == len(artifact_bytes)
    assert worker.once()["status"] == "idle"
    with pytest.raises(ScopeDeniedError):
        store.cancel_transfer(app_env.context, uuid4())


def test_checksum_failure_never_publishes_an_installation(app_env, tmp_path: Path):
    store = UiResourceStore(app_env.db)
    item = sample_artifact()
    store.add_transfer(app_env.context, request_id=uuid4(), artifact=item)
    model_root = tmp_path / "models"
    model_root.mkdir()
    worker = TransferWorker(app_env.db, app_env.context, model_root,
                            downloader=lambda *_: iter([b"synthetic-but-wrong-contents"]))
    assert worker.once()["status"] == "failed"
    assert store.transfers(app_env.context)[0]["verifiedBytes"] == 0
    assert not list(model_root.rglob("*.gguf"))


def test_queued_cancel_is_terminal_without_transfer(app_env):
    store = UiResourceStore(app_env.db)
    item = store.add_transfer(app_env.context, request_id=uuid4(), artifact=sample_artifact())
    cancelled = store.cancel_transfer(app_env.context, __import__("uuid").UUID(item["id"]))
    assert cancelled["state"] == "cancelled"
    assert store.transfers(app_env.context)[0]["downloadedBytes"] == 0
