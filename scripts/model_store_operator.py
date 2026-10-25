#!/usr/bin/env python3
"""Operator-only model download intake, classification and promotion.

No credentials are passed into sandboxes. Manifests are supplied by the trusted
operator, NEVER read from /var/ai or generated from model-provided filenames.
Use `observe` periodically or `watch` to supervise a guest's /var/ai staging.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from potbot.tools.shared_model_library import (
    ModelAsset,
    ModelDownloadTicket,
    ModelDownloadWatcher,
    ModelFilesystemNotifier,
    ModelIdentity,
    SharedModelLibrary,
)


def _ticket(path: Path, staging: Path, shared: Path) -> ModelDownloadTicket:
    # A forged file in a writable guest location must not become an authorized
    # model plan. The caller's trusted host process supplies this private file.
    source = path.resolve(strict=True)
    if source.is_relative_to(staging.resolve(strict=True)) or source.is_relative_to(
        shared.resolve(strict=True)
    ) or path.is_symlink():
        raise ValueError("model ticket cannot be guest-owned or a symlink")
    doc: Any = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or set(doc) != {"schema", "ticket_id", "model", "files"}:
        raise ValueError("operator download ticket does not match schema")
    if doc["schema"] != "potbot-model-download-ticket/v1":
        raise ValueError("unknown operator download ticket")
    model = doc["model"]
    if not isinstance(model, dict) or set(model) != {
        "repository", "revision", "quantization", "format"
    }:
        raise ValueError("model identity must be complete and exact")
    if not isinstance(doc["files"], list):
        raise ValueError("model assets must be an array")
    return ModelDownloadTicket(
        doc["ticket_id"], ModelIdentity(**model),
        tuple(ModelAsset(item["path"], item["sha256"], item["bytes"])
              for item in doc["files"]),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("observe", "watch", "promote"))
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--shared-root", type=Path)
    parser.add_argument("--lock-root", type=Path)
    parser.add_argument("--ticket", type=Path)
    parser.add_argument("--tickets-dir", type=Path,
                        help="operator-private enrolled tickets for automatic verified promotion")
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()
    try:
        if args.action == "promote":
            if not args.ticket or not args.shared_root or not args.lock_root:
                raise ValueError("promotion requires --ticket, --shared-root and --lock-root")
            model = _ticket(args.ticket, args.staging, args.shared_root)
            library = SharedModelLibrary(shared_root=args.shared_root, lock_root=args.lock_root)
            promoted = library.promote(ticket=model, staging=args.staging)
            destination = library.link_workspace(staging=args.staging, model=promoted)
            print(json.dumps({"state": "linked", "model": promoted.key,
                              "bundle_sha256": promoted.manifest_sha256,
                              "guest_path": "/var/ai/models/" + promoted.key,
                              "file_count": len(promoted.files)}, sort_keys=True))
            return 0
        if not 0.1 <= args.interval <= 3600:
            raise ValueError("observer interval must be 0.1 to 3600 seconds")
        observer = ModelDownloadWatcher(args.staging)
        last: dict[str, tuple[str, int]] = {}
        attempted: dict[str, tuple[tuple[str, int, int], ...]] = {}
        tickets: Path | None = None
        library: SharedModelLibrary | None = None
        if args.tickets_dir:
            if not args.shared_root or not args.lock_root:
                raise ValueError("automatic promotion needs --shared-root and --lock-root")
            tickets = args.tickets_dir.resolve(strict=True)
            if (not tickets.is_dir() or tickets.is_relative_to(args.staging.resolve())
                    or tickets.is_relative_to(args.shared_root.resolve())
                    or args.tickets_dir.is_symlink()):
                raise ValueError("enrollment records must stay private to the trusted operator")
            library = SharedModelLibrary(shared_root=args.shared_root, lock_root=args.lock_root)
        with ModelFilesystemNotifier(args.staging) as notifier:
            while True:
                observations = observer.poll()
                for item in observations:
                    key = (item.state, item.byte_count)
                    if last.get(item.path) != key:
                        print(json.dumps({"state": item.state, "path": item.path,
                                          "possible_repository": item.possible_repository,
                                          "possible_revision": item.possible_revision,
                                          "format": item.format, "bytes": item.byte_count,
                                          "authorized": False}, sort_keys=True), flush=True)
                        last[item.path] = key
                for name in set(last) - {item.path for item in observations}:
                    last.pop(name, None)
                if tickets is not None and library is not None:
                    stable = {item.path: item for item in observations
                              if item.state == "needs_ticket"}
                    enrolled = list(sorted(tickets.glob("*.json")))
                    if len(enrolled) > 1000:
                        raise ValueError("operator has too many pending model tickets")
                    for source in enrolled:
                        if source.is_symlink():
                            continue
                        try:
                            admission = _ticket(source, args.staging, args.shared_root)
                            weights = [f"downloads/{admission.ticket_id}/{asset.path}"
                                       for asset in admission.assets
                                       if asset.path.endswith((".gguf", ".safetensors", ".onnx"))]
                            if not weights or not all(path in stable for path in weights):
                                continue
                            state = tuple((name, stable[name].byte_count, stable[name].mtime_ns)
                                          for name in weights)
                            if attempted.get(admission.ticket_id) == state:
                                continue
                            attempted[admission.ticket_id] = state
                            published = library.promote(ticket=admission, staging=args.staging)
                            library.link_workspace(staging=args.staging, model=published)
                            print(json.dumps({"state": "linked", "model": published.key,
                                              "bundle_sha256": published.manifest_sha256,
                                              "guest_path": "/var/ai/models/" + published.key,
                                              "authorized": True}, sort_keys=True), flush=True)
                        except Exception as exc:
                            # A failed digest or uncertain read is never a valid
                            # install; a new file state is required to retry.
                            print(json.dumps({"state": "requires_review", "ticket": source.stem,
                                              "error_kind": type(exc).__name__},
                                             sort_keys=True), flush=True)
                if args.action == "observe":
                    return 0
                notifier.wait(args.interval)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print("model operator failed: " + type(exc).__name__ + ": " + str(exc),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
