"""Large downloads happen only for parsed encoding operations, before encoding."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from potbot.domain.errors import ModelUnavailableError
from potbot.memory import embedding_cli, local_encoder
from potbot.semantics import cli as syn_cli


@pytest.mark.parametrize("module", [embedding_cli, syn_cli])
def test_help_and_invalid_arguments_do_not_download(
    module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module,
        "ensure_standard_model",
        lambda *_args, **_kwargs: pytest.fail("unparsed operation must not download"),
    )
    with pytest.raises(SystemExit) as help_exit:
        module.main(["--help"])
    assert help_exit.value.code == 0
    with pytest.raises(SystemExit) as missing_arguments:
        module.main(["index"])
    assert missing_arguments.value.code == 2


@pytest.mark.parametrize("override", [None, "explicit relative model"])
def test_smoke_prepares_project_local_default_or_explicit_path_before_encoding(
    override: str | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.setenv("POTBOT_PROJECT_ROOT", str(project))
    monkeypatch.chdir(caller)
    expected = Path(override) if override else project / "models" / "qwen3-embedding-0.6b"
    events: list[str] = []

    def ensure(root: Path, *, model_dir: Path, progress: Any) -> dict[str, Any]:
        assert root == project
        assert model_dir == expected
        assert callable(progress)
        events.append("prepared")
        return {}

    class Encoder:
        def __init__(self, model_dir: Path, profile: Any) -> None:
            assert model_dir == expected
            assert events == ["prepared"]

        def encode_supervised(self, texts: Any, kind: str, **kwargs: Any) -> SimpleNamespace:
            events.append(kind)
            vectors = ((1.0, 0.0), (0.0, 1.0), (-1.0, -1.0))
            return SimpleNamespace(vectors=vectors[: len(texts)], input_tokens=10, elapsed_ms=1)

    monkeypatch.setattr(embedding_cli, "ensure_standard_model", ensure)
    monkeypatch.setattr(local_encoder, "QwenLocalEncoder", Encoder)
    arguments = ["smoke"] if override is None else ["smoke", "--model-dir", override]
    assert embedding_cli.main(arguments) == 0
    assert events == ["prepared", "document", "query"]
    assert json.loads(capsys.readouterr().out)["top_document_indices"] == [0, 1]


def test_syn_index_prepares_model_before_any_encoding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepared: list[Path] = []
    monkeypatch.setenv("POTBOT_DSN", "postgresql://fixture@localhost/potbot_test")
    monkeypatch.setenv("POTBOT_PROJECT_ROOT", str(tmp_path))

    def ensure(root: Path, *, model_dir: Path, progress: Any) -> dict[str, Any]:
        assert root == tmp_path
        prepared.append(model_dir)
        raise ModelUnavailableError("Controlled preparation failure before encoding.")

    monkeypatch.setattr(syn_cli, "ensure_standard_model", ensure)
    arguments = ["index"]
    for argument in ("workspace-id", "principal-id", "snapshot-id", "episode-id", "space-id"):
        arguments.extend([f"--{argument}", str(uuid4())])
    arguments.extend(["--ref", "syn://object/fixture"])
    assert syn_cli.main(arguments) == 2
    assert prepared == [tmp_path / "models" / "qwen3-embedding-0.6b"]
    assert json.loads(capsys.readouterr().err)["error"] == "model_unavailable"
