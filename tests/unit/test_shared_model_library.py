"""Model candidate observation + exact immutable promotion, no network/weights."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.tools.shared_model_library import (
    ModelAsset,
    ModelDownloadTicket,
    ModelDownloadWatcher,
    ModelIdentity,
    SharedModelLibrary,
)


def _gguf() -> bytes:
    return b"GGUF" + (3).to_bytes(4, "little") + (1).to_bytes(8, "little") + b"\x00" * 12


def _ticket(*, ticket_id: str = "a" * 32, data: bytes | None = None, repo: str = "Qwen/Test-0.6B"):
    blob = _gguf() if data is None else data
    identity = ModelIdentity(repo, "b" * 40, "q4_k_m", "gguf")
    return ModelDownloadTicket(ticket_id, identity, (
        ModelAsset("weights.gguf", hashlib.sha256(blob).hexdigest(), len(blob)),
    ))


@pytest.fixture
def storage(tmp_path: Path):
    root = tmp_path / "global-model-store"
    root.mkdir()
    locks = tmp_path / "operator-locks"
    locks.mkdir()
    staging = tmp_path / "workspace-A-ai"
    staging.mkdir()
    (staging / "downloads" / ("a" * 32)).mkdir(parents=True)
    library = SharedModelLibrary(shared_root=root, lock_root=locks)
    return library, root, staging


def test_promote_named_and_hashed_model_with_ro_links(storage):
    library, global_root, staging = storage
    content = _gguf()
    source = staging / "downloads" / ("a" * 32) / "weights.gguf"
    source.write_bytes(content)
    ticket = _ticket()
    result = library.promote(ticket=ticket, staging=staging)
    assert result.key.startswith("qwen--test-0.6b__r-")
    assert len(result.manifest_sha256) == 64
    blob = global_root / "blobs" / ticket.assets[0].sha256[:2] / ticket.assets[0].sha256
    assert blob.read_bytes() == content
    assert blob.stat().st_mode & 0o222 == 0
    guest_model = library.link_workspace(staging=staging, model=result)
    assert (guest_model / "weights.gguf").is_symlink()
    assert (guest_model / "weights.gguf").readlink().as_posix() == result.files[0][1]
    assert result == library.promote(ticket=ticket, staging=staging)
    assert library.link_workspace(staging=staging, model=result) == guest_model
    # Another workspace sees the same shared bytes through its own symlinks.
    another = staging.parent / "workspace-B-ai"
    another.mkdir()
    assert (library.link_workspace(staging=another, model=result) / "weights.gguf").is_symlink()
    assert len(list((global_root / "blobs").rglob(ticket.assets[0].sha256))) == 1


def test_bad_digest_partial_and_unsafe_link_fail_closed(storage):
    library, global_root, staging = storage
    source = staging / "downloads" / ("a" * 32) / "weights.gguf"
    content = _gguf()
    source.write_bytes(content[:10])
    with pytest.raises(StateMismatchError):
        library.promote(ticket=_ticket(), staging=staging)
    source.write_bytes(b"NOTG" + content[4:])
    with pytest.raises(StateMismatchError):
        library.promote(ticket=_ticket(data=b"NOTG" + content[4:]), staging=staging)
    source.unlink()
    source.symlink_to(global_root)
    with pytest.raises(ScopeDeniedError):
        library.promote(ticket=_ticket(), staging=staging)


def test_unknown_model_identity_and_path_not_promoted(storage):
    library, _, staging = storage
    with pytest.raises(InvalidInputError):
        ModelIdentity("Qwen/Test", "main", "q4_k_m", "gguf")
    with pytest.raises(InvalidInputError):
        ModelIdentity("Bad", "a" * 40, "q4_k_m", "gguf")
    with pytest.raises(InvalidInputError):
        ModelIdentity("Qwen/Test", "a" * 40, "malware!", "gguf")
    with pytest.raises(InvalidInputError):
        ModelAsset("../outside.gguf", "f" * 64, 25)
    (staging / "downloads" / ("a" * 32) / "weights.gguf").write_bytes(_gguf())
    bad = ModelDownloadTicket("a" * 32, ModelIdentity("Qwen/Test", "b" * 40,
                                                      "q4_k_m", "gguf"), (
        ModelAsset("missing.gguf", "c" * 64, 25),
    ))
    with pytest.raises(StateMismatchError):
        library.promote(ticket=bad, staging=staging)


def test_operator_sees_stable_completed_files_and_hf_hints(storage):
    _, _, staging = storage
    cache = staging / "cache/huggingface/hub/models--Qwen--Test-0.6B/snapshots" / ("c" * 40)
    cache.mkdir(parents=True)
    weight = cache / "model.safetensors"
    weight.write_bytes(b"weights")
    (cache / "half.safetensors.part").write_bytes(b"partial")
    (cache / "link.gguf").symlink_to("model.safetensors")
    watcher = ModelDownloadWatcher(staging)
    before = watcher.poll()
    assert len(before) == 2 and all(item.state == "settling" for item in before)
    assert all(item.possible_repository == "Qwen/Test-0.6B" for item in before)
    assert all(item.possible_revision == "c" * 40 for item in before)
    after = watcher.poll()
    assert len(after) == 2 and all(item.state == "needs_ticket" for item in after)
    weight.write_bytes(b"more model weights")
    assert any(item.state == "settling" for item in watcher.poll())
    assert all(item.state == "needs_ticket" for item in watcher.poll())


def test_existing_global_blob_never_overwritten(storage):
    library, root, staging = storage
    data = _gguf()
    asset = _ticket().assets[0]
    output = staging / "downloads" / ("a" * 32) / "weights.gguf"
    output.write_bytes(data)
    blob = root / "blobs" / asset.sha256[:2] / asset.sha256
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"wrong bytes")
    with pytest.raises(StateMismatchError):
        library.promote(ticket=_ticket(), staging=staging)
    assert blob.read_bytes() == b"wrong bytes"
