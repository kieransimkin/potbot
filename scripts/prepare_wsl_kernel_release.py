#!/usr/bin/env python3
"""Prepare a WSL kernel release for external signing, then verify and package it."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import tarfile
import zipfile
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import IO, Any, BinaryIO

from potbot.domain.errors import (
    DomainError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.wsl_kernel import (
    ARTIFACT_LIMITS,
    MANIFEST,
    PROFILE,
    SIGNATURE,
    SOURCE_REPOSITORY,
    encode_manifest,
    open_bundle_file,
    parse_manifest,
    read_bundle_file,
    signing_message,
    verify_artifacts,
    verify_manifest,
)

SOURCE = "source.tar.gz"
SOURCE_LIMIT = 1024**3
SIGNING_INPUT = "manifest.to-sign"


@contextmanager
def fresh_output(path: Path) -> Iterator[tuple[Path, ExitStack]]:
    output = path.absolute()
    if output.parent.resolve(strict=True) != output.parent:
        raise InvalidInputError("Output must have an existing, nonredirected parent directory")
    output.mkdir()
    with ExitStack() as rollback:
        rollback.callback(output.rmdir)
        yield output, rollback
        rollback.pop_all()


@contextmanager
def new_file(path: Path, rollback: ExitStack) -> Iterator[BinaryIO]:
    with path.open("xb") as output:
        rollback.callback(path.unlink)
        yield output


def stream_asset(path: Path, output: IO[bytes] | None, limit: int) -> dict[str, Any]:
    digest = hashlib.sha256()
    with open_bundle_file(path, limit) as (source, size):
        remaining = size
        while remaining:
            block = source.read(min(remaining, 1024**2))
            if not block:
                raise StateMismatchError(f"Release input was truncated: {path.name}")
            digest.update(block)
            if output is not None:
                output.write(block)
            remaining -= len(block)
        if source.read(1):
            raise StateMismatchError(f"Release input grew: {path.name}")
    return {"size": size, "sha256": digest.hexdigest()}


def copy_asset(source: Path, target: Path, limit: int, rollback: ExitStack) -> dict[str, Any]:
    with new_file(target, rollback) as output:
        return stream_asset(source, output, limit)


def copy_source(
    source: Path, target: Path, *, commit: str, sha256: str, rollback: ExitStack
) -> dict[str, Any]:
    info = copy_asset(source, target, SOURCE_LIMIT, rollback)
    check_source(target, commit, sha256, info)
    return info


def check_source(path: Path, commit: str, sha256: str, info: dict[str, Any]) -> None:
    if info["sha256"] != sha256:
        raise ScopeDeniedError("Source archive differs from the selected SHA-256")
    with (
        open_bundle_file(path, SOURCE_LIMIT) as (stream, _),
        tarfile.open(fileobj=stream, mode="r|gz") as archive,
    ):
        root = archive.next()
        if root is None or not root.isdir() or root.name != f"WSL2-Linux-Kernel-{commit}":
            raise InvalidInputError(
                "Source archive root differs from the selected Microsoft commit"
            )


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    if (
        re.fullmatch(r"[0-9a-f]{40}", args.source_commit) is None
        or re.fullmatch(r"[0-9a-f]{64}", args.source_sha256) is None
        or re.fullmatch(r"[0-9][A-Za-z0-9_.+-]{0,63}", args.kernel_release) is None
    ):
        raise InvalidInputError(
            "Select an exact Microsoft commit, archive SHA-256, and kernel release"
        )
    with fresh_output(args.output) as (output, rollback):
        bundle = output / "bundle"
        bundle.mkdir()
        rollback.callback(bundle.rmdir)
        copy_source(
            args.source_archive,
            output / SOURCE,
            commit=args.source_commit,
            sha256=args.source_sha256,
            rollback=rollback,
        )
        artifacts = {
            name: copy_asset(args.components / name, bundle / name, limit, rollback)
            for name, limit in ARTIFACT_LIMITS.items()
        }
        manifest = encode_manifest(
            {
                "profile": PROFILE,
                "architecture": "x86_64",
                "kernel_release": args.kernel_release,
                "source": {
                    "repository": SOURCE_REPOSITORY,
                    "commit": args.source_commit,
                    "archive_sha256": args.source_sha256,
                },
                "artifacts": artifacts,
            }
        )
        verify_artifacts(bundle, parse_manifest(manifest))
        with new_file(bundle / MANIFEST, rollback) as output_file:
            output_file.write(manifest)
        with new_file(output / SIGNING_INPUT, rollback) as output_file:
            output_file.write(signing_message(manifest))
    return {
        "state": "prepared",
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "signing_input": SIGNING_INPUT,
    }


def zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


def review_signing_input(prepared: Path, raw: bytes) -> bytes:
    expected = signing_message(raw)
    actual = read_bundle_file(prepared / SIGNING_INPUT, len(expected))
    if actual != expected:
        raise ScopeDeniedError("Signing input differs from the reviewed manifest")
    return actual


def review(args: argparse.Namespace) -> dict[str, Any]:
    bundle = args.prepared / "bundle"
    raw = read_bundle_file(bundle / MANIFEST, 8192)
    if re.fullmatch(r"[0-9a-f]{64}", args.manifest_sha256) is None:
        raise InvalidInputError("Select an exact lowercase manifest SHA-256")
    if hashlib.sha256(raw).hexdigest() != args.manifest_sha256:
        raise ScopeDeniedError("Kernel manifest differs from the independent selection")
    manifest = parse_manifest(raw)
    message = review_signing_input(args.prepared, raw)
    verify_artifacts(bundle, manifest)
    source = args.prepared / SOURCE
    check_source(
        source,
        manifest["source"]["commit"],
        manifest["source"]["archive_sha256"],
        stream_asset(source, None, SOURCE_LIMIT),
    )
    return {
        "profile": "potbot.wsl-kernel-signing-review/1",
        "state": "ready_for_external_signing",
        "kernel_release": manifest["kernel_release"],
        "architecture": manifest["architecture"],
        "manifest_sha256": args.manifest_sha256,
        "signing_input": SIGNING_INPUT,
        "signing_input_size": len(message),
        "signing_input_sha256": hashlib.sha256(message).hexdigest(),
    }


def finalize(args: argparse.Namespace) -> dict[str, Any]:
    bundle = args.prepared / "bundle"
    raw = read_bundle_file(bundle / MANIFEST, 8192)
    if args.signature_format == "raw":
        raw_signature = read_bundle_file(args.signature, 64)
        if len(raw_signature) != 64:
            raise InvalidInputError("Expected a 64-byte detached Ed25519 signature")
        signature = raw_signature.hex().encode("ascii")
    else:
        signature = read_bundle_file(args.signature, 128)
    manifest = verify_manifest(
        raw, signature, manifest_sha256=args.manifest_sha256, public_key=args.public_key
    )
    review_signing_input(args.prepared, raw)
    verify_artifacts(bundle, manifest)
    prefix = f"potbot-wsl-kernel-{manifest['kernel_release']}-x86_64"
    zip_name = f"{prefix}.zip"
    source_name = f"{prefix}.source.tar.gz"
    with fresh_output(args.output) as (output, rollback):
        source = copy_source(
            args.prepared / SOURCE,
            output / source_name,
            commit=manifest["source"]["commit"],
            sha256=manifest["source"]["archive_sha256"],
            rollback=rollback,
        )
        with (
            new_file(output / zip_name, rollback) as archive_file,
            zipfile.ZipFile(archive_file, "w", compression=zipfile.ZIP_STORED) as archive,
        ):
            for name in sorted(ARTIFACT_LIMITS):
                expected = manifest["artifacts"][name]
                info = zip_info(name)
                info.file_size = expected["size"]
                with archive.open(info, "w") as entry:
                    if stream_asset(bundle / name, entry, expected["size"]) != expected:
                        raise ScopeDeniedError(f"Prepared bundle changed while packaging {name}")
            archive.writestr(zip_info(MANIFEST), raw)
            archive.writestr(zip_info(SIGNATURE), signature)
        packed = stream_asset(output / zip_name, None, sum(ARTIFACT_LIMITS.values()) + 65536)
        receipt = {
            "profile": "potbot.wsl-kernel-release-assets/1",
            "kernel_release": manifest["kernel_release"],
            "architecture": manifest["architecture"],
            "manifest_sha256": args.manifest_sha256,
            "release_key_sha256": hashlib.sha256(bytes.fromhex(args.public_key)).hexdigest(),
            "source": manifest["source"],
            "assets": {zip_name: packed, source_name: source},
        }
        with new_file(output / "release.json", rollback) as output_file:
            output_file.write(encode_manifest(receipt))
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    unsigned = actions.add_parser("prepare", help="stage artifacts and write the signing input")
    for option in ("components", "source-archive", "output"):
        unsigned.add_argument(f"--{option}", type=Path, required=True)
    for option in ("source-commit", "source-sha256", "kernel-release"):
        unsigned.add_argument(f"--{option}", required=True)
    preflight = actions.add_parser("review", help="check the exact bytes before external signing")
    preflight.add_argument("--prepared", type=Path, required=True)
    preflight.add_argument("--manifest-sha256", required=True)
    signed = actions.add_parser("finalize", help="verify an external signature and package assets")
    for option in ("prepared", "signature", "output"):
        signed.add_argument(f"--{option}", type=Path, required=True)
    for option in ("manifest-sha256", "public-key"):
        signed.add_argument(f"--{option}", required=True)
    signed.add_argument("--signature-format", choices=("hex", "raw"), default="hex")
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            receipt = prepare(args)
        elif args.action == "review":
            receipt = review(args)
        else:
            receipt = finalize(args)
    except DomainError as error:
        print(json.dumps(error.to_dict(), sort_keys=True))
        return 2
    except (OSError, RuntimeError, tarfile.TarError):
        print(
            json.dumps(
                {"code": "release_unavailable", "message": "Could not package release assets"}
            )
        )
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
