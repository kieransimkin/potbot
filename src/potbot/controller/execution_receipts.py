"""Signed historical trace inspection. Replay never dispatches the original work."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from potbot.controller.execution_events import ExecutionEventService
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.security.public import VerificationPolicy, verify_signature_bundle_digest
from potbot.tools.execution_contracts import (
    MAX_EVENTS,
    MAX_RUNS,
    MAX_TRACE_BYTES,
    RECEIPT,
    TERMINAL,
    ExecutionPolicy,
    digest,
    fields,
    identifier,
)

AUDIENCE = "potbot.execution-receipt"


class NativeReceiptSigner:
    """Existing native broker owns key custody and interactive approval."""

    def __init__(
        self, broker: Any, vault: Path, signer_hash: str, known_types: frozenset[str]
    ) -> None:
        self.broker, self.vault, self.signer_hash, self.known_types = (
            broker,
            vault,
            digest(signer_hash),
            known_types,
        )

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = self.broker.sign(
            self.vault,
            request,
            expected_signer_hash=self.signer_hash,
            known_type_refs=self.known_types,
        )
        return result


class ExecutionReceiptService:
    def __init__(
        self,
        database: Any,
        artifacts: Any,
        actor: Any,
        *,
        policy: ExecutionPolicy,
        signer_hash: str,
        known_types: frozenset[str],
        sign: Callable[[dict[str, Any]], dict[str, Any]],
        cursor_ttl_seconds: int = 300,
    ) -> None:
        if (
            not callable(sign)
            or type(cursor_ttl_seconds) is not int
            or not 1 <= cursor_ttl_seconds <= 3600
        ):
            raise InvalidInputError(
                "Receipt service requires host signing and a bounded cursor lifetime"
            )
        self.db, self.artifacts, self.actor, self.policy = database, artifacts, actor, policy
        self.sign, self.ttl = sign, cursor_ttl_seconds
        self.verification = VerificationPolicy(
            expected_signer_hash=digest(signer_hash),
            audience=AUDIENCE,
            workspace_id=str(actor.workspace_id),
            domain="potbot.event.v2",
            allowed_payload_type_refs=frozenset((RECEIPT,)),
            known_type_refs=known_types,
        )

    def _now(self) -> int:
        with self.db.transaction(self.actor) as connection:
            return int(
                connection.execute(
                    "SELECT floor(extract(epoch FROM clock_timestamp())) AS now"
                ).fetchone()["now"]
            )

    def seal(self, request: object) -> dict[str, Any]:
        value = fields(
            request,
            {"schema_version", "root_run_id", "expected_input_sha256", "expected_policy_hash"},
        )
        root = identifier(value["root_run_id"])
        with self.db.transaction(self.actor) as connection:
            existing = connection.execute(
                "SELECT * FROM execution_provenance_receipts WHERE workspace_id=%s AND root_run_id=%s "
                "AND owner_principal_id=%s",
                (self.actor.workspace_id, root, self.actor.principal_id),
            ).fetchone()
        if existing:
            manifest, _ = self._verify(dict(existing), value)
            return self._summary(dict(existing), manifest)
        trace = self._trace(root, value)
        raw_trace = canonical_json(trace).encode("utf-8")
        if len(raw_trace) > MAX_TRACE_BYTES:
            raise InvalidInputError("Trace exceeds the retained 16 MiB receipt bound")
        trace_artifact = self.artifacts.put(self.actor, raw_trace, "application/json")
        root_binding = trace["runs"][0]
        body = {
            "schema_version": 1,
            "type_ref": RECEIPT,
            "workspace_id": str(self.actor.workspace_id),
            "root_run_id": str(root),
            "catalog_hash": root_binding["scope"]["catalog_hash"],
            "policy_hash": self.policy.fingerprint,
            "input_sha256": root_binding["input_sha256"],
            "root_scope_hash": root_binding["scope_hash"],
            "trace_sha256": hashlib.sha256(raw_trace).hexdigest(),
            "trace_artifact_id": str(trace_artifact["id"]),
            "trace_bytes": len(raw_trace),
            "trace_media_type": "application/json",
            "trace_encoding": "utf-8",
            "trace_complete": True,
            "run_count": len(trace["runs"]),
            "event_count": len(trace["events"]),
            "root_status": trace["root_status"],
            "outcome_verification": "not_assessed",
        }
        raw_body = canonical_json(body).encode("utf-8")
        body_hash = hashlib.sha256(raw_body).hexdigest()
        signing_request = {
            "workspace_id": str(self.actor.workspace_id),
            "audience": AUDIENCE,
            "domain": "potbot.event.v2",
            "payload_type_ref": RECEIPT,
            "payload_sha256": body_hash,
            "request_id": str(uuid4()),
            "validity_seconds": 120,
        }
        # No DB lock survives an interactive native signing operation.
        signed = self.sign(signing_request)
        verified_at = self._now()
        verified = verify_signature_bundle_digest(
            signed, body_hash, policy=self.verification, now=verified_at
        )
        if (
            verified.request_id != signing_request["request_id"]
            or verified.expires_at - verified.issued_at != 120
        ):
            raise StateMismatchError("Signer returned a different authorized request")
        body_artifact = self.artifacts.put(self.actor, raw_body, "application/json")
        signature_artifact = self.artifacts.put(
            self.actor, canonical_json(signed).encode("utf-8"), "application/json"
        )
        with self.db.transaction(self.actor) as connection:
            row = connection.execute(
                "INSERT INTO execution_provenance_receipts(workspace_id,id,root_run_id,owner_principal_id,"
                "body_artifact_id,trace_artifact_id,signature_artifact_id,body_sha256,trace_sha256,signer_hash,verified_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(workspace_id,root_run_id) DO NOTHING RETURNING *",
                (
                    self.actor.workspace_id,
                    uuid4(),
                    root,
                    self.actor.principal_id,
                    body_artifact["id"],
                    trace_artifact["id"],
                    signature_artifact["id"],
                    body_hash,
                    body["trace_sha256"],
                    self.verification.expected_signer_hash,
                    verified_at,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM execution_provenance_receipts WHERE workspace_id=%s AND root_run_id=%s "
                    "AND owner_principal_id=%s",
                    (self.actor.workspace_id, root, self.actor.principal_id),
                ).fetchone()
                if row is None or row["body_sha256"] != body_hash:
                    raise StateMismatchError(
                        "Concurrent receipt differs from the pinned completed trace"
                    )
        return self._summary(dict(row), body)

    def _trace(self, root: UUID, expected: dict[str, Any]) -> dict[str, Any]:
        with self.db.transaction(self.actor) as connection:
            ExecutionEventService._lock_cursor(connection, self.actor)
            rows = connection.execute(
                "WITH RECURSIVE family AS (SELECT c.*,0 AS depth FROM execution_run_contracts c "
                "WHERE workspace_id=%s AND run_id=%s AND owner_principal_id=%s UNION ALL "
                "SELECT c.*,f.depth+1 FROM execution_run_contracts c JOIN family f ON c.workspace_id=f.workspace_id "
                "AND c.parent_run_id=f.run_id WHERE f.depth<16) SELECT * FROM family ORDER BY depth,run_id LIMIT %s",
                (self.actor.workspace_id, root, self.actor.principal_id, MAX_RUNS + 1),
            ).fetchall()
            if not rows or len(rows) > MAX_RUNS or rows[0]["parent_run_id"] is not None:
                raise ScopeDeniedError("Receipt requires a bounded authenticated root lineage")
            runs = [dict(row) for row in rows]
            omitted = connection.execute(
                "SELECT 1 FROM execution_run_contracts WHERE workspace_id=%s AND parent_run_id=ANY(%s) "
                "AND NOT(run_id=ANY(%s)) LIMIT 1",
                (self.actor.workspace_id, [r["run_id"] for r in runs], [r["run_id"] for r in runs]),
            ).fetchone()
            if omitted:
                raise ScopeDeniedError("Lineage exceeds its complete traversal bound")
            self._expected(runs[0]["input_sha256"], runs[0]["scope"]["policy_hash"], expected)
            events = connection.execute(
                "SELECT * FROM syn_execution_events WHERE workspace_id=%s "
                "AND run_id=ANY(%s) ORDER BY sequence LIMIT %s",
                (self.actor.workspace_id, [r["run_id"] for r in runs], MAX_EVENTS + 1),
            ).fetchall()
        if len(events) > MAX_EVENTS:
            raise InvalidInputError("Trace exceeds its 2048-event receipt bound")
        last = {e["run_id"]: e for e in events}
        if len(last) != len(runs) or any(last[r["run_id"]]["stage"] not in TERMINAL for r in runs):
            raise ScopeDeniedError("Partial work cannot be sealed as a complete trace")
        for run in runs:
            if (
                run["owner_principal_id"] != self.actor.principal_id
                or content_hash(run["scope"]) != run["scope_hash"]
            ):
                raise StateMismatchError("Lineage scope is not intact")
            data = self.artifacts.read(self.actor, run["input_artifact_id"])
            if hashlib.sha256(data).hexdigest() != run["input_sha256"]:
                raise StateMismatchError("Trace input artifact changed")
        for event in events:
            if event["output_artifact_ref"]:
                data = self.artifacts.read(
                    self.actor, UUID(event["output_artifact_ref"].removeprefix("artifact:"))
                )
                if hashlib.sha256(data).hexdigest() != event["output_sha256"]:
                    raise StateMismatchError("Trace output artifact changed")
        from potbot.controller.tasks import json_record

        return {
            "schema_version": 1,
            "root_run_id": str(root),
            "root_status": last[root]["stage"],
            "runs": [
                json_record(
                    {
                        k: r[k]
                        for k in (
                            "run_id",
                            "scope_hash",
                            "scope",
                            "input_artifact_id",
                            "input_sha256",
                            "parent_run_id",
                            "parent_event_id",
                            "emitter_run_id",
                        )
                    }
                )
                for r in runs
            ],
            "events": [ExecutionEventService._public(dict(e)) for e in events],
        }

    def _expected(self, original: str, policy: str, expected: dict[str, Any]) -> None:
        if (
            digest(expected["expected_input_sha256"]) != original
            or digest(expected["expected_policy_hash"]) != policy
            or policy != self.policy.fingerprint
        ):
            raise StateMismatchError("Changed input or policy invalidates historical receipt reuse")

    def _verify(
        self, row: dict[str, Any], expected: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        body_raw = self.artifacts.read(self.actor, row["body_artifact_id"])
        trace_raw = self.artifacts.read(self.actor, row["trace_artifact_id"])
        signature = self.artifacts.read(self.actor, row["signature_artifact_id"])
        if (
            hashlib.sha256(body_raw).hexdigest() != row["body_sha256"]
            or hashlib.sha256(trace_raw).hexdigest() != row["trace_sha256"]
        ):
            raise StateMismatchError("Retained receipt bytes do not match their immutable digests")
        # Trusted DB acceptance time proves historical validity, not fresh permission.
        verify_signature_bundle_digest(
            signature, row["body_sha256"], policy=self.verification, now=row["verified_at"]
        )
        body, trace = json.loads(body_raw), json.loads(trace_raw)
        self._expected(body["input_sha256"], body["policy_hash"], expected)
        if (
            body["trace_sha256"] != row["trace_sha256"]
            or body["trace_artifact_id"] != str(row["trace_artifact_id"])
            or body["trace_bytes"] != len(trace_raw)
            or body["root_run_id"] != str(row["root_run_id"])
            or body["event_count"] != len(trace["events"])
        ):
            raise StateMismatchError("Signed manifest and retained trace disagree")
        return body, trace

    @staticmethod
    def _summary(row: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "receipt_id": str(row["id"]),
            "body_sha256": row["body_sha256"],
            "trace_sha256": row["trace_sha256"],
            "trace_artifact_ref": f"artifact:{row['trace_artifact_id']}",
            "signature_artifact_ref": f"artifact:{row['signature_artifact_id']}",
            "signature_status": "valid_at_seal",
            "outcome_verification": "not_assessed",
            "root_status": body["root_status"],
            "event_count": body["event_count"],
            "run_count": body["run_count"],
            "trace_complete": True,
            "reexecution_authorized": False,
        }

    def replay(self, request: object) -> dict[str, Any]:
        value = fields(
            request,
            {
                "schema_version",
                "receipt_id",
                "expected_input_sha256",
                "expected_policy_hash",
                "limit",
            },
            {"cursor"},
        )
        receipt_id = identifier(value["receipt_id"])
        if type(value["limit"]) is not int or not 1 <= value["limit"] <= 8:
            raise InvalidInputError("Historical replay pages contain 1..8 compact events")
        after = 0
        with self.db.transaction(self.actor) as connection:
            row = connection.execute(
                "SELECT * FROM execution_provenance_receipts WHERE workspace_id=%s AND id=%s "
                "AND owner_principal_id=%s",
                (self.actor.workspace_id, receipt_id, self.actor.principal_id),
            ).fetchone()
            if row is None:
                raise ScopeDeniedError("Receipt is unavailable in this authenticated scope")
            if "cursor" in value:
                cursor = connection.execute(
                    "SELECT * FROM execution_receipt_cursors WHERE workspace_id=%s AND id=%s "
                    "AND receipt_id=%s AND owner_principal_id=%s AND expires_at>clock_timestamp()",
                    (
                        self.actor.workspace_id,
                        identifier(value["cursor"]),
                        receipt_id,
                        self.actor.principal_id,
                    ),
                ).fetchone()
                if (
                    cursor is None
                    or cursor["body_sha256"] != row["body_sha256"]
                    or cursor["policy_hash"] != self.policy.fingerprint
                ):
                    raise ScopeDeniedError(
                        "Receipt cursor is expired, foreign or from another revision"
                    )
                after = cursor["after_index"]
        body, trace = self._verify(dict(row), value)
        selected = trace["events"][after : after + value["limit"]]
        items = [
            {
                k: e[k]
                for k in (
                    "sequence",
                    "event_ref",
                    "run_id",
                    "parent_run_id",
                    "parent_event_id",
                    "stage",
                    "trigger_input_sha256",
                    "output_sha256",
                    "elapsed_ms",
                )
            }
            | {
                "work_kind": e["telemetry"]["work_kind"],
                "metrics": e["telemetry"]["metrics"],
            }
            for e in selected
        ]
        end, next_cursor = after + len(items), None
        if end < len(trace["events"]):
            next_cursor = uuid4()
            with self.db.transaction(self.actor) as connection:
                connection.execute(
                    "INSERT INTO execution_receipt_cursors(workspace_id,id,receipt_id,owner_principal_id,"
                    "body_sha256,policy_hash,after_index,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,clock_timestamp()+(%s*interval '1 second'))",
                    (
                        self.actor.workspace_id,
                        next_cursor,
                        receipt_id,
                        self.actor.principal_id,
                        row["body_sha256"],
                        self.policy.fingerprint,
                        end,
                        self.ttl,
                    ),
                )
        return {
            **self._summary(dict(row), body),
            "items": items,
            "after_index": after,
            "next_cursor": str(next_cursor) if next_cursor else None,
            "page_complete": end == len(trace["events"]),
            "page_qualification": "bounded_projection_of_retained_trace",
        }
