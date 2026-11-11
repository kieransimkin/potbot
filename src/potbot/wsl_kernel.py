"""Download and verify an optional WSL kernel bundle against independent release pins."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import stat
import time
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, BinaryIO

from potbot.domain.errors import (
    CapabilityUnavailableError,
    DomainError,
    InvalidInputError,
    ScopeDeniedError,
    SourceUnavailableError,
    StateMismatchError,
)
from potbot.tools.temporary import TemporaryDirectory

PROFILE = "potbot.wsl-kernel-bundle/1"
MANIFEST = "manifest.json"
SIGNATURE = "manifest.ed25519"
SOURCE_REPOSITORY = "microsoft/WSL2-Linux-Kernel"
ARTIFACT_LIMITS = {
    "bzImage": 128 * 1024**2,
    "modules.vhdx": 2 * 1024**3,
    "kernel.config": 2 * 1024**2,
    "wsl-taskstats-pidns.patch": 2 * 1024**2,
}
_ARCHIVE_MEMBERS = {**ARTIFACT_LIMITS, MANIFEST: 8192, SIGNATURE: 128}
_ARCHIVE_LIMIT = sum(_ARCHIVE_MEMBERS.values()) + 65536
_RELEASE_ROOT = "https://github.com/kieransimkin/potbot/releases/download"
_ASSET_HOSTS = {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}
OPTIONS = ("CONFIG_TASKSTATS", "CONFIG_TASK_XACCT", "CONFIG_TASK_IO_ACCOUNTING")
_BLOCK = 1024**2


def _hex(value: object, length: int) -> bool:
    return isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None


def _reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@contextmanager
def open_bundle_file(path: Path, limit: int) -> Iterator[tuple[BinaryIO, int]]:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or _reparse(before) or not 0 < before.st_size <= limit:
        raise ScopeDeniedError(f"Kernel bundle requires a bounded regular file for {path.name}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as source:
        opened = os.fstat(source.fileno())
        path_identity = _file_identity(before)
        handle_identity = _file_identity(opened)
        # Windows' CRT handle stat and Python's path stat expose different IDs.
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size != before.st_size
            or (os.name != "nt" and handle_identity != path_identity)
        ):
            raise StateMismatchError(f"Kernel bundle file changed while opening {path.name}")
        yield source, before.st_size
        if (
            _file_identity(os.fstat(source.fileno())) != handle_identity
            or _file_identity(path.lstat()) != path_identity
        ):
            raise StateMismatchError(f"Kernel bundle file changed while reading {path.name}")


def read_bundle_file(path: Path, limit: int) -> bytes:
    with open_bundle_file(path, limit) as (source, size):
        value = source.read(size + 1)
        if len(value) != size:
            raise StateMismatchError(f"Kernel bundle file size changed for {path.name}")
        return value


def encode_manifest(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def parse_manifest(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
        canonical = encode_manifest(value)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise InvalidInputError("Invalid kernel bundle manifest JSON") from error
    if (
        type(value) is not dict
        or raw != canonical
        or set(value) != {"profile", "architecture", "kernel_release", "source", "artifacts"}
        or value["profile"] != PROFILE
        or value["architecture"] != "x86_64"
        or not isinstance(value["kernel_release"], str)
        or not re.fullmatch(r"[0-9][A-Za-z0-9_.+-]{0,63}", value["kernel_release"])
    ):
        raise InvalidInputError("Invalid x86-64 WSL kernel manifest")
    source = value["source"]
    if (
        type(source) is not dict
        or set(source) != {"repository", "commit", "archive_sha256"}
        or source["repository"] != SOURCE_REPOSITORY
        or not _hex(source["commit"], 40)
        or not _hex(source["archive_sha256"], 64)
    ):
        raise InvalidInputError("Invalid WSL kernel source identity")
    artifacts = value["artifacts"]
    if type(artifacts) is not dict or set(artifacts) != set(ARTIFACT_LIMITS):
        raise InvalidInputError("Kernel manifest must list exactly the four bundle files")
    for name, limit in ARTIFACT_LIMITS.items():
        record = artifacts[name]
        if (
            type(record) is not dict
            or set(record) != {"size", "sha256"}
            or type(record["size"]) is not int
            or not 0 < record["size"] <= limit
            or not _hex(record["sha256"], 64)
        ):
            raise InvalidInputError(f"Invalid kernel manifest entry for {name}")
    return value


def signing_message(raw: bytes) -> bytes:
    return PROFILE.encode("ascii") + b"\0" + raw


def verify_manifest(
    raw: bytes, signature: bytes, *, manifest_sha256: str, public_key: str
) -> dict[str, Any]:
    if not _hex(manifest_sha256, 64) or not _hex(public_key, 64):
        raise InvalidInputError("Select an exact manifest SHA-256 and raw Ed25519 public key")
    if hashlib.sha256(raw).hexdigest() != manifest_sha256:
        raise ScopeDeniedError("Kernel manifest differs from the independent selection")
    if re.fullmatch(rb"[0-9a-f]{128}", signature) is None:
        raise ScopeDeniedError("Invalid WSL kernel manifest signature encoding")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as error:
        raise CapabilityUnavailableError(
            "Install Potbot's security extra to verify WSL kernel bundles"
        ) from error
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key)).verify(
            bytes.fromhex(signature.decode("ascii")), signing_message(raw)
        )
    except (InvalidSignature, ValueError) as error:
        raise ScopeDeniedError("WSL kernel manifest signature verification failed") from error
    return parse_manifest(raw)


def _verify_artifact(path: Path, record: dict[str, Any], prefix_size: int = 0) -> bytes:
    expected_size = record["size"]
    with open_bundle_file(path, expected_size) as (source, size):
        if size != expected_size:
            raise ScopeDeniedError(f"Kernel bundle size differs for {path.name}")
        prefix = source.read(prefix_size)
        digest = hashlib.sha256(prefix)
        remaining = size - len(prefix)
        while remaining:
            block = source.read(min(remaining, _BLOCK))
            if not block:
                raise StateMismatchError(f"Kernel bundle file was truncated: {path.name}")
            digest.update(block)
            remaining -= len(block)
        if source.read(1) or digest.hexdigest() != record["sha256"]:
            raise ScopeDeniedError(f"Kernel bundle digest differs for {path.name}")
        return prefix


def _verify_kernel(header: bytes, release: str) -> None:
    if (
        len(header) < 0x238
        or header[0x1FE:0x200] != b"\x55\xaa"
        or header[0x202:0x206] != b"HdrS"
        or not header[0x211] & 1  # LOADED_HIGH, the bzImage flag
        or not header[0x236] & 1  # XLF_KERNEL_64
    ):
        raise InvalidInputError("Bundle kernel is not an x86-64 bzImage")
    offset = int.from_bytes(header[0x20E:0x210], "little")
    version_bytes, terminator, _ = header[0x200 + offset :].partition(b"\0")
    version = version_bytes.split()
    if not offset or not terminator or not version or version[0] != release.encode("ascii"):
        raise ScopeDeniedError("bzImage kernel release differs from the signed manifest")


def _verify_config(config: bytes) -> None:
    lines = config.splitlines()
    for option in OPTIONS:
        name = option.encode("ascii")
        entries = [
            line
            for line in lines
            if line.startswith(name + b"=") or line == b"# " + name + b" is not set"
        ]
        if entries != [name + b"=y"]:
            raise ScopeDeniedError(f"Kernel config must enable {option}")


def verify_artifacts(bundle: Path, manifest: dict[str, Any]) -> None:
    artifacts = manifest["artifacts"]
    _verify_kernel(
        _verify_artifact(bundle / "bzImage", artifacts["bzImage"], 0x10400),
        manifest["kernel_release"],
    )
    if _verify_artifact(bundle / "modules.vhdx", artifacts["modules.vhdx"], 8) != b"vhdxfile":
        raise InvalidInputError("Bundle modules file is not a VHDX image")
    _verify_config(
        _verify_artifact(
            bundle / "kernel.config", artifacts["kernel.config"], artifacts["kernel.config"]["size"]
        )
    )
    _verify_artifact(bundle / "wsl-taskstats-pidns.patch", artifacts["wsl-taskstats-pidns.patch"])


def verify_bundle(bundle: Path, *, manifest_sha256: str, public_key: str) -> dict[str, Any]:
    bundle = bundle.absolute()
    info = bundle.lstat()
    if not stat.S_ISDIR(info.st_mode) or _reparse(info) or bundle.resolve(strict=True) != bundle:
        raise ScopeDeniedError("Kernel bundle requires a directory without redirected paths")
    raw = read_bundle_file(bundle / MANIFEST, 8192)
    manifest = verify_manifest(
        raw,
        read_bundle_file(bundle / SIGNATURE, 128),
        manifest_sha256=manifest_sha256,
        public_key=public_key,
    )
    verify_artifacts(bundle, manifest)
    return {
        "profile": "potbot.wsl-kernel-verification/1",
        "bundle_verified": True,
        "architecture": manifest["architecture"],
        "kernel_release": manifest["kernel_release"],
        "manifest_sha256": manifest_sha256,
        "release_key_sha256": hashlib.sha256(bytes.fromhex(public_key)).hexdigest(),
        "source": manifest["source"],
        "activation": "not_attempted",
    }


class _AssetRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        target = urllib.parse.urlsplit(newurl)
        if (
            target.scheme != "https"
            or target.hostname not in _ASSET_HOSTS
            or target.username is not None
            or target.password is not None
            or target.port not in {None, 443}
            or target.fragment
        ):
            raise ScopeDeniedError("Untrusted Potbot release asset redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_archive(url: str, stream: BinaryIO, expected_digest: str) -> None:
    opener = urllib.request.build_opener(_AssetRedirect())
    request = urllib.request.Request(
        url, headers={"Accept-Encoding": "identity", "User-Agent": "potbot-wsl-kernel/1"}
    )
    digest = hashlib.sha256()
    total = 0
    deadline = time.monotonic() + 600
    try:
        with opener.open(request, timeout=20) as response:
            if (
                response.status != 200
                or response.headers.get("Content-Encoding", "identity") != "identity"
            ):
                raise SourceUnavailableError(
                    "Potbot kernel download did not return an unencoded asset"
                )
            length = response.headers.get("Content-Length")
            if length is not None and (
                not length.isascii() or not length.isdecimal() or len(length) > 10
            ):
                raise InvalidInputError("Invalid Potbot kernel download size")
            limit = _ARCHIVE_LIMIT if length is None else int(length)
            if not 0 < limit <= _ARCHIVE_LIMIT:
                raise InvalidInputError("Potbot kernel download exceeds its byte bound")
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError("Potbot kernel download deadline")
                block = response.read1(min(_BLOCK, limit - total + 1))
                if not block:
                    break
                total += len(block)
                if total > limit:
                    raise InvalidInputError("Potbot kernel download exceeds its byte bound")
                digest.update(block)
                stream.write(block)
    except (OSError, http.client.HTTPException) as error:
        raise SourceUnavailableError(
            "Selected Potbot kernel release asset could not be downloaded"
        ) from error
    if length is not None and total != limit:
        raise SourceUnavailableError("Potbot kernel download ended before its declared size")
    if digest.hexdigest() != expected_digest:
        raise ScopeDeniedError("Potbot kernel archive differs from the independent selection")
    stream.seek(0)


def _unpack_archive(stream: BinaryIO, output: Path, rollback: ExitStack) -> None:
    with zipfile.ZipFile(stream) as archive:
        members = archive.infolist()
        if len(members) != len(_ARCHIVE_MEMBERS) or {
            member.orig_filename for member in members
        } != set(_ARCHIVE_MEMBERS):
            raise InvalidInputError("Kernel ZIP must contain exactly the six bundle files")
        for member in members:
            name = member.filename
            if (
                member.compress_type != zipfile.ZIP_STORED
                or member.compress_size != member.file_size
                or not 0 < member.file_size <= _ARCHIVE_MEMBERS[name]
            ):
                raise InvalidInputError(f"Invalid kernel ZIP member: {name}")
            path = output / name
            with archive.open(member) as entry, path.open("xb") as target:
                rollback.callback(path.unlink)
                while block := entry.read(_BLOCK):
                    target.write(block)


def download_bundle(
    output: Path,
    *,
    tag: str,
    kernel_release: str,
    archive_sha256: str,
    manifest_sha256: str,
    public_key: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:\.dev[0-9]+)?", tag) or len(tag) > 48:
        raise InvalidInputError("Select an exact Potbot release tag")
    if not re.fullmatch(r"[0-9][A-Za-z0-9_.+-]{0,63}", kernel_release):
        raise InvalidInputError("Select an exact WSL kernel release")
    if not all(_hex(value, 64) for value in (archive_sha256, manifest_sha256, public_key)):
        raise InvalidInputError(
            "Select exact archive and manifest SHA-256 pins and a raw Ed25519 key"
        )
    output = output.absolute()
    if output.parent.resolve(strict=True) != output.parent:
        raise ScopeDeniedError("Kernel output requires an existing parent without redirected paths")
    name = f"potbot-wsl-kernel-{kernel_release}-x86_64.zip"
    url = f"{_RELEASE_ROOT}/{tag}/{urllib.parse.quote(name)}"
    with ExitStack() as rollback:
        output.mkdir(mode=0o777 if os.name == "nt" else 0o700)
        rollback.callback(output.rmdir)
        with TemporaryDirectory(dir=output.parent) as temporary:
            with (Path(temporary) / "bundle.zip").open("x+b") as stream:
                _download_archive(url, stream, archive_sha256)
                _unpack_archive(stream, output, rollback)
        receipt = verify_bundle(output, manifest_sha256=manifest_sha256, public_key=public_key)
        if receipt["kernel_release"] != kernel_release:
            raise ScopeDeniedError("Kernel bundle release differs from the independent selection")
        rollback.pop_all()
    return {**receipt, "potbot_tag": tag, "archive_sha256": archive_sha256}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="potbot wsl-kernel", description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    verify = actions.add_parser("verify", help="verify the signed bundle on disk")
    verify.add_argument("--bundle", type=Path, required=True)
    download = actions.add_parser(
        "download", help="download and verify a pinned Potbot release asset"
    )
    download.add_argument("--tag", required=True)
    download.add_argument("--kernel-release", required=True)
    download.add_argument("--archive-sha256", required=True)
    download.add_argument(
        "--output", type=Path, required=True, help="fresh verified bundle directory"
    )
    for action in (verify, download):
        action.add_argument("--manifest-sha256", required=True)
        action.add_argument(
            "--public-key", required=True, help="independently trusted raw Ed25519 key, in hex"
        )
    args = parser.parse_args(argv)
    try:
        if args.action == "verify":
            receipt = verify_bundle(
                args.bundle, manifest_sha256=args.manifest_sha256, public_key=args.public_key
            )
        else:
            receipt = download_bundle(
                args.output,
                tag=args.tag,
                kernel_release=args.kernel_release,
                archive_sha256=args.archive_sha256,
                manifest_sha256=args.manifest_sha256,
                public_key=args.public_key,
            )
    except zipfile.BadZipFile:
        print(json.dumps({"code": "invalid_input", "message": "Invalid kernel ZIP archive"}))
        return 2
    except DomainError as error:
        print(json.dumps(error.to_dict(), sort_keys=True))
        return 2
    except (OSError, RuntimeError):
        print(
            json.dumps(
                {"code": "bundle_unavailable", "message": "Kernel bundle files could not be read"}
            )
        )
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return 0
