"""Filesystem artifact identity, atomic publication, and containment tests."""

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.persistence.artifacts import ArtifactIntegrityError, LocalArtifactBackend


def test_identical_payloads_share_identity_and_exact_bytes(tmp_path: Path) -> None:
    backend = LocalArtifactBackend(tmp_path / "objects")
    workspace = uuid4()
    data = b"\x00raw evidence\xff\n"
    first = backend.publish(workspace, data)
    assert first == backend.publish(workspace, data)
    assert backend.read(workspace, first) == data
    other = backend.publish(uuid4(), data)
    assert other.digest == first.digest
    assert other.locator != first.locator


@pytest.mark.parametrize("locator", ["../../secret", "/etc/passwd", "other/sha256/object"])
def test_locator_escape_rejected_before_read(tmp_path: Path, locator: str) -> None:
    backend = LocalArtifactBackend(tmp_path)
    workspace = uuid4()
    artifact = backend.publish(workspace, b"safe")
    with pytest.raises(ArtifactIntegrityError):
        backend.read(workspace, replace(artifact, locator=locator))


def test_corruption_is_never_returned_or_silently_repaired(tmp_path: Path) -> None:
    backend = LocalArtifactBackend(tmp_path)
    workspace = uuid4()
    artifact = backend.publish(workspace, b"correct")
    (tmp_path / artifact.locator).write_bytes(b"altered")
    with pytest.raises(ArtifactIntegrityError):
        backend.read(workspace, artifact)
    with pytest.raises(ArtifactIntegrityError):
        backend.publish(workspace, b"correct")
    assert (tmp_path / artifact.locator).read_bytes() == b"altered"


def test_symlinked_scope_directory_cannot_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    backend = LocalArtifactBackend(tmp_path / "objects")
    workspace = uuid4()
    (backend.root / str(workspace)).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ArtifactIntegrityError):
        backend.publish(workspace, b"secret")
    assert list(outside.iterdir()) == []


def test_interrupted_publication_never_exposes_a_complete_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = LocalArtifactBackend(tmp_path)

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("fixture publication failure")

    monkeypatch.setattr("potbot.persistence.artifacts.os.link", fail)
    with pytest.raises(ArtifactIntegrityError):
        backend.publish(uuid4(), b"incomplete")
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


def test_limits_are_enforced_before_publication(tmp_path: Path) -> None:
    backend = LocalArtifactBackend(tmp_path, max_bytes=2)
    with pytest.raises(InvalidInputError):
        backend.publish(uuid4(), b"long")
    assert list(tmp_path.iterdir()) == []
