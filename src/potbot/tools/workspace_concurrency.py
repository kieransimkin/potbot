"""Controller-private, cross-process workspace leases for declared tool effects.

This is an admission primitive, not an OS sandbox: an unbrokered shell can still
write files. Until every mutating tool is routed through a fenced, supervised
broker, this subsystem must not be advertised as complete isolation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError

Effect = Literal["read", "write", "unknown"]
Mode = Literal["read", "write"]
State = Literal["active", "completed", "indeterminate", "reconciled"]
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GITHUB_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_REF = re.compile(r"[A-Za-z0-9_./-]{1,200}\Z")


def _home_path(value: str) -> str:
    """Canonical lexical paths only; OS symlink admission is a *separate* check."""
    if not isinstance(value, str) or "\x00" in value or "\\" in value:
        raise InvalidInputError("workspace access paths must be Linux paths")
    if not value.startswith("/home") or (len(value) > 5 and value[5] != "/"):
        raise ScopeDeniedError("only /home may be used for writable workspace paths")
    if value != "/home":
        chunks = value.split("/")[1:]
        if any(not part or part in {".", ".."} for part in chunks):
            raise InvalidInputError("workspace access paths must be canonical")
    if len(value.encode("utf-8")) > 1024:
        raise InvalidInputError("workspace access paths exceed their bound")
    return value


def _github_ref(value: str) -> str:
    """Resource format owner/repo#refs/heads/name or owner/repo#*."""
    if not isinstance(value, str) or "#" not in value:
        raise InvalidInputError("GitHub access needs an exact repository and ref")
    repo, ref = value.split("#", 1)
    identity = repo.split("/")
    if len(identity) != 2 or not all(_GITHUB_NAME.fullmatch(v) for v in identity):
        raise InvalidInputError("invalid GitHub repository identity")
    if ref != "*" and (
        not ref.startswith("refs/heads/")
        or not _REF.fullmatch(ref)
        or ".." in ref
        or "//" in ref
        or ref.endswith("/")
        or ref.endswith(".lock")
    ):
        raise InvalidInputError("GitHub mutation requires an exact branch")
    return f"{repo.lower()}#{ref}"


@dataclass(frozen=True, slots=True)
class AccessResource:
    kind: Literal["home", "github", "github_file", "workspace", "external", "shared_tool"]
    name: str
    mode: Mode

    def __post_init__(self) -> None:
        if self.mode not in ("read", "write"):
            raise InvalidInputError("access must be read or write")
        if self.kind == "home":
            object.__setattr__(self, "name", _home_path(self.name))
        elif self.kind == "github":
            object.__setattr__(self, "name", _github_ref(self.name))
        elif self.kind == "github_file":
            if not isinstance(self.name, str) or ":" not in self.name:
                raise InvalidInputError("GitHub file requires repository ref and path")
            identity, path = self.name.split(":", 1)
            identity = _github_ref(identity)
            if (not path or path.startswith("/") or len(path.encode()) > 1024
                    or "\\" in path or any(part in {"", ".", ".."}
                                       for part in path.split("/"))):
                raise InvalidInputError("GitHub file must be a canonical repository path")
            object.__setattr__(self, "name", identity + ":" + path)
        elif self.kind == "shared_tool":
            # All processes using the same installed tool share a named lease;
            # disjoint tool installations may be used concurrently.
            if not isinstance(self.name, str) or not re.fullmatch(
                r"[a-z][a-z0-9-]{1,62}", self.name
            ):
                raise InvalidInputError("shared tool name must be a canonical slug")
        elif self.kind == "workspace":
            if self.name != "*":
                raise InvalidInputError("workspace-wide access must use *")
        elif self.kind == "external":
            if not isinstance(self.name, str) or not re.fullmatch(
                r"[a-z0-9][a-z0-9._:/-]{0,199}", self.name
            ):
                raise InvalidInputError("external effect requires a bounded broker resource")
        else:
            raise InvalidInputError("unknown resource class")

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "name": self.name, "mode": self.mode}


