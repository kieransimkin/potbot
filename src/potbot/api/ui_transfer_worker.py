"""Dedicated, opt-in artifact transfer worker with honest byte progress.

Never follows arbitrary URLs, runs downloaded code, or changes model routing.
A worker crash marks its lease indeterminate for operator reconciliation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urljoin
from uuid import UUID, uuid4

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, OperationCancelledError, StateMismatchError
from potbot.tools.temporary import allocate_file, create_directory
from potbot.persistence.database import Database


def _safe_hf_url(url: str) -> bool:
    """Reject SSRF, downgrade, unusual ports and arbitrary file hosts."""
    try:
        u = urlsplit(url)
        host = (u.hostname or "").lower()
        return (
            u.scheme == "https" and not u.username and not u.password
            and u.port in {None, 443} and not u.fragment
            and (host == "huggingface.co" or host == "cdn-lfs.huggingface.co"
                 or host.endswith(".xethub.hf.co") or host == "cdn-lfs.hf.co")
        )
    except ValueError:
        return False


def hf_chunks(model_repo: str, revision: str, filename: str) -> Iterable[bytes]:
    """Stream an approved Hugging Face source, permitting only known CDN redirects."""
    import httpx

    uri = f"https://huggingface.co/{model_repo}/resolve/{revision}/{quote(filename)}"
    if not _safe_hf_url(uri):
        raise InvalidInputError("Unapproved Hugging Face artifact location")
    with httpx.Client(follow_redirects=False, trust_env=False,
                      timeout=httpx.Timeout(45, connect=6)) as client:
        for _ in range(6):
            with client.stream("GET", uri, headers={"Accept": "application/octet-stream"}) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise InvalidInputError("Missing model CDN redirect")
                    uri = urljoin(uri, location)
                    if not _safe_hf_url(uri):
                        raise InvalidInputError("Model redirect target is not approved")
                    continue
                if response.status_code != 200:
                    raise InvalidInputError("Model source unavailable or requires separate authorization")
                for chunk in response.iter_bytes(chunk_size=256 * 1024):
                    if chunk:
                        yield chunk
                return
        raise InvalidInputError("Excessive model source redirects")


def _owned_directory(root: Path, components: tuple[str, ...]) -> Path:
    root = root.resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise InvalidInputError("Model storage root must be an ordinary directory")
    directory = root
    for component in components:
        if component in {"", ".", ".."} or "/" in component or "\\" in component:
            raise InvalidInputError("Invalid model storage component")
        directory = directory / component
        if directory.is_symlink():
            raise InvalidInputError("Model storage path cannot be a symlink")
        create_directory(directory)
    return directory


class TransferWorker:
    def __init__(self, database: Database, context: WorkspaceContext, model_root: Path,
                 *, downloader: Callable[[str, str, str], Iterable[bytes]] = hf_chunks) -> None:
        self.db, self.context, self.root, self.downloader = database, context, model_root, downloader
        if model_root.is_symlink() or not model_root.is_dir():
            raise InvalidInputError("Operator must provision an explicit model root")

    def claim(self) -> dict[str, Any] | None:
        with self.db.transaction(self.context) as conn:
            row = conn.execute(
                "SELECT * FROM ui_artifact_transfers WHERE workspace_id=%s AND state='queued' "
                "ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1",
                (self.context.workspace_id,),
            ).fetchone()
            if not row:
                return None
            token = uuid4()
            conn.execute(
                "UPDATE ui_artifact_transfers SET state='downloading',lease_token=%s, "
                "generation=generation+1,updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND id=%s",
                (token, self.context.workspace_id, row["id"]),
            )
            return {**row, "lease_token": token}

    def _update(self, job: dict[str, Any], *, state: str,
                downloaded: int, verified: int = 0, error: str | None = None,
                terminal: bool = False) -> None:
        with self.db.transaction(self.context) as conn:
            row = conn.execute(
                "UPDATE ui_artifact_transfers SET state=%s,downloaded_bytes=%s, "
                "verified_bytes=%s,error_code=%s, "
                "lease_token=CASE WHEN %s THEN NULL ELSE lease_token END, "
                "updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s AND lease_token=%s "
                "RETURNING cancel_requested",
                (state, downloaded, verified, error, terminal, self.context.workspace_id,
                 job["id"], job["lease_token"]),
            ).fetchone()
            if row is None:
                raise StateMismatchError("Transfer lease lost; do not commit an installation")
            if row["cancel_requested"] and not terminal:
                raise OperationCancelledError("Transfer cancellation requested")

    def run_claim(self, job: dict[str, Any]) -> dict[str, Any]:
        installed = _owned_directory(self.root, (
            str(job["model_id"]).replace("/", "--"), str(job["revision"]),
            str(job["quantization"]),
        )) / job["filename"]
        expected = int(job["expected_bytes"])
        downloaded = 0
        temporary: Path | None = None
        fd: int | None = None
        try:
            if installed.is_symlink():
                raise InvalidInputError("An installed model path is a symlink")
            if installed.exists():
                if installed.stat().st_size != expected:
                    raise InvalidInputError("Existing model artifact conflicts with requested size")
                digest = hashlib.sha256()
                with installed.open("rb") as inp:
                    for chunk in iter(lambda: inp.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != job["sha256"]:
                    raise InvalidInputError("Existing artifact conflicts with requested checksum")
                self._update(job, state="installed", downloaded=expected,
                             verified=expected, terminal=True)
                return {"status": "installed", "bytes": expected}
            fd, temporary = allocate_file(installed.parent, prefix=".potbot-transfer-", suffix=".part")
            digest = hashlib.sha256()
            last_report = time.monotonic()
            with os.fdopen(fd, "wb") as target:
                fd = None
                for chunk in self.downloader(job["artifact_repository"], job["revision"], job["filename"]):
                    if not isinstance(chunk, bytes) or not chunk:
                        raise InvalidInputError("Downloader returned invalid bytes")
                    downloaded += len(chunk)
                    if downloaded > expected:
                        raise InvalidInputError("Downloaded bytes exceed approved artifact size")
                    digest.update(chunk)
                    target.write(chunk)
                    if downloaded % (1024 * 1024) < len(chunk) or time.monotonic() - last_report >= 0.5:
                        target.flush()
                        self._update(job, state="downloading", downloaded=downloaded)
                        last_report = time.monotonic()
                target.flush()
                os.fsync(target.fileno())
            self._update(job, state="verifying", downloaded=downloaded)
            if downloaded != expected or digest.hexdigest() != job["sha256"]:
                raise InvalidInputError("Artifact length or checksum mismatch")
            if installed.is_symlink() or installed.exists():
                raise InvalidInputError("Installation destination changed during transfer")
            # Hard-link with O_EXCL semantics: another process cannot make us
            # overwrite an existing model during final destination publication.
            os.link(temporary, installed, follow_symlinks=False)
            temporary.unlink()
            temporary = None
            self._update(job, state="installed", downloaded=downloaded,
                         verified=downloaded, terminal=True)
            return {"status": "installed", "bytes": downloaded}
        except OperationCancelledError:
            self._update(job, state="cancelled", downloaded=downloaded, terminal=True)
            return {"status": "cancelled"}
        except Exception:
            # Never claim a partial/uncertain download was installed; don't
            # expose raw network/exception details or local filesystem paths.
            self._update(job, state="failed", downloaded=downloaded,
                         error="verification_or_transport_failed", terminal=True)
            return {"status": "failed"}
        finally:
            if fd is not None:
                os.close(fd)
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def once(self) -> dict[str, Any]:
        job = self.claim()
        if job is None:
            return {"status": "idle"}
        return self.run_claim(job)

    def reconcile_stale(self, *, older_than_seconds: int = 900) -> int:
        if not 60 <= older_than_seconds <= 86400:
            raise InvalidInputError("Invalid reconciliation window")
        with self.db.transaction(self.context) as conn:
            rows = conn.execute(
                "UPDATE ui_artifact_transfers SET state='indeterminate',lease_token=NULL,"
                "error_code='worker_disconnected',updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND state IN ('downloading','verifying') "
                "AND updated_at < clock_timestamp()-(%s::int * interval '1 second') RETURNING id",
                (self.context.workspace_id, older_than_seconds),
            ).fetchall()
        return len(rows)


def main() -> int:
    from potbot.api.jobs_cli import add_scope_arguments, database_context
    parser = argparse.ArgumentParser(description=__doc__)
    add_scope_arguments(parser)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2)
    args = parser.parse_args()
    db, context = database_context(args)
    worker = TransferWorker(db, context, args.model_root)
    worker.reconcile_stale()
    if args.once:
        print(json.dumps(worker.once()))
        return 0
    while True:
        worker.once()
        time.sleep(max(1, min(60, args.poll_seconds)))


if __name__ == "__main__":
    raise SystemExit(main())
