"""S11: value-blind, evidence-bound approvals for S9-tested shared tools.

HTTP handlers only read independently published S9 evidence or queue approval.
They have no access to the shared-tool filesystem and cannot invoke activation.
A separately authorized operator claims, verifies and executes an approval.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypedDict

from fastapi import Request
from uuid import UUID, uuid4

from potbot.domain.errors import (
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)

if TYPE_CHECKING:
    from fastapi import FastAPI
    from potbot.domain.auth import WorkspaceContext
    from potbot.persistence.database import Database

class ToolApprovalClaim(TypedDict):
    id: UUID
    agent_id: UUID
    slug: str
    old_commit: str
    new_commit: str
    evidence_sha256: str
    suite_sha256: str
    passed_tests: int
    tests: list[dict[str, object]] | str
    worker_token: UUID


_SLUG = re.compile(r"[a-z][a-z0-9-]{1,62}\Z")
_COMMIT = re.compile(r"[a-f0-9]{40}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


def _approval_mac(token: str, record: dict[str, Any], approver: UUID, revision: int) -> str:
    """Versioned decision MAC, independently checked by the host worker.

    A general workspace DB login can update RLS-scoped data, but cannot mint
    an executable approval without the separately stored native-vault token.
    """
    if (not isinstance(token, str) or not 32 <= len(token) <= 4096
            or not all(33 <= ord(char) <= 126 for char in token)
            or not isinstance(approver, UUID) or type(revision) is not int):
        raise ScopeDeniedError("Operator approval requires an independent setup capability")
    names = ("workspace_id", "id", "agent_id", "slug", "old_commit", "new_commit",
             "suite_sha256", "evidence_sha256")
    message = {name: str(record[name]) for name in names}
    tests = record["tests"]
    if isinstance(tests, str):
        tests = json.loads(tests)
    if not isinstance(tests, list):
        raise StateMismatchError("Approval test summary is invalid")
    message.update({"schema": "potbot-s11-approved/v1", "approver": str(approver),
                    "revision": revision, "decision": "approved",
                    "tests": tests, "passed_tests": record["passed_tests"]})
    encoded = json.dumps(message, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(token.encode("ascii"), encoded, hashlib.sha256).hexdigest()


def _uuid(value: object) -> UUID:
    try:
        if not isinstance(value, str):
            raise ValueError
        result = UUID(value)
        if str(result) != value:
            raise ValueError
        return result
    except (TypeError, ValueError, AttributeError):
        raise InvalidInputError("Expected a canonical tool operation UUID") from None


def _digest(value: object) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise InvalidInputError("Expected SHA-256 evidence")
    return value


def _public(row: Any) -> dict[str, object]:
    tests = row["tests"]
    if isinstance(tests, str):
        tests = json.loads(tests)
    if not isinstance(tests, list):
        raise StateMismatchError("Stored S9 test results are not a list")
    return {
        "id": str(row["id"]), "workspaceId": str(row["workspace_id"]),
        "slug": row["slug"], "oldCommit": row["old_commit"],
        "newCommit": row["new_commit"], "suiteSha256": row["suite_sha256"],
        "evidenceSha256": row["evidence_sha256"],
        "passedTests": row["passed_tests"], "tests": tests, "revision": row["revision"],
        "state": row["state"], "updatedAt": row["updated_at"].isoformat(),
    }


class UiToolUpgradeStore:
    """PostgreSQL/RLS decisions, isolated from host-execution credentials.

    ``publish_test`` must be invoked only with evidence freshly read from the
    S9 operator manager, NEVER from an HTTP body. The publish call validates
    the S9 evidence hash against the corresponding manager record at its caller.
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _audit(conn: Any, context: WorkspaceContext, ident: UUID, action: str,
               previous: str, next_state: str, revision: int, evidence: str) -> None:
        conn.execute(
            "INSERT INTO ui_tool_upgrade_audit "
            "(workspace_id,id,upgrade_id,principal_id,action,prior_state,next_state,revision,evidence_sha256) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (context.workspace_id, uuid4(), ident, context.principal_id,
             action, previous, next_state, revision, evidence),
        )

    def publish_test(self, context: WorkspaceContext, *, agent_id: UUID,
                     tested: dict[str, object]) -> dict[str, object]:
        """Host-only: caller has already verified S9 record and S7 monitor."""
        if not isinstance(agent_id, UUID) or not isinstance(tested, dict):
            raise InvalidInputError("Only verified S9 test evidence may be published")
        operation = _uuid(tested.get("id"))
        slug, old, new = tested.get("slug"), tested.get("old_commit"), tested.get("new_commit")
        suite, evidence = tested.get("suite_sha256"), tested.get("test_evidence_sha256")
        tests = tested.get("test_results")
        if (not isinstance(slug, str) or not _SLUG.fullmatch(slug)
                or not isinstance(old, str) or not _COMMIT.fullmatch(old)
                or not isinstance(new, str) or not _COMMIT.fullmatch(new) or old == new
                or not isinstance(tests, list) or not 2 <= len(tests) <= 16
                or any(not isinstance(t, dict) or t.get("passed") is not True for t in tests)):
            raise InvalidInputError("Published S9 evidence is incomplete")
        _digest(suite)
        _digest(evidence)
        safe_tests: list[dict[str, object]] = []
        for case in tests:
            if (not isinstance(case, dict) or not isinstance(case.get("name"), str)
                    or not re.fullmatch(r"[a-z][a-z0-9_.-]{1,63}", case["name"])
                    or case.get("kind") not in {"smoke", "unit", "integration", "regression"}
                    or type(case.get("elapsed_ms")) is not int
                    or not 0 <= case["elapsed_ms"] <= 180_000):
                raise InvalidInputError("S9 test case is missing safe measured evidence")
            safe_tests.append({"name": case["name"], "kind": case["kind"],
                               "elapsedMs": case["elapsed_ms"], "passed": True})
        with self.database.transaction(context) as conn:
            conn.execute("SELECT pg_catalog.pg_advisory_xact_lock(hashtext(%s))",
                         (f"potbot:tools:publish:{context.workspace_id}:{slug}",))
            existing = conn.execute(
                "SELECT * FROM ui_tool_upgrade_approvals WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, operation),
            ).fetchone()
            if existing is not None:
                if (existing["agent_id"] != agent_id or existing["slug"] != slug
                        or existing["old_commit"] != old or existing["new_commit"] != new
                        or existing["suite_sha256"] != suite
                        or existing["evidence_sha256"] != evidence
                        or (existing["tests"] if isinstance(existing["tests"], list)
                            else json.loads(existing["tests"])) != safe_tests):
                    raise RevisionConflictError("Published test identity has changed")
                return _public(existing)
            row = conn.execute(
                "INSERT INTO ui_tool_upgrade_approvals "
                "(workspace_id,id,principal_id,agent_id,slug,old_commit,new_commit,"
                "suite_sha256,evidence_sha256,passed_tests,tests) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING *",
                (context.workspace_id, operation, context.principal_id, agent_id,
                 slug, old, new, suite, evidence, len(tests), json.dumps(safe_tests)),
            ).fetchone()
            self._audit(conn, context, operation, "published", "none", "awaiting_approval", 0, evidence)
            return _public(row)

    def list(self, context: WorkspaceContext, *, limit: int = 50) -> dict[str, object]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidInputError("Upgrade listing limit is invalid")
        with self.database.transaction(context) as conn:
            rows = conn.execute(
                "SELECT * FROM ui_tool_upgrade_approvals WHERE workspace_id=%s "
                "ORDER BY created_at DESC,id LIMIT %s", (context.workspace_id, limit),
            ).fetchall()
        return {"schema": "potbot-tool-upgrades-s11/v1", "items": [_public(row) for row in rows]}

    def inspect(self, context: WorkspaceContext, operation: UUID) -> dict[str, object]:
        with self.database.transaction(context) as conn:
            row = conn.execute(
                "SELECT * FROM ui_tool_upgrade_approvals WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, operation),
            ).fetchone()
        if row is None:
            raise ScopeDeniedError("Tool upgrade was not published to this workspace")
        return _public(row)

    def decide(self, context: WorkspaceContext, operation: UUID, *,
               expected_revision: int, evidence_sha256: str, approve: bool,
               setup_token: str | None = None) -> dict[str, object]:
        if type(expected_revision) is not int or expected_revision < 0:
            raise InvalidInputError("Upgrade decision needs a bounded revision")
        _digest(evidence_sha256)
        if type(approve) is not bool:
            raise InvalidInputError("Upgrade decision requires an explicit boolean")
        state = "approved" if approve else "rejected"
        with self.database.transaction(context) as conn:
            row = conn.execute(
                "SELECT * FROM ui_tool_upgrade_approvals "
                "WHERE workspace_id=%s AND id=%s FOR UPDATE",
                (context.workspace_id, operation),
            ).fetchone()
            if row is None:
                raise ScopeDeniedError("Tool upgrade approval unavailable")
            if (row["state"] != "awaiting_approval" or row["revision"] != expected_revision
                    or row["evidence_sha256"] != evidence_sha256):
                raise RevisionConflictError("Tool test results or approval revision changed")
            mac = (_approval_mac(setup_token, row, context.principal_id, expected_revision + 1)
                   if approve else None)
            updated = conn.execute(
                "UPDATE ui_tool_upgrade_approvals SET state=%s,revision=revision+1,"
                "approval_principal_id=%s,approval_mac=%s,updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND id=%s AND state='awaiting_approval' AND revision=%s "
                "RETURNING *",
                (state, context.principal_id, mac, context.workspace_id, operation, expected_revision),
            ).fetchone()
            if updated is None:
                raise RevisionConflictError("Concurrent approval changed the record")
            self._audit(conn, context, operation, "approved" if approve else "rejected",
                        "awaiting_approval", state, updated["revision"], evidence_sha256)
            return _public(updated)

    def claim(self, context: WorkspaceContext, *, agent_id: UUID,
              setup_token: str) -> ToolApprovalClaim | None:
        """Host-only worker claim; API never calls this or handles worker token."""
        if not isinstance(agent_id, UUID):
            raise InvalidInputError("Approval worker needs an enrolled agent")
        with self.database.transaction(context) as conn:
            row = conn.execute(
                "SELECT * FROM ui_tool_upgrade_approvals "
                "WHERE workspace_id=%s AND agent_id=%s AND state='approved' ORDER BY created_at,id "
                "FOR UPDATE SKIP LOCKED LIMIT 1", (context.workspace_id, agent_id),
            ).fetchone()
            if row is None:
                return None
            if row["approval_principal_id"] is None or not isinstance(row["approval_mac"], str):
                raise ScopeDeniedError("S11 approval lacks operator signing evidence")
            expected = _approval_mac(setup_token, row, row["approval_principal_id"], row["revision"])
            if not hmac.compare_digest(expected, row["approval_mac"]):
                raise ScopeDeniedError("S11 approval signature differs from operator vault")
            token = uuid4()
            changed = conn.execute(
                "UPDATE ui_tool_upgrade_approvals SET state='executing',worker_token=%s,"
                "revision=revision+1,updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND id=%s AND state='approved' RETURNING *",
                (token, context.workspace_id, row["id"]),
            ).fetchone()
            if changed is None:
                raise StateMismatchError("Tool approval claim was lost")
            self._audit(conn, context, row["id"], "claimed", "approved", "executing",
                        changed["revision"], changed["evidence_sha256"])
            # Only the host worker gets its freshly generated capability; no public projection.
            return {"id": row["id"], "slug": row["slug"],
                    "agent_id": row["agent_id"], "old_commit": row["old_commit"],
                    "new_commit": row["new_commit"], "evidence_sha256": row["evidence_sha256"],
                    "worker_token": token, "suite_sha256": row["suite_sha256"],
                    "passed_tests": row["passed_tests"], "tests": row["tests"]}

    def finish(self, context: WorkspaceContext, *, operation: UUID, worker_token: UUID,
               active: bool) -> dict[str, object]:
        if not isinstance(worker_token, UUID) or type(active) is not bool:
            raise InvalidInputError("Tool execution completion needs a host receipt")
        result = "awaiting_repin" if active else "indeterminate"
        with self.database.transaction(context) as conn:
            changed = conn.execute(
                "UPDATE ui_tool_upgrade_approvals SET state=%s,worker_token=NULL,"
                "revision=revision+1,updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND id=%s AND state='executing' AND worker_token=%s "
                "RETURNING *",
                (result, context.workspace_id, operation, worker_token),
            ).fetchone()
            if changed is None:
                raise StateMismatchError("Approval worker lease changed; preserve indeterminate state")
            self._audit(conn, context, operation,
                        "activated" if active else "indeterminate", "executing", result,
                        changed["revision"], changed["evidence_sha256"])
            return _public(changed)

    def confirm_repin(self, context: WorkspaceContext, *, operation: UUID,
                      tested_sha256: str) -> dict[str, object]:
        """Host-only completion after operator verifies current runtime hashes."""
        _digest(tested_sha256)
        with self.database.transaction(context) as conn:
            record = conn.execute(
                "SELECT * FROM ui_tool_upgrade_approvals WHERE workspace_id=%s AND id=%s FOR UPDATE",
                (context.workspace_id, operation),
            ).fetchone()
            if record is None or record["state"] != "awaiting_repin" or (
                    record["evidence_sha256"] != tested_sha256):
                raise StateMismatchError("Operator runtime repin not ready")
            updated = conn.execute(
                "UPDATE ui_tool_upgrade_approvals SET state='active',"
                "revision=revision+1,updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND id=%s AND state='awaiting_repin' RETURNING *",
                (context.workspace_id, operation),
            ).fetchone()
            if updated is None:
                raise StateMismatchError("Operator repin state changed")
            self._audit(conn, context, operation, "reconciled", "awaiting_repin",
                        "active", updated["revision"], tested_sha256)
            return _public(updated)