def conflict(a: AccessResource, b: AccessResource, *, same_workspace: bool = True) -> bool:
    """Parallel reads are safe. GitHub and external resources are global across workspaces."""
    if a.mode == b.mode == "read":
        return False
    if not same_workspace:
        if a.kind != b.kind and {a.kind, b.kind} != {"github", "github_file"}:
            return False
        if a.kind not in {"github", "github_file", "external"}:
            return False
    if "workspace" in {a.kind, b.kind}:
        return True
    if {a.kind, b.kind} <= {"github", "github_file"}:
        # A branch ref update is serialized against all staged file operations
        # on that branch. Disjoint *file staging* can proceed concurrently.
        key_a = a.name.split(":", 1)[0] if a.kind == "github_file" else a.name
        key_b = b.name.split(":", 1)[0] if b.kind == "github_file" else b.name
        repo_a, ref_a = key_a.split("#", 1)
        repo_b, ref_b = key_b.split("#", 1)
        if repo_a != repo_b or not (ref_a == ref_b or ref_a == "*" or ref_b == "*"):
            return False
        if a.kind == "github" or b.kind == "github":
            return True
        path_a, path_b = a.name.split(":", 1)[1], b.name.split(":", 1)[1]
        return path_a == path_b or path_a.startswith(path_b + "/") or path_b.startswith(path_a + "/")
    if a.kind != b.kind:
        return False
    if a.kind == "shared_tool":
        return a.name == b.name
    if a.kind == "home":
        return (
            a.name == b.name
            or a.name.startswith(b.name + "/")
            or b.name.startswith(a.name + "/")
        )
    if a.kind == "github":
        a_repo, a_ref = a.name.split("#", 1)
        b_repo, b_ref = b.name.split("#", 1)
        return a_repo == b_repo and (a_ref == "*" or b_ref == "*" or a_ref == b_ref)
    return a.name == b.name


