from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
from typing import Any
from urllib.request import Request

import pytest

from potbot import dependencies
from potbot.domain.errors import InvalidInputError, ModelUnavailableError
from potbot.memory.embedding_profile import EmbeddingProfile
from potbot.memory.local_encoder import setup_local_model, verify_local_model


class Response(io.BytesIO):
    def __init__(self, data: bytes, *, status: int = 200, content_range: str = "") -> None:
        super().__init__(data)
        self.status = status
        self.headers = {"Content-Range": content_range}

    def geturl(self) -> str:
        return "https://model-cdn.example/public-model"


@pytest.fixture
def assets(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    data = {
        asset.name: f"verified test data for {asset.name}".encode()
        for asset in dependencies.QWEN_ASSETS
    }
    monkeypatch.setattr(
        dependencies,
        "QWEN_ASSETS",
        tuple(
            dependencies.ModelAsset(name, len(value), hashlib.sha256(value).hexdigest())
            for name, value in data.items()
        ),
    )
    monkeypatch.setattr(dependencies, "CHUNK_BYTES", 8)
    return data


def server(monkeypatch: pytest.MonkeyPatch, assets: dict[str, bytes]) -> list[Request]:
    requests: list[Request] = []

    def download(request: Request) -> Response:
        requests.append(request)
        data = assets[request.full_url.rsplit("/", 1)[1]]
        offset = int(request.headers.get("Range", "bytes=0-").split("=")[1][:-1])
        return Response(
            data[offset:],
            status=206 if offset else 200,
            content_range=f"bytes {offset}-{len(data) - 1}/{len(data)}" if offset else "",
        )

    monkeypatch.setattr(dependencies, "_open_download", download)
    return requests


def model_directory(root: Path) -> Path:
    return root / "models" / dependencies.MODEL_DIRECTORY


def deny_network(*args: object, **kwargs: object) -> Any:
    raise AssertionError("Unexpected network request")


def test_complete_download_is_pinned_atomic_and_reusable_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    assets: dict[str, bytes],
) -> None:
    requests = server(monkeypatch, assets)
    events: list[dependencies.DownloadProgress] = []
    directory = model_directory(tmp_path)

    def progress(event: dependencies.DownloadProgress) -> None:
        assert not (directory / dependencies.MANIFEST).exists()
        events.append(event)

    manifest = dependencies.ensure_standard_model(tmp_path, progress=progress)
    assert len(requests) == len(assets)
    assert all(dependencies.QWEN_REVISION in request.full_url for request in requests)
    assert all(request.headers["Accept-encoding"] == "identity" for request in requests)
    assert json.loads((directory / dependencies.MANIFEST).read_text()) == manifest
    assert all((directory / name).read_bytes() == data for name, data in assets.items())
    assert events[-1].overall_completed_bytes == sum(map(len, assets.values()))
    assert any(event.stage == "verifying" for event in events)
    assert not list(directory.glob("*.part"))
    verify_local_model(directory, EmbeddingProfile())
    monkeypatch.setattr(dependencies, "_open_download", deny_network)
    assert dependencies.ensure_standard_model(tmp_path) == manifest
    assert dependencies.dependency_status(tmp_path)["model"]["status"] == "ready"


def test_interrupted_download_resumes_with_http_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    assets: dict[str, bytes],
) -> None:
    name = next(iter(assets))
    monkeypatch.setattr(dependencies, "_open_download", lambda request: Response(assets[name][:9]))
    with pytest.raises(ModelUnavailableError, match="interrupted"):
        dependencies.ensure_standard_model(tmp_path)
    directory = model_directory(tmp_path)
    assert (directory / f".{name}.part").read_bytes() == assets[name][:9]
    assert not (directory / name).exists()
    assert not (directory / dependencies.MANIFEST).exists()
    requests = server(monkeypatch, assets)
    events: list[dependencies.DownloadProgress] = []
    dependencies.ensure_standard_model(tmp_path, progress=events.append)
    assert requests[0].headers["Range"] == "bytes=9-"
    assert events[1].stage == "resuming"
    assert (directory / name).read_bytes() == assets[name]


def test_server_ignoring_range_restarts_without_appending_duplicate_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    assets: dict[str, bytes],
) -> None:
    name = next(iter(assets))
    monkeypatch.setattr(dependencies, "_open_download", lambda request: Response(assets[name][:7]))
    with pytest.raises(ModelUnavailableError):
        dependencies.ensure_standard_model(tmp_path)
    monkeypatch.setattr(
        dependencies,
        "_open_download",
        lambda request: Response(assets[request.full_url.rsplit("/", 1)[1]]),
    )
    events: list[dependencies.DownloadProgress] = []
    dependencies.ensure_standard_model(tmp_path, progress=events.append)
    assert any(event.stage == "restarting" for event in events)
    assert (model_directory(tmp_path) / name).read_bytes() == assets[name]


def test_invalid_resume_response_retains_previous_partial_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    assets: dict[str, bytes],
) -> None:
    name = next(iter(assets))
    monkeypatch.setattr(dependencies, "_open_download", lambda request: Response(assets[name][:7]))
    with pytest.raises(ModelUnavailableError):
        dependencies.ensure_standard_model(tmp_path)
    monkeypatch.setattr(
        dependencies,
        "_open_download",
        lambda request: Response(assets[name], status=206, content_range="bytes 0-1/2"),
    )
    with pytest.raises(ModelUnavailableError, match="resume range"):
        dependencies.ensure_standard_model(tmp_path)
    assert (model_directory(tmp_path) / f".{name}.part").read_bytes() == assets[name][:7]


