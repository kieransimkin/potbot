"""Local embedding validation has no dependency on a downloaded model."""

from dataclasses import replace
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.memory.embedding_profile import EmbeddingProfile, validate_vector
from potbot.memory.local_encoder import LocalModelUnavailable, QwenLocalEncoder


def test_query_and_document_format_are_distinct_and_space_is_complete() -> None:
    profile = EmbeddingProfile()
    assert (
        profile.prepare("retry after timeout", "query")
        == f"Instruct: {profile.query_instruction}\nQuery:retry after timeout"
    )
    assert profile.prepare("retry after timeout", "document") == "retry after timeout"
    for changed in (
        replace(profile, generation="v2"),
        replace(profile, model_revision="a" * 40),
        replace(profile, query_instruction="Find code"),
    ):
        assert changed.fingerprint != profile.fingerprint
    with pytest.raises(InvalidInputError):
        replace(profile, dimensions=768)
    with pytest.raises(InvalidInputError):
        replace(profile, model_revision="main")


@pytest.mark.parametrize(
    "vector",
    [
        [1.0],
        [0.0] * 1024,
        [float("nan")] + [0.0] * 1023,
        [float("inf")] + [0.0] * 1023,
        [2.0] + [0.0] * 1023,
    ],
)
def test_invalid_vectors_rejected(vector: list[float]) -> None:
    with pytest.raises(InvalidInputError):
        validate_vector(vector)


def test_missing_local_model_fails_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    def deny(*args: object, **kwargs: object) -> None:
        raise AssertionError("offline adapter attempted network")

    monkeypatch.setattr(socket.socket, "connect", deny)
    encoder = QwenLocalEncoder(tmp_path / "missing")
    with pytest.raises(LocalModelUnavailable):
        encoder.encode(("hello",), "query")


def test_unit_vector_accepted_and_wrong_profile_rejected(tmp_path: Path) -> None:
    assert len(validate_vector([1.0] + [0.0] * 1023)) == 1024
    with pytest.raises(InvalidInputError):
        QwenLocalEncoder(tmp_path, replace(EmbeddingProfile(), backend_version="different"))