@dataclass(frozen=True, slots=True)
class AccessRequest:
    workspace_id: UUID
    actor_id: UUID
    operation: str
    idempotency_key: str
    effect: Effect
    resources: tuple[AccessResource, ...]
    image_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, UUID) or not isinstance(self.actor_id, UUID):
            raise InvalidInputError("access requires exact workspace and agent identities")
        if not isinstance(self.operation, str) or not re.fullmatch(
            r"[a-z][a-z0-9_.-]{1,99}", self.operation
        ):
            raise InvalidInputError("access operation must be named and bounded")
        if not isinstance(self.idempotency_key, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,160}", self.idempotency_key
        ):
            raise InvalidInputError("access idempotency key is invalid")
        if self.effect not in {"read", "write", "unknown"}:
            raise InvalidInputError("operation effect is unsupported")
        if not isinstance(self.resources, tuple) or not 1 <= len(self.resources) <= 32:
            raise InvalidInputError("an access must declare 1-32 resources")
        if any(not isinstance(r, AccessResource) for r in self.resources):
            raise InvalidInputError("resources must be structured")
        if len({(r.kind, r.name) for r in self.resources}) != len(self.resources):
            raise InvalidInputError("duplicate resources are not permitted")
        if self.effect == "read" and any(r.mode != "read" for r in self.resources):
            raise InvalidInputError("read-only tools cannot request writes")
        if self.effect == "write" and not any(r.mode == "write" for r in self.resources):
            raise InvalidInputError("mutations require explicit write resources")
        if self.effect == "unknown" and AccessResource("workspace", "*", "write") not in self.resources:
            raise InvalidInputError("unknown tool effects require an exclusive workspace claim")
        if not isinstance(self.image_digest, str) or not _SHA256.fullmatch(self.image_digest):
            raise InvalidInputError("a pinned system image SHA-256 is required")

    @property
    def fingerprint(self) -> str:
        payload = {
            "workspace_id": str(self.workspace_id),
            "actor_id": str(self.actor_id),
            "operation": self.operation,
            "effect": self.effect,
            "resources": [r.to_dict() for r in self.resources],
            "image_digest": self.image_digest,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class AccessLease:
    lease_id: str
    workspace_id: UUID
    generation: int
    expires_ns: int
    state: State
    token: str = field(repr=False)  # Host-only capability; never expose to sandbox.


class WorkspaceLeaseLedger:
    """SQLite transactions serialize leases across processes on ONE trusted host.

    Controller database must be outside the sandbox's writable /home. Use
    PostgreSQL once the controller supports distributed multi-host scheduling.
    """

    def __init__(self, db_path: Path, *, workspace_home: Path) -> None:
        self.path = Path(db_path).resolve()
        home = Path(workspace_home).resolve()
        if not home.is_dir():
            raise InvalidInputError("controller must use a registered workspace home")
        self.home = home
        if self.path.is_relative_to(home) or self.path.parent == Path("/"):
            raise ScopeDeniedError("lease ledger must be controller-private, outside /home")
        if not self.path.parent.is_dir():
            raise InvalidInputError("controller ledger parent must already exist")
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """CREATE TABLE IF NOT EXISTS leases(
                    generation INTEGER PRIMARY KEY AUTOINCREMENT,
                    lease_id TEXT NOT NULL UNIQUE,
                    workspace_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    image_digest TEXT NOT NULL,
                    resources TEXT NOT NULL,
                    token TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                        ('active','completed','indeterminate','reconciled')),
                    expires_ns INTEGER NOT NULL,
                    result_sha256 TEXT,
                    UNIQUE(workspace_id,idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS lease_scopes
                ON leases(workspace_id,state);
                CREATE TABLE IF NOT EXISTS workspace_image_pins(
                    workspace_id TEXT PRIMARY KEY,
                    digest TEXT NOT NULL,
                    image_ref TEXT NOT NULL,
                    build_commit TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    rootfs_path TEXT NOT NULL,
                    qualification_sha256 TEXT NOT NULL,
                    revision INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspace_image_history(
                    workspace_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    image_ref TEXT NOT NULL,
                    qualification_sha256 TEXT NOT NULL,
                    PRIMARY KEY(workspace_id,revision)
                );"""
            )
        if os.name == "posix":
            os.chmod(self.path, 0o600)

    @staticmethod
    def _lease(row: sqlite3.Row) -> AccessLease:
        return AccessLease(
            row["lease_id"], UUID(row["workspace_id"]), row["generation"],
            row["expires_ns"], row["state"], row["token"]
        )

    @staticmethod
    def _expire(db: sqlite3.Connection, now: int) -> None:
        # Uncertain effects stay blocking until explicit verified reconciliation.
        db.execute(
            "UPDATE leases SET state='indeterminate' WHERE state='active' AND expires_ns<=?",
            (now,),
        )

    def _validate_home_aliases(self, request: AccessRequest) -> None:
        """Reject symlinks in existing /home path components before leasing.

        The execution layer still must use descriptor-relative, race-resistant
        opens; this lexical gate is not an openat2 replacement.
        """
        for resource in request.resources:
            if resource.kind != "home":
                continue
            relative = resource.name.removeprefix("/home").lstrip("/")
            current = self.home
            for component in Path(relative).parts:
                current = current / component
                if current.is_symlink():
                    raise ScopeDeniedError("workspace resource path contains a symlink")
                if not current.exists():
                    break

    def claim(
        self, request: AccessRequest, *, ttl_seconds: int = 60,
        replay_existing: bool = True,
    ) -> AccessLease:
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
            raise InvalidInputError("lease TTL must be between 1 and 300 seconds")
        self._validate_home_aliases(request)
        now = time.time_ns()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db, now)
            prior = db.execute(
                "SELECT * FROM leases WHERE workspace_id=? AND idempotency_key=?",
                (str(request.workspace_id), request.idempotency_key),
            ).fetchone()
            if prior:
                if prior["request_hash"] != request.fingerprint:
                    raise StateMismatchError("access idempotency conflict")
                if not replay_existing:
                    # A second worker must not obtain the active fencing token
                    # and finish somebody else's concurrent execution. An
                    # execution attempt is never silently redispatched.
                    raise StateMismatchError("workspace invocation already recorded")
                return self._lease(prior)
            pinned = db.execute(
                "SELECT digest FROM workspace_image_pins WHERE workspace_id=?",
                (str(request.workspace_id),),
            ).fetchone()
            if pinned is None or pinned["digest"] != request.image_digest:
                raise StateMismatchError("workspace system image is not pinned or has changed")
            for row in db.execute(
                "SELECT workspace_id,resources FROM leases "
                "WHERE state IN ('active','indeterminate')"
            ):
                same_workspace = row["workspace_id"] == str(request.workspace_id)
                existing = tuple(AccessResource(**item) for item in json.loads(row["resources"]))
                if any(
                    conflict(a, b, same_workspace=same_workspace)
                    for a in request.resources for b in existing
                ):
                    raise StateMismatchError("conflicting or indeterminate workspace access")
            identifier = uuid4().hex
            token = uuid4().hex + uuid4().hex
            db.execute(
                "INSERT INTO leases(lease_id,workspace_id,actor_id,operation,"
                "idempotency_key,request_hash,image_digest,resources,token,state,expires_ns) "
                "VALUES(?,?,?,?,?,?,?,?,?,'active',?)",
                (
                    identifier, str(request.workspace_id), str(request.actor_id),
                    request.operation, request.idempotency_key, request.fingerprint,
                    request.image_digest,
                    json.dumps([r.to_dict() for r in request.resources], sort_keys=True),
                    token, now + ttl_seconds * 1_000_000_000,
                ),
            )
            row = db.execute("SELECT * FROM leases WHERE lease_id=?", (identifier,)).fetchone()
            assert row is not None
            return self._lease(row)

    def renew(self, lease: AccessLease, *, ttl_seconds: int = 60) -> AccessLease:
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
            raise InvalidInputError("invalid lease TTL")
        now = time.time_ns()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db, now)
            updated = db.execute(
                "UPDATE leases SET expires_ns=? WHERE lease_id=? AND workspace_id=? "
                "AND generation=? AND token=? AND state='active'",
                (now + ttl_seconds * 1_000_000_000, lease.lease_id,
                 str(lease.workspace_id), lease.generation, lease.token),
            )
            if updated.rowcount != 1:
                raise StateMismatchError("workspace tool lease is expired or fenced")
            row = db.execute("SELECT * FROM leases WHERE lease_id=?", (lease.lease_id,)).fetchone()
            assert row is not None
            return self._lease(row)

    def finish(self, lease: AccessLease, *, result_sha256: str, uncertain: bool = False) -> None:
        if not isinstance(result_sha256, str) or not _SHA256.fullmatch(result_sha256):
            raise InvalidInputError("completion requires retained SHA-256 evidence")
        now = time.time_ns()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db, now)
            state = "indeterminate" if uncertain else "completed"
            result = db.execute(
                "UPDATE leases SET state=?,result_sha256=? WHERE lease_id=? "
                "AND workspace_id=? AND generation=? AND token=? AND state='active'",
                (state, result_sha256, lease.lease_id, str(lease.workspace_id),
                 lease.generation, lease.token),
            )
            if result.rowcount != 1:
                raise StateMismatchError("workspace tool completion is stale or duplicated")

    def reconcile(self, lease: AccessLease, *, evidence_sha256: str) -> None:
        """Host-admin reconciliation only; never callable by an untrusted model."""
        if not isinstance(evidence_sha256, str) or not _SHA256.fullmatch(evidence_sha256):
            raise InvalidInputError("reconciliation needs independent evidence")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                "UPDATE leases SET state='reconciled',result_sha256=? WHERE lease_id=? "
                "AND workspace_id=? AND generation=? AND token=? AND state='indeterminate'",
                (evidence_sha256, lease.lease_id, str(lease.workspace_id),
                 lease.generation, lease.token),
            )
            if result.rowcount != 1:
                raise StateMismatchError("only indeterminate tool calls can be reconciled")

    def active(self, workspace_id: UUID) -> list[dict[str, object]]:
        """Controller-side metadata, never returns bearer tokens or raw results."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db, time.time_ns())
            rows = db.execute(
                "SELECT generation,operation,state,image_digest,expires_ns "
                "FROM leases WHERE workspace_id=? AND state IN ('active','indeterminate') "
                "ORDER BY generation", (str(workspace_id),)
            ).fetchall()
            return [dict(row) for row in rows]

    def pin_image(
        self, workspace_id: UUID, *, digest: str, image_ref: str,
        build_commit: str, platform: str, rootfs_path: Path,
        qualification_sha256: str, expected_revision: int = 0,
    ) -> int:
        """Host-only image upgrade with CAS; no live/uncertain tool calls allowed.

        Callers must validate image manifest signature, immutable rootfs extraction,
        guest architecture, OS isolation and the external qualification receipt.
        An untrusted model or sandbox shell must not call this host service.
        """
        if not isinstance(workspace_id, UUID) or not all(
            isinstance(v, str) and _SHA256.fullmatch(v)
            for v in (digest, qualification_sha256)
        ):
            raise InvalidInputError("image activation requires digests")
        if not isinstance(build_commit, str) or not re.fullmatch(
            r"[0-9a-f]{40}", build_commit
        ):
            raise InvalidInputError("image build must bind a commit")
        if platform not in {"linux/amd64", "linux/arm64"}:
            raise InvalidInputError("image platform must be Linux and supported")
        if image_ref.count("@sha256:") != 1 or not image_ref.endswith("@sha256:" + digest):
            raise InvalidInputError("image reference must be digest-pinned")
        root = Path(rootfs_path).resolve(strict=True)
        if not root.is_dir() or root.is_relative_to(self.path.parent):
            raise ScopeDeniedError("system rootfs must be separately provisioned")
        if type(expected_revision) is not int or expected_revision < 0:
            raise InvalidInputError("invalid image revision precondition")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db, time.time_ns())
            active = db.execute(
                "SELECT 1 FROM leases WHERE workspace_id=? "
                "AND state IN ('active','indeterminate') LIMIT 1",
                (str(workspace_id),),
            ).fetchone()
            if active:
                raise StateMismatchError("reconcile/finish workspace operations before image change")
            existing = db.execute(
                "SELECT revision FROM workspace_image_pins WHERE workspace_id=?",
                (str(workspace_id),),
            ).fetchone()
            old = existing["revision"] if existing else 0
            if old != expected_revision:
                raise StateMismatchError("system image revision was changed concurrently")
            revision = old + 1
            db.execute(
                "INSERT INTO workspace_image_pins VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(workspace_id) DO UPDATE SET digest=excluded.digest, "
                "image_ref=excluded.image_ref,build_commit=excluded.build_commit, "
                "platform=excluded.platform,rootfs_path=excluded.rootfs_path, "
                "qualification_sha256=excluded.qualification_sha256,revision=excluded.revision",
                (str(workspace_id), digest, image_ref, build_commit, platform,
                 str(root), qualification_sha256, revision),
            )
            db.execute(
                "INSERT INTO workspace_image_history VALUES(?,?,?,?,?)",
                (str(workspace_id), revision, digest, image_ref, qualification_sha256),
            )
            return revision

    def image_status(self, workspace_id: UUID) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT workspace_id,digest,image_ref,build_commit,platform,revision, "
                "qualification_sha256,rootfs_path FROM workspace_image_pins WHERE workspace_id=?",
                (str(workspace_id),),
            ).fetchone()
            return dict(row) if row is not None else None