def register_tool_upgrade_routes(app: FastAPI, database: Database, authenticate: Any,
                                 allowed_origins: tuple[str, ...],
                                 authorize_setup: Callable[[str | None], None]) -> None:
    """Read-only results plus explicitly confirmed, privileged approval requests."""
    from fastapi.responses import JSONResponse

    store = UiToolUpgradeStore(database)

    def actor(request: Request, *, privileged: bool) -> WorkspaceContext:
        if request.query_params:
            raise InvalidInputError("Upgrade endpoint does not accept query credentials")
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            raise ScopeDeniedError("Untrusted upgrade origin")
        context = authenticate(request.headers.get("authorization"))
        if privileged:
            if origin is None:
                raise ScopeDeniedError("Upgrade decisions require an exact browser origin")
            authorize_setup(request.headers.get("x-potbot-setup-token"))
        return context

    async def body(request: Request) -> dict[str, Any]:
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            raise InvalidInputError("Upgrade decision requires JSON")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 2048:
                raise InvalidInputError("Upgrade decision exceeds size allowance")
        def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
            values: dict[str, Any] = {}
            for name, value in items:
                if name in values:
                    raise ValueError("duplicate key")
                values[name] = value
            return values

        def forbid_constant(_value: str) -> None:
            raise ValueError("nonfinite JSON constant")

        try:
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                              parse_constant=forbid_constant)
        except (UnicodeError, ValueError):
            raise InvalidInputError("Invalid upgrade decision document") from None
        if not isinstance(data, dict) or set(data) != {"revision", "evidenceSha256", "confirm"}:
            raise InvalidInputError("Upgrade decision fields are invalid")
        return data

    def response(record: dict[str, object]) -> JSONResponse:
        return JSONResponse(record, headers={"Cache-Control": "private, no-store"})

    @app.get("/v1/ui/tools/upgrades")
    async def listing(request: Request) -> JSONResponse:
        identity = actor(request, privileged=False)
        return response(store.list(identity))

    @app.get("/v1/ui/tools/upgrades/{operation_id}")
    async def detail(request: Request, operation_id: str) -> JSONResponse:
        return response(store.inspect(actor(request, privileged=False), _uuid(operation_id)))

    @app.post("/v1/ui/tools/upgrades/{operation_id}/approve")
    async def approve(request: Request, operation_id: str) -> JSONResponse:
        identity = actor(request, privileged=True)
        value = await body(request)
        if value["confirm"] is not True:
            raise InvalidInputError("Operator confirmation required")
        return response(store.decide(identity, _uuid(operation_id), expected_revision=value["revision"],
                                     evidence_sha256=value["evidenceSha256"], approve=True,
                                     setup_token=request.headers.get("x-potbot-setup-token")))

    @app.post("/v1/ui/tools/upgrades/{operation_id}/reject")
    async def reject(request: Request, operation_id: str) -> JSONResponse:
        identity = actor(request, privileged=True)
        value = await body(request)
        if value["confirm"] is not True:
            raise InvalidInputError("Operator confirmation required")
        return response(store.decide(identity, _uuid(operation_id), expected_revision=value["revision"],
                                     evidence_sha256=value["evidenceSha256"], approve=False))