def test_hash_mismatch_never_publishes_a_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    assets: dict[str, bytes],
) -> None:
    name = next(iter(assets))
    monkeypatch.setattr(
        dependencies, "_open_download", lambda request: Response(b"!" * len(assets[name]))
    )
    with pytest.raises(ModelUnavailableError, match="SHA256"):
        dependencies.ensure_standard_model(tmp_path)
    directory = model_directory(tmp_path)
    assert not (directory / name).exists()
    assert not (directory / f".{name}.part").exists()
    assert not (directory / dependencies.MANIFEST).exists()


def test_mismatched_existing_owned_file_is_repaired_from_pinned_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    assets: dict[str, bytes],
) -> None:
    server(monkeypatch, assets)
    dependencies.ensure_standard_model(tmp_path)
    name = next(iter(assets))
    (model_directory(tmp_path) / name).write_bytes(b"tampered")
    assert dependencies.dependency_status(tmp_path)["model"]["status"] == "incomplete"
    requests = server(monkeypatch, assets)
    dependencies.ensure_standard_model(tmp_path)
    assert len(requests) == 1
    assert (model_directory(tmp_path) / name).read_bytes() == assets[name]


def test_unowned_directory_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    assets: dict[str, bytes],
) -> None:
    directory = model_directory(tmp_path)
    directory.mkdir(parents=True)
    existing = directory / "config.json"
    existing.write_text("keep me")
    monkeypatch.setattr(dependencies, "_open_download", deny_network)
    with pytest.raises(InvalidInputError, match="unowned"):
        dependencies.ensure_standard_model(tmp_path)
    assert existing.read_text() == "keep me"


@pytest.mark.skipif(
    os.name == "nt", reason="Creating Windows symlinks requires elevated privileges"
)
def test_partial_download_symlink_cannot_overwrite_another_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    assets: dict[str, bytes],
) -> None:
    directory = model_directory(tmp_path)
    directory.mkdir(parents=True)
    (directory / dependencies.DOWNLOAD_MARKER).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": dependencies.QWEN_MODEL,
                "revision": dependencies.QWEN_REVISION,
            }
        )
    )
    outside = tmp_path / "keep.txt"
    outside.write_text("keep me")
    name = next(iter(assets))
    (directory / f".{name}.part").symlink_to(outside)
    monkeypatch.setattr(dependencies, "_open_download", deny_network)
    with pytest.raises(InvalidInputError, match="unsupported file or link"):
        dependencies.ensure_standard_model(tmp_path)
    assert outside.read_text() == "keep me"


def test_concurrent_downloader_is_rejected_without_stealing_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    assets: dict[str, bytes],
) -> None:
    directory = model_directory(tmp_path)
    directory.mkdir(parents=True)
    monkeypatch.setattr(dependencies, "_open_download", deny_network)
    with dependencies._download_lock(directory):
        with pytest.raises(ModelUnavailableError, match="Another process"):
            dependencies.ensure_standard_model(tmp_path)
    server(monkeypatch, assets)
    dependencies.ensure_standard_model(tmp_path)


def test_https_redirect_cannot_downgrade_transport() -> None:
    redirect = dependencies._HttpsOnlyRedirect()
    with pytest.raises(ModelUnavailableError, match="HTTPS"):
        redirect.redirect_request(
            Request("https://huggingface.co/model"), None, 302, "", {}, "http://example.com/model"
        )


def test_explicit_relative_model_dir_is_relative_to_caller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    assets: dict[str, bytes],
) -> None:
    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.chdir(caller)
    server(monkeypatch, assets)
    dependencies.ensure_standard_model(tmp_path / "project", model_dir=Path("chosen models"))
    assert (caller / "chosen models" / dependencies.MANIFEST).is_file()
    assert not (tmp_path / "project" / "models").exists()


def test_project_root_environment_controls_default_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POTBOT_PROJECT_ROOT", str(tmp_path / "project"))
    assert dependencies.project_root() == tmp_path / "project"


def test_setup_wrapper_shares_resume_download_and_stderr_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    assets: dict[str, bytes],
    capsys: pytest.CaptureFixture[str],
) -> None:
    server(monkeypatch, assets)
    setup_local_model(tmp_path / "explicit model")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[Potbot dependencies]" in captured.err
    assert "overall 100.0%" in captured.err


def test_dependency_cli_has_json_output_progress_and_no_credential_requirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    assets: dict[str, bytes],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("POTBOT_DATABASE_URL", raising=False)
    server(monkeypatch, assets)
    assert dependencies.main(["download", "--project-root", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "ready"
    assert "[Potbot dependencies]" in captured.err
    monkeypatch.setattr(dependencies, "_open_download", deny_network)
    assert dependencies.main(["status", "--project-root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["model"]["status"] == "ready"


def test_dependency_help_does_not_download(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dependencies, "_open_download", deny_network)
    with pytest.raises(SystemExit) as result:
        dependencies.main(["--help"])
    assert result.value.code == 0


def test_network_errors_never_echo_download_url_or_other_exception_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    assets: dict[str, bytes],
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unavailable(request: Request) -> None:
        raise OSError("https://somewhere.example/?credential=secret")

    monkeypatch.setattr(dependencies, "_open_download", unavailable)
    assert dependencies.main(["download", "--project-root", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert "credential=secret" not in captured.err
    assert not captured.out


def test_cancelled_download_has_resumable_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    assets: dict[str, bytes],
    capsys: pytest.CaptureFixture[str],
) -> None:
    def interrupted(request: Request) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(dependencies, "_open_download", interrupted)
    assert dependencies.main(["download", "--project-root", str(tmp_path)]) == 130
    assert "retained for resume" in capsys.readouterr().err
