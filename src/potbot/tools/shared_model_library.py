"""Host-owned content-addressed model library and sandbox model staging intake.

Only a trusted operator may enroll exact source/revision/quantization/digests.
Guest writes are observations, not evidence sufficient for promotion.
No network, model execution, deserialization or arbitrary source imports occur.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

try:
    import fcntl
except ImportError:  # pragma: no cover - library executes in Linux/WSL operator only
    fcntl = None

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.tools.model_cache_routes import SHARED_MODELS_GUEST

_SHA = re.compile(r"[a-f0-9]{64}\Z")
_REV = re.compile(r"[a-f0-9]{40,64}\Z")
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}/[A-Za-z0-9][A-Za-z0-9._-]{0,100}\Z")
_QUANT = re.compile(r"(?:q[2-8]_[a-z0-9_]+|f16|bf16|fp32|int8|native)\Z")
_TICKET = re.compile(r"[a-f0-9]{32}\Z")
_ALLOWED_SUFFIX = frozenset({
    ".gguf", ".safetensors", ".onnx", ".json", ".model", ".tiktoken",
    ".txt", ".vocab", ".merges",
})
_IGNORED_SUFFIX = frozenset({".part", ".tmp", ".lock", ".incomplete", ".download"})


def _relative_file(name: str) -> tuple[str, ...]:
    if not isinstance(name, str) or not name or len(name.encode()) > 400:
        raise InvalidInputError("model file name must be a bounded relative path")
    segments = name.split("/")
    if (any(not s or s in {".", ".."} or s.startswith(".") or "\\" in s
            or "\x00" in s for s in segments) or len(segments) > 8):
        raise InvalidInputError("model file names must not traverse directories")
    if Path(name).suffix.lower() not in _ALLOWED_SUFFIX:
        raise InvalidInputError("unknown or executable model sidecar extension")
    return tuple(segments)


@dataclass(frozen=True, slots=True)
class ModelAsset:
    path: str
    sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        _relative_file(self.path)
        if not isinstance(self.sha256, str) or not _SHA.fullmatch(self.sha256):
            raise InvalidInputError("model asset must have an exact SHA-256")
        if type(self.byte_count) is not int or not 8 <= self.byte_count <= 2 * 1024**4:
            raise InvalidInputError("model asset byte count must be bounded")


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    """Exact source and immutable revision, supplied by a trusted download plan."""

    repository: str
    revision: str
    quantization: str
    format: Literal["gguf", "safetensors", "onnx"]

    def __post_init__(self) -> None:
        if not isinstance(self.repository, str) or not _REPO.fullmatch(self.repository):
            raise InvalidInputError("model needs publisher/repository identity")
        if not isinstance(self.revision, str) or not _REV.fullmatch(self.revision):
            raise InvalidInputError("model revision must be an immutable hexadecimal commit")
        if not isinstance(self.quantization, str) or not _QUANT.fullmatch(self.quantization):
            raise InvalidInputError("quantization must have an explicit supported name")
        if self.format not in {"gguf", "safetensors", "onnx"}:
            raise InvalidInputError("unknown model format")

    @property
    def key(self) -> str:
        namespace, repo = self.repository.casefold().split("/")
        return f"{namespace}--{repo}__r-{self.revision}__{self.quantization}"


@dataclass(frozen=True, slots=True)
class ModelDownloadTicket:
    """Trusted operator authorization; never read this from the guest staging area."""

    ticket_id: str
    identity: ModelIdentity
    assets: tuple[ModelAsset, ...]

    def __post_init__(self) -> None:
        if not _TICKET.fullmatch(self.ticket_id):
            raise InvalidInputError("download ticket requires a 32-character hexadecimal ID")
        if not isinstance(self.assets, tuple) or not 1 <= len(self.assets) <= 64:
            raise InvalidInputError("model download needs 1-64 assets")
        paths = [a.path for a in self.assets]
        if len(set(paths)) != len(paths):
            raise InvalidInputError("duplicate model asset paths")
        primary = "." + self.identity.format
        for asset in self.assets:
            suffix = Path(asset.path).suffix.lower()
            if suffix in {".gguf", ".safetensors", ".onnx"} and suffix != primary:
                raise InvalidInputError("model bundle mixes different primary weight formats")
        if not any(p.lower().endswith(primary) for p in paths):
            raise InvalidInputError("model bundle has no declared weight artifact")


@dataclass(frozen=True, slots=True)
class PromotedModel:
    key: str
    files: tuple[tuple[str, str], ...]
    manifest_sha256: str


class SharedModelLibrary:
    """Promote exact bytes after an operator-owned plan and create per-workspace links.

    Immutable blobs are shared globally; mutable hub caches are never shared.
    Promotion uses a controller-side advisory flock outside the sandbox. For
    hostile guests, OS file access must be mediated/frozen while hashing.
    """

    def __init__(self, *, shared_root: Path, lock_root: Path) -> None:
        if Path(shared_root).is_symlink() or Path(lock_root).is_symlink():
            raise ScopeDeniedError("controller library roots must not be symlinks")
        self.root = Path(shared_root).resolve(strict=True)
        self.locks = Path(lock_root).resolve(strict=True)
        if (not self.root.is_dir() or not self.locks.is_dir() or self.root == self.locks
                or self.root.is_relative_to(self.locks) or self.locks.is_relative_to(self.root)):
            raise ScopeDeniedError("model library and controller locks must have disjoint roots")
        if not os.access(self.root, os.W_OK) or not os.access(self.locks, os.W_OK):
            raise ScopeDeniedError("operator must control a writable library and lock directory")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        if os.name != "posix" or fcntl is None:
            raise ScopeDeniedError("model promotions require a Linux/WSL operator")
        descriptor = os.open(self.locks / "model-store.lock",
                             os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _digest_asset(path: Path, expected: ModelAsset, destination: Path) -> None:
        """Snapshot into controller-private destination while checking exact bytes."""
        source_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size != expected.byte_count:
                raise StateMismatchError("model source changed or remains incomplete")
            # Model files may contain code-like metadata but are never executed.
            out_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600)
            try:
                digest = hashlib.sha256()
                total = 0
                head = b""
                while data := os.read(source_fd, 2**20):
                    if not head:
                        head = data[:4096]
                    total += len(data)
                    if total > expected.byte_count:
                        raise StateMismatchError("model bytes increased while copying")
                    digest.update(data)
                    os.write(out_fd, data)
                after = os.fstat(source_fd)
                if (total != expected.byte_count or digest.hexdigest() != expected.sha256
                        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                    raise StateMismatchError("model bytes or file identity changed during snapshot")
                ext = path.suffix.lower()
                if ext == ".gguf" and not (head.startswith(b"GGUF") and len(head) >= 24
                                               and 2 <= int.from_bytes(head[4:8], "little") <= 3):
                    raise StateMismatchError("model does not have a supported GGUF header")
                if ext == ".safetensors":
                    length = int.from_bytes(head[:8], "little") if len(head) >= 8 else 0
                    if not 2 <= length <= 32 * 1024**2 or length + 8 >= expected.byte_count:
                        raise StateMismatchError("model has an invalid safetensors header length")
                    try:
                        header = json.loads(os.pread(source_fd, length, 8))
                    except (UnicodeDecodeError, ValueError) as exc:
                        raise StateMismatchError("model safetensors header is not JSON") from exc
                    if not isinstance(header, dict) or not header:
                        raise StateMismatchError("model safetensors header lacks tensor entries")
                    tensors = [v for k, v in header.items() if k != "__metadata__"]
                    if not tensors or any(not isinstance(v, dict) or not {
                        "dtype", "shape", "data_offsets"
                    } <= v.keys() for v in tensors):
                        raise StateMismatchError("model safetensors tensor metadata is invalid")
                os.fsync(out_fd)
            finally:
                os.close(out_fd)
        finally:
            os.close(source_fd)

    def promote(self, *, ticket: ModelDownloadTicket, staging: Path) -> PromotedModel:
        """Never infer model identity from filenames; enforce exact host ticket."""
        if Path(staging).is_symlink():
            raise ScopeDeniedError("staging root cannot be a symbolic link")
        staging = Path(staging).resolve(strict=True)
        if not staging.is_dir() or staging.is_relative_to(self.root) or self.root.is_relative_to(staging):
            raise ScopeDeniedError("staging area must be separate from the global model store")
        downloads = staging / "downloads"
        if downloads.is_symlink() or not downloads.is_dir():
            raise StateMismatchError("model staging downloads directory is missing or linked")
        bundle = downloads / ticket.ticket_id
        if bundle.is_symlink() or not bundle.is_dir():
            raise StateMismatchError("download ticket is not an admitted staging directory")
        included = {asset.path for asset in ticket.assets}
        for asset in ticket.assets:
            if not asset.path.endswith(".safetensors.index.json"):
                continue
            path = bundle / asset.path
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 8_388_608:
                raise StateMismatchError("model sharded index is missing or not a regular file")
            index = json.loads(path.read_bytes())
            if not isinstance(index, dict) or not isinstance(index.get("weight_map"), dict):
                raise StateMismatchError("model shard index requires a tensor weight map")
            required = set(index["weight_map"].values())
            if not required or any(not isinstance(v, str) for v in required) or not required <= included:
                raise StateMismatchError("model index references shards outside admitted bundle")
        payload = {
            "schema": "potbot-model-bundle/v1", "repository": ticket.identity.repository,
            "revision": ticket.identity.revision, "quantization": ticket.identity.quantization,
            "format": ticket.identity.format,
            "assets": [{"path": a.path, "sha256": a.sha256, "bytes": a.byte_count}
                       for a in ticket.assets],
        }
        serialized = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        receipt_sha = hashlib.sha256(serialized).hexdigest()
        with self._locked():
            blobs = self.root / "blobs"
            manifests = self.root / "manifests"
            if blobs.is_symlink() or manifests.is_symlink():
                raise ScopeDeniedError("global model metadata paths cannot be symlinks")
            blobs.mkdir(mode=0o755, exist_ok=True)
            manifests.mkdir(mode=0o755, exist_ok=True)
            linked: list[tuple[str, str]] = []
            for asset in ticket.assets:
                parts = _relative_file(asset.path)
                source = bundle
                for segment in parts:
                    source = source / segment
                    if source.is_symlink():
                        raise ScopeDeniedError("symbolic links from sandbox staging are forbidden")
                if not source.is_file():
                    raise StateMismatchError("declared model file missing")
                blob_dir = blobs / asset.sha256[:2]
                if blob_dir.is_symlink():
                    raise ScopeDeniedError("global blob shard directory is a symlink")
                blob_dir.mkdir(mode=0o755, exist_ok=True)
                final = blob_dir / asset.sha256
                if final.is_symlink():
                    raise ScopeDeniedError("global model blob path is a symlink")
                if final.exists():
                    self._verify_existing(final, asset)
                else:
                    temporary = blob_dir / (".promote-" + ticket.ticket_id + "-" + asset.sha256)
                    try:
                        self._digest_asset(source, asset, temporary)
                        temporary.chmod(0o444)
                        os.link(temporary, final, follow_symlinks=False)
                        directory_fd = os.open(blob_dir, os.O_RDONLY | os.O_DIRECTORY)
                        try:
                            os.fsync(directory_fd)
                        finally:
                            os.close(directory_fd)
                    finally:
                        temporary.unlink(missing_ok=True)
                linked.append((asset.path, f"{SHARED_MODELS_GUEST}/blobs/{asset.sha256[:2]}/{asset.sha256}"))
            manifest_path = manifests / (ticket.identity.key + ".json")
            if manifest_path.exists():
                if manifest_path.read_bytes() != serialized:
                    raise StateMismatchError("conflicting canonical model identity/quantization")
            else:
                pending = manifests / ("." + ticket.ticket_id + ".part")
                try:
                    with pending.open("xb") as stream:
                        stream.write(serialized)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(pending, manifest_path)
                finally:
                    pending.unlink(missing_ok=True)
            return PromotedModel(ticket.identity.key, tuple(linked), receipt_sha)

    @staticmethod
    def _verify_existing(path: Path, expected: ModelAsset) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size != expected.byte_count:
                raise StateMismatchError("global model blob differs from enrolled bytes")
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 2**20):
                digest.update(chunk)
            if digest.hexdigest() != expected.sha256:
                raise StateMismatchError("global model blob checksum does not match")
        finally:
            os.close(descriptor)

    def link_workspace(self, *, staging: Path, model: PromotedModel) -> Path:
        """Guest-visible absolute links to the rootfs's read-only global mount."""
        if Path(staging).is_symlink():
            raise ScopeDeniedError("staging root cannot be a symbolic link")
        staging = Path(staging).resolve(strict=True)
        if staging.is_relative_to(self.root) or self.root.is_relative_to(staging):
            raise ScopeDeniedError("cannot link into global model library")
        # A caller-provided receipt is not authority to create arbitrary links.
        # Read exact approved asset identities from the controller library first.
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]+--[a-z0-9][a-z0-9._-]+__r-[a-f0-9]{40,64}__(?:q[2-8]_[a-z0-9_]+|f16|bf16|fp32|int8|native)", model.key):
            raise InvalidInputError("invalid model release identity")
        manifest_file = self.root / "manifests" / (model.key + ".json")
        if manifest_file.is_symlink() or not manifest_file.is_file():
            raise StateMismatchError("unknown model release: no authoritative manifest")
        manifest_bytes = manifest_file.read_bytes()
        if hashlib.sha256(manifest_bytes).hexdigest() != model.manifest_sha256:
            raise StateMismatchError("model release manifest changed or mismatched")
        document = json.loads(manifest_bytes)
        approved_files = tuple((item["path"],
                                f"{SHARED_MODELS_GUEST}/blobs/{item['sha256'][:2]}/{item['sha256']}")
                               for item in document["assets"])
        if model.files != approved_files:
            raise ScopeDeniedError("requested model links differ from approved asset manifest")
        with self._locked():
            model_root = staging / "models"
            if model_root.is_symlink():
                raise ScopeDeniedError("model link root cannot be a symbolic link")
            parent = model_root / model.key
            if parent.is_symlink():
                raise ScopeDeniedError("model link destination is a symlink")
            parent.mkdir(parents=True, exist_ok=True)
            for relative, target in model.files:
                segments = _relative_file(relative)
                destination = parent
                for segment in segments[:-1]:
                    destination = destination / segment
                    if destination.is_symlink():
                        raise ScopeDeniedError("model link parent is a symlink")
                    destination.mkdir(exist_ok=True)
                file = destination / segments[-1]
                if file.is_symlink():
                    if os.readlink(file) != target:
                        raise StateMismatchError("model link occupied by different target")
                elif file.exists():
                    raise StateMismatchError("model link occupied by sandbox-controlled file")
                else:
                    file.symlink_to(target)
            return parent


