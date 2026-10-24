"""Project-local dependencies, verified resumable model downloads, and progress.

The launch scripts use uv for Python and every locked package extra. This module
manages the active model's data files; inference never downloads dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import importlib.metadata
import json
import os
import platform
import re
import shutil
import stat
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit
from uuid import uuid4

from potbot import __version__
from potbot.domain.errors import DomainError, InvalidInputError, ModelUnavailableError
from potbot.domain.serialization import canonical_json
from potbot.memory.embedding_profile import QWEN_MODEL, QWEN_REVISION

MODEL_DIRECTORY = "qwen3-embedding-0.6b"
MANIFEST = "potbot-model.json"
DOWNLOAD_MARKER = ".potbot-download.json"
CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ModelAsset:
    name: str
    size: int
    sha256: str

    @property
    def url(self) -> str:
        return f"https://huggingface.co/{QWEN_MODEL}/resolve/{QWEN_REVISION}/{self.name}"


# Exact files from the already-qualified model revision. Large-file SHA256 values
# are published with that revision's Xet pointers; all six match the prior local
# embedding manifest. No mutable 'main' reference or first-use hash is trusted.
QWEN_ASSETS = (
    ModelAsset(
        "config.json", 727, "b5bf1f51fc45be473a54718cef92448d90a1be001bf9b9a44b8c7f10a19feaa9"
    ),
    ModelAsset(
        "model.safetensors",
        1191586416,
        "0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd",
    ),
    ModelAsset(
        "tokenizer.json",
        11423705,
        "def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a",
    ),
    ModelAsset(
        "tokenizer_config.json",
        9706,
        "253153d0738ceb4c668d2eff957714dd2bea0b56de772a9fdccd96cbf517e6a0",
    ),
    ModelAsset(
        "vocab.json", 2776833, "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910"
    ),
    ModelAsset(
        "merges.txt", 1671853, "8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5"
    ),
)


@dataclass(frozen=True, slots=True)
class DownloadProgress:
    stage: str
    item: str
    index: int
    count: int
    completed_bytes: int
    total_bytes: int
    overall_completed_bytes: int
    overall_total_bytes: int


class ConsoleProgress:
    """Human progress on stderr, while stdout remains available for JSON."""

    def __init__(self, stream: TextIO | None = None, interval: float = 0.25) -> None:
        self.stream = stream if stream is not None else sys.stderr
        self.interval = interval
        self._last_time = 0.0
        self._last_key: tuple[str, str] | None = None

    def __call__(self, event: DownloadProgress) -> None:
        now = time.monotonic()
        key = event.stage, event.item
        done = event.completed_bytes == event.total_bytes
        if key == self._last_key and not done and now - self._last_time < self.interval:
            return
        self._last_time, self._last_key = now, key
        percent = 100 * event.completed_bytes / max(1, event.total_bytes)
        total_percent = 100 * event.overall_completed_bytes / max(1, event.overall_total_bytes)
        print(
            f"[Potbot dependencies] {event.stage} {event.index}/{event.count} "
            f"{event.item}: {percent:.1f}% "
            f"({event.completed_bytes:,}/{event.total_bytes:,} bytes; overall {total_percent:.1f}%)",
            file=self.stream,
            flush=True,
        )


def project_root() -> Path:
    explicit = os.environ.get("POTBOT_PROJECT_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()
    checkout = Path(__file__).resolve().parents[2]
    return checkout if (checkout / "pyproject.toml").is_file() else Path.cwd().resolve()


def _target(root: Path, model_dir: Path | None) -> Path:
    selected = model_dir if model_dir is not None else root / "models" / MODEL_DIRECTORY
    original = selected.expanduser().absolute()
    if original.resolve() != original:
        raise InvalidInputError("Model storage must not traverse symlinks or junctions.")
    return original


def _regular(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode):
        raise InvalidInputError("Model storage contains an unsupported file or link.")
    return True


def _sha256(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise InvalidInputError("Model files must be ordinary files.")
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _matches(path: Path, asset: ModelAsset) -> bool:
    return _regular(path) and path.stat().st_size == asset.size and _sha256(path) == asset.sha256


def _manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "model": QWEN_MODEL,
        "revision": QWEN_REVISION,
        "tokenizer_revision": QWEN_REVISION,
        "files": {
            asset.name: {"sha256": asset.sha256, "bytes": asset.size} for asset in QWEN_ASSETS
        },
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    if not _regular(path):
        return None
    if path.stat().st_size > 65536:
        raise InvalidInputError("Model metadata exceeds its supported size.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise InvalidInputError(
            "Model metadata is invalid; choose an empty model directory."
        ) from None
    if not isinstance(value, dict):
        raise InvalidInputError("Model metadata must be an object.")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        _regular(path)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(canonical_json(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _download_lock(directory: Path) -> Iterator[None]:
    lock_path = directory / ".potbot-download.lock"
    if lock_path.exists() or lock_path.is_symlink():
        _regular(lock_path)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    stream = os.fdopen(descriptor, "r+b")
    try:
        if os.name == "nt":
            windows_lock = importlib.import_module("msvcrt")

            if os.fstat(descriptor).st_size == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                windows_lock.locking(descriptor, windows_lock.LK_NBLCK, 1)
            except OSError:
                raise ModelUnavailableError(
                    "Another process is downloading this model; retry shortly."
                ) from None
        else:
            import fcntl

            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ModelUnavailableError(
                    "Another process is downloading this model; retry shortly."
                ) from None
        yield
    finally:
        # Closing releases the OS lock, including on interruption; no PID-based
        # stale-lock deletion can accidentally steal another downloader's lock.
        stream.close()


def _check_directory_owner(directory: Path) -> None:
    marker = _read_json(directory / DOWNLOAD_MARKER)
    manifest = _read_json(directory / MANIFEST)
    expected = {"schema_version": 1, "model": QWEN_MODEL, "revision": QWEN_REVISION}
    if marker is not None and marker != expected:
        raise InvalidInputError("Model directory belongs to a different download revision.")
    if manifest is not None and any(manifest.get(key) != value for key, value in expected.items()):
        raise InvalidInputError("Model directory belongs to a different model revision.")
    if marker is None and manifest is None:
        assets = {asset.name: asset for asset in QWEN_ASSETS}
        for child in directory.iterdir():
            if child.name == ".potbot-download.lock":
                continue
            # Reuse exact complete files from earlier explicit setup. Unowned
            # mismatched files are never overwritten by a dependency operation.
            if child.name not in assets or not _matches(child, assets[child.name]):
                raise InvalidInputError(
                    "Model directory has unowned files; choose an empty directory."
                )
    _write_json(directory / DOWNLOAD_MARKER, expected)


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        response: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> urllib.request.Request | None:
        if urlsplit(new_url).scheme != "https":
            raise ModelUnavailableError("Model download redirected outside HTTPS.")
        return super().redirect_request(request, response, code, message, headers, new_url)


def _open_download(request: urllib.request.Request) -> Any:
    return urllib.request.build_opener(_HttpsOnlyRedirect()).open(request, timeout=30)


def _download_asset(
    directory: Path,
    asset: ModelAsset,
    index: int,
    already_complete: int,
    progress: Callable[[DownloadProgress], None],
) -> None:
    total = sum(item.size for item in QWEN_ASSETS)

    def emit(stage: str, amount: int) -> None:
        progress(
            DownloadProgress(
                stage,
                asset.name,
                index,
                len(QWEN_ASSETS),
                amount,
                asset.size,
                already_complete + amount,
                total,
            )
        )

    destination = directory / asset.name
    emit("checking", 0)
    if _matches(destination, asset):
        emit("ready", asset.size)
        return
    temporary = directory / f".{asset.name}.part"
    _regular(temporary)
    offset = temporary.stat().st_size if temporary.exists() else 0
    if offset > asset.size or (offset == asset.size and _sha256(temporary) != asset.sha256):
        temporary.unlink()
        offset = 0
    if offset == asset.size:
        os.replace(temporary, destination)
        emit("ready", asset.size)
        return
    headers = {"User-Agent": f"potbot/{__version__}", "Accept-Encoding": "identity"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(asset.url, headers=headers)
    emit("resuming" if offset else "downloading", offset)
    try:
        with _open_download(request) as response:
            if not response.geturl().startswith("https://"):
                raise ModelUnavailableError("Model download redirected outside HTTPS.")
            status = response.status
            if status == 206:
                match = re.fullmatch(
                    r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", "")
                )
                if (
                    match is None
                    or int(match[1]) != offset
                    or int(match[2]) != asset.size - 1
                    or int(match[3]) != asset.size
                ):
                    raise ModelUnavailableError("Model server returned an invalid resume range.")
            elif status == 200:
                if offset:
                    offset = 0
                    emit("restarting", 0)
            else:
                raise ModelUnavailableError("Model server did not return a downloadable file.")
            flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            flags |= os.O_APPEND if offset else os.O_TRUNC
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                if not stat.S_ISREG(os.fstat(output.fileno()).st_mode):
                    raise InvalidInputError("Partial model download must be an ordinary file.")
                while offset < asset.size:
                    data = response.read(min(CHUNK_BYTES, asset.size - offset))
                    if not data:
                        raise ModelUnavailableError(
                            "Model download was interrupted; run again to resume."
                        )
                    output.write(data)
                    offset += len(data)
                    emit("downloading", offset)
                if response.read(1):
                    raise ModelUnavailableError("Model file exceeds its pinned size.")
                output.flush()
                os.fsync(output.fileno())
    except (OSError, urllib.error.URLError, http.client.HTTPException):
        raise ModelUnavailableError(
            "Model download failed; run again to resume retained partial files."
        ) from None
    emit("verifying", asset.size)
    if _sha256(temporary) != asset.sha256:
        temporary.unlink(missing_ok=True)
        raise ModelUnavailableError(
            "Model file failed its pinned SHA256 check; no model was published."
        )
    os.replace(temporary, destination)
    emit("ready", asset.size)


def ensure_standard_model(
    root: Path,
    *,
    model_dir: Path | None = None,
    progress: Callable[[DownloadProgress], None] | None = None,
) -> dict[str, Any]:
    """Ensure the active pinned model exists, resuming partial downloads safely.

    Explicit relative model paths are relative to the caller's current directory;
    the default is always the supplied project's models directory. This operation
    never reads Hugging Face/global credential caches or changes the encoder profile.
    """
    directory = _target(root, model_dir)
    directory.mkdir(parents=True, exist_ok=True)
    callback = progress if progress is not None else (lambda event: None)
    with _download_lock(directory):
        _check_directory_owner(directory)
        completed = 0
        for index, asset in enumerate(QWEN_ASSETS, 1):
            _download_asset(directory, asset, index, completed, callback)
            completed += asset.size
        manifest = _manifest()
        _write_json(directory / MANIFEST, manifest)
        return manifest


def dependency_status(root: Path, *, model_dir: Path | None = None) -> dict[str, Any]:
    """Inspect installed dependencies and pinned model data without networking."""
    directory = _target(root, model_dir)
    packages: dict[str, str | None] = {}
    for name in ("potbot", "psycopg", "jsonschema", "keyring", "transformers", "torch"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    state = "missing"
    if directory.exists():
        try:
            state = (
                "ready"
                if (
                    _read_json(directory / MANIFEST) == _manifest()
                    and all(_matches(directory / asset.name, asset) for asset in QWEN_ASSETS)
                )
                else "incomplete"
            )
        except (DomainError, OSError):
            state = "invalid"
    return {
        "schema_version": 1,
        "python": platform.python_version(),
        "packages": packages,
        "model": {
            "name": QWEN_MODEL,
            "revision": QWEN_REVISION,
            "path": str(directory),
            "status": state,
            "total_bytes": sum(item.size for item in QWEN_ASSETS),
        },
        "platform": platform.system(),
        "host_tools": {
            name: shutil.which(name) is not None for name in ("git", "bwrap", "prlimit")
        },
        "execution_backend": "Linux Bubblewrap/libseccomp with permitted namespaces required",
        "postgres_service": "An existing PostgreSQL/pgvector service must be configured separately.",
        "package_installation": "Use start.bat, start.ps1, or start.sh for locked uv installation of all Python extras.",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="potbot dependencies",
        description=__doc__,
        epilog="Examples: start.bat dependencies status; ./start.sh dependencies download; "
        "./start.sh dependencies download --model-dir /path/to/models/qwen",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "download"):
        command = commands.add_parser(name)
        command.add_argument("--project-root", type=Path, default=project_root())
        command.add_argument("--model-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "download":
            console = ConsoleProgress()
            from potbot.api.observability import EventLogger, TransferProgress

            events = EventLogger(log_path=Path(os.environ["POTBOT_LOG_FILE"])
                                 if os.environ.get("POTBOT_LOG_FILE") else None)
            telemetry = TransferProgress(events)

            def progress(event: DownloadProgress) -> None:
                console(event)
                telemetry(event)

            events.emit("model.install.start", model=QWEN_MODEL)
            manifest = ensure_standard_model(
                args.project_root, model_dir=args.model_dir, progress=progress
            )
            events.emit("model.install.completed", model=QWEN_MODEL, verified=True)
            result = {
                "schema_version": 1,
                "model_dir": str(_target(args.project_root, args.model_dir)),
                "status": "ready",
                "manifest": manifest,
            }
        else:
            result = dependency_status(args.project_root, model_dir=args.model_dir)
        print(json.dumps(result, indent=2))
        return 0
    except DomainError as error:
        print(json.dumps({"error": error.to_dict()}), file=sys.stderr)
        return 2
    except OSError:
        print(
            json.dumps(
                {
                    "error": {
                        "code": "model_unavailable",
                        "message": "Local model storage is unavailable.",
                    }
                }
            ),
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        print(
            "Model download cancelled; completed and partial files are retained for resume.",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