@dataclass(frozen=True, slots=True)
class ObservedModelFile:
    """Tentative file observation; not a verified or installable model."""

    path: str
    format: str
    byte_count: int
    mtime_ns: int
    possible_repository: str | None
    possible_revision: str | None
    state: Literal["settling", "needs_ticket"]


class ModelDownloadWatcher:
    """Filesystem-change observer with stable-size detection, for the operator.

    Call poll after a Linux inotify CLOSE_WRITE/MOVED_TO event. Periodic polling
    remains required on WSL hosts where notifications can be lost. The watcher
    never authorizes promotions or follows guest-created symbolic links.
    """

    def __init__(self, staging: Path, *, max_entries: int = 10000) -> None:
        self.staging = Path(staging).resolve(strict=True)
        if not self.staging.is_dir() or self.staging.is_symlink():
            raise ScopeDeniedError("model watcher requires a trusted staging directory")
        if type(max_entries) is not int or not 1 <= max_entries <= 100000:
            raise InvalidInputError("invalid model watcher scan bound")
        self.max_entries = max_entries
        self._last: dict[str, tuple[int, int, int, int]] = {}

    @staticmethod
    def _hint(parts: tuple[str, ...]) -> tuple[str | None, str | None]:
        for index, item in enumerate(parts):
            # HF native cache paths contain repo and exact revision, but neither
            # the path nor a named "main" ref is authoritative. Host ticket
            # must still specify immutable origin/revision and each file digest.
            if item.startswith("models--") and index + 2 < len(parts):
                chunks = item.split("--")
                if len(chunks) != 3:
                    continue
                if parts[index + 1] != "snapshots":
                    continue
                revision = parts[index + 2]
                if _REV.fullmatch(revision):
                    return f"{chunks[1]}/{chunks[2]}", revision
        return None, None

    def poll(self) -> tuple[ObservedModelFile, ...]:
        """Classify newly discovered stable model weight files; no downloads."""
        current: dict[str, tuple[int, int, int, int]] = {}
        result: list[ObservedModelFile] = []
        stack = [(self.staging, 0)]
        seen = 0
        while stack:
            directory, depth = stack.pop()
            if depth > 12:
                continue
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    seen += 1
                    if seen > self.max_entries:
                        raise StateMismatchError("model staging scan exceeds its bound")
                    if entry.is_symlink():
                        # HF snapshots commonly point at content-addressed
                        # cache blobs, so don't silently miss these downloads.
                        # Never follow an arbitrary symlink outside /var/ai.
                        if "models--" not in entry.path or "/snapshots/" not in entry.path:
                            continue
                        try:
                            resolved = Path(entry.path).resolve(strict=True)
                            if not resolved.is_relative_to(self.staging) or not resolved.is_file():
                                continue
                            info = resolved.stat()
                        except (OSError, RuntimeError):
                            continue
                        suffix = Path(entry.name.casefold()).suffix
                        if suffix not in {".gguf", ".safetensors", ".onnx"}:
                            continue
                        rel = Path(entry.path).relative_to(self.staging).as_posix()
                        signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                        current[rel] = signature
                        repo, revision = self._hint(tuple(rel.split("/")))
                        result.append(ObservedModelFile(
                            rel, suffix.lstrip("."), info.st_size, info.st_mtime_ns, repo, revision,
                            "needs_ticket" if self._last.get(rel) == signature else "settling",
                        ))
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((Path(entry.path), depth + 1))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    name = entry.name.casefold()
                    suffix = Path(name).suffix
                    is_ollama_blob = ("/ollama/models/blobs/" in entry.path
                                      and re.fullmatch(r"sha256-[a-f0-9]{64}", name) is not None)
                    if suffix in _IGNORED_SUFFIX or name.startswith(".") or (
                        suffix not in {".gguf", ".safetensors", ".onnx"}
                        and not is_ollama_blob
                    ):
                        continue
                    info = entry.stat(follow_symlinks=False)
                    rel = Path(entry.path).relative_to(self.staging).as_posix()
                    signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                    current[rel] = signature
                    parts = tuple(rel.split("/"))
                    repo, revision = self._hint(parts)
                    result.append(ObservedModelFile(
                        rel, "ollama-blob" if is_ollama_blob else suffix.lstrip("."),
                        info.st_size, info.st_mtime_ns, repo, revision,
                        "needs_ticket" if self._last.get(rel) == signature else "settling",
                    ))
        self._last = current
        return tuple(sorted(result, key=lambda item: item.path))


class ModelFilesystemNotifier:
    """Best-effort Linux/WSL inotify wakeups plus mandatory periodic rescan.

    Events accelerate discovering completed writes but NEVER establish trust or
    completion. Polling remains necessary for missed notifications, WSL mounts,
    directory races and inotify watch limits. Use on the guest's Linux filesystem.
    """

    _MASK = 0x00000008 | 0x00000080 | 0x00000100 | 0x00000200  # close_write/move/create/delete

    def __init__(self, staging: Path, *, max_watches: int = 4096) -> None:
        import ctypes
        import ctypes.util

        self.root = Path(staging).resolve(strict=True)
        self._fd = -1
        self._tracked: set[Path] = set()
        self.max_watches = max_watches
        if os.name != "posix":
            return
        name = ctypes.util.find_library("c")
        if not name:
            return
        libc = ctypes.CDLL(name, use_errno=True)
        init = getattr(libc, "inotify_init1", None)
        add = getattr(libc, "inotify_add_watch", None)
        if init is None or add is None:
            return
        init.argtypes = [ctypes.c_int]
        init.restype = ctypes.c_int
        add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        add.restype = ctypes.c_int
        self._fd = init(os.O_NONBLOCK | os.O_CLOEXEC)
        self._add = add
        if self._fd >= 0:
            self.refresh()

    @property
    def enabled(self) -> bool:
        return self._fd >= 0

    def refresh(self) -> None:
        if self._fd < 0:
            return
        directories = [self.root]
        count = 0
        while directories:
            current = directories.pop()
            if current in self._tracked or current.is_symlink():
                continue
            count += 1
            if count + len(self._tracked) > self.max_watches:
                return  # bounded fallback to polling
            if self._add(self._fd, os.fsencode(current), self._MASK) >= 0:
                self._tracked.add(current)
            try:
                with os.scandir(current) as entries:
                    directories.extend(Path(entry.path) for entry in entries
                                       if entry.is_dir(follow_symlinks=False))
            except (OSError, PermissionError):
                pass

    def wait(self, interval: float) -> None:
        import select

        if not 0.1 <= interval <= 3600:
            raise InvalidInputError("model watcher interval outside allowed bounds")
        if self._fd < 0:
            import time
            time.sleep(interval)
            return
        ready, _, _ = select.select([self._fd], [], [], interval)
        if ready:
            try:
                os.read(self._fd, 262144)
            except BlockingIOError:
                pass
            self.refresh()

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> ModelFilesystemNotifier:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def configured_shared_model_root(environment: dict[str, str]) -> Path:
    """Single host-owned store used by base Potbot and all sandbox read-only mounts.

    This is a controller setting, never an environment variable accepted from an
    agent/tool request. Provision the directory beforehand with trusted ownership.
    """
    value = environment.get("POTBOT_GLOBAL_MODELS_ROOT")
    if not isinstance(value, str) or not value.startswith("/"):
        raise InvalidInputError("operator must explicitly set POTBOT_GLOBAL_MODELS_ROOT")
    raw = Path(value)
    if raw.is_symlink() or ".." in raw.parts:
        raise ScopeDeniedError("global model root must not traverse or follow symlinks")
    resolved = raw.resolve(strict=True)
    if not resolved.is_dir() or resolved == Path("/"):
        raise ScopeDeniedError("configured model root must be a concrete existing directory")
    return resolved
