"""Real PostgreSQL + installed SYN upload/read tools, authority and durable monitoring."""

import asyncio
import base64
import hashlib
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import potbot.cogs.artifact_uploads as upload_bindings
from potbot import __version__
from potbot.api.server import create_app
from potbot.cogs.artifact_ranges import OPEN as OPEN_READ
from potbot.cogs.artifact_ranges import READ
from potbot.cogs.artifact_uploads import ABORT, CHUNK, COMMIT, INSPECT, OPEN, UPLOAD_COGS
from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.cogs.execution import WORK_COGS
from potbot.cogs.registry import BoundCogRegistry
from potbot.controller.artifact_ranges import ArtifactRangeService
from potbot.controller.artifact_uploads import ArtifactUploadService
from potbot.controller.execution_cogs import ExecutionCogRuntime, HostOperation
from potbot.controller.execution_events import ExecutionEventService
from potbot.domain.errors import (
    InvalidInputError,
    OperationTimeoutError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.serialization import content_hash
from potbot.persistence.artifacts import ArtifactIntegrityError
from potbot.semantics.service import SynService
from potbot.tools.artifact_ranges import MAX_RANGE_BYTES
from potbot.tools.artifact_uploads import PAGE_SLOTS, schemas
from potbot.tools.execution_contracts import (
    MAX_INLINE_BYTES,
    WORK_KINDS,
    ExecutionPolicy,
    WorkBinding,
)
from potbot.tools.semantic_contracts import QUERY, RESULTS
from tests.integration.test_repository_states import setup

pytestmark = pytest.mark.postgres
ROOT = Path(__file__).resolve().parents[2]
SOURCES = (
    "src/potbot/tools/artifact_uploads.py",
    "src/potbot/cogs/artifact_uploads.py",
    "src/potbot/controller/artifact_uploads.py",
    "src/potbot/persistence/artifact_uploads_schema.py",
    "src/potbot/persistence/artifacts.py",
    "src/potbot/controller/execution_cogs.py",
    "src/potbot/controller/execution_journal.py",
    "src/potbot/cogs/registry.py",
    "src/potbot/tools/semantic_contracts.py",
)


def test_real_upload_cogs_resume_commit_read_and_reject_invalid_authority(
    app_env, tmp_path, monkeypatch
):
    _, artifacts, _, _, state, _ = setup(app_env, tmp_path)
    registry = BoundCogRegistry.artifact_uploads()
    sealed = SynService(app_env.db, artifacts).import_snapshot(
        app_env.context, app_env.repository_id, "artifact-upload-v1", registry.snapshot
    )
    snapshot = UUID(sealed["scope"]["snapshot_id"])
    policy = ExecutionPolicy("a" * 64, "b" * 64, state["content_digest"], __version__)
    raw = (
        b"".join(hashlib.sha256(f"fixture:{i}".encode()).digest() for i in range(4097))
        + b"\xff upload end\x00"
    )
    sha = hashlib.sha256(raw).hexdigest()
    task, revoked, uploaded, negatives = uuid4(), [], [], []
    request = {
        "schema_version": 1,
        "upload_id": str(uuid4()),
        "artifact_sha256": sha,
        "byte_length": len(raw),
        "media_type": "application/octet-stream",
        "ttl_seconds": 300,
    }
    pin = {k: request[k] for k in ("schema_version", "upload_id", "artifact_sha256")}

    def admit(*args):
        if revoked:
            raise ScopeDeniedError("Fixture host grant revoked")

    def chunk_request(pinned, data, offset):
        chunk = data[offset : offset + MAX_RANGE_BYTES]
        return {
            **pinned,
            "offset": offset,
            "chunk_sha256": hashlib.sha256(chunk).hexdigest(),
            "data_base64": base64.b64encode(chunk).decode("ascii"),
        }

    async def workflow(call, source):
        await call.child_cog(str(OPEN.cog_ref), TypedCogValue(OPEN.input_type_ref, request))
        for offset in (0, MAX_RANGE_BYTES):
            source_chunk = chunk_request(pin, raw, offset)
            out = await call.child_cog(
                str(CHUNK.cog_ref), TypedCogValue(CHUNK.input_type_ref, source_chunk)
            )
            uploaded.append({**source_chunk, **out.value.value})
        call.progress("first_two_chunks_retained")
        return TypedCogValue(
            RESULTS,
            {
                "schema_version": 1,
                "query_hash": source.fingerprint,
                "corpus_hash": sha,
                "items": [],
                "complete": True,
            },
        )

    operation = HostOperation(
        WorkBinding(
            "workflow",
            "syn://thing/fixture-artifact-upload",
            QUERY,
            RESULTS,
            content_hash({"handler": "fixture-artifact-upload"}),
            "idempotent_write",
            policy,
        ),
        workflow,
    )

    def runtime():
        return ExecutionCogRuntime(
            database=app_env.db,
            artifacts=artifacts,
            actor=app_env.context,
            snapshot_id=snapshot,
            task_revision_id=task,
            repository_state_id=state["id"],
            emitter_run_id=uuid4(),
            policy=policy,
            operations=(operation,),
            admit=admit,
            artifact_ranges=ArtifactRangeService(
                app_env.db, artifacts, app_env.context, policy=policy
            ),
            artifact_uploads=ArtifactUploadService(
                app_env.db, artifacts, app_env.context, policy=policy
            ),
        )

    host = runtime()

    def invoke(signature, value, selected=None):
        target = selected or host
        context = CogRunContext(
            registry.snapshot.fingerprint,
            task,
            state["id"],
            uuid4(),
        )
        return asyncio.run(
            registry.invoke(
                str(signature.cog_ref),
                TypedCogValue(signature.input_type_ref, value),
                context,
                controller=target,
            )
        ).value.value

    def reject(name, exception, signature, value):
        nonlocal host
        with pytest.raises(exception):
            invoke(signature, value)
        if host._poisoned:
            # Preserve the existing conservative write fence; a trusted new
            # controller inspects durable session state before continuing.
            with pytest.raises(ScopeDeniedError):
                invoke(INSPECT, {**pin, "cursor": 0})
            host = runtime()
            if not revoked:
                current_pin = {k: value[k] for k in pin}
                assert invoke(INSPECT, {**current_pin, "cursor": 0})["state"] in {
                    "open",
                    "committed",
                    "aborted",
                    "expired",
                }
        negatives.append(name)

    original_publish, original_read = artifacts.backend.publish, artifacts.backend.read

    def bounded_publish(workspace, data):
        assert data != raw, "Upload commit used the ordinary whole-object buffer publisher"
        return original_publish(workspace, data)

    def bounded_read(workspace, artifact):
        assert artifact.digest != sha, "Readback used the ordinary whole-object buffer reader"
        return original_read(workspace, artifact)

    monkeypatch.setattr(artifacts.backend, "publish", bounded_publish)
    monkeypatch.setattr(artifacts.backend, "read", bounded_read)
    flow = WORK_COGS[WORK_KINDS.index("workflow")]
    invoke(
        flow,
        {
            "schema_version": 1,
            "operation_ref": operation.binding.operation_ref,
            "input": {
                "type_ref": QUERY,
                "value": {
                    "schema_version": 1,
                    "text": "Upload retained binary evidence",
                    "limit": 1,
                },
            },
        },
    )
    original_open = invoke(OPEN, request)
    host = runtime()  # New controller/service instances, in this same test process.
    assert invoke(OPEN, request) == original_open
    missing = invoke(INSPECT, {**pin, "cursor": 0})
    offsets = list(range(2 * MAX_RANGE_BYTES, len(raw), MAX_RANGE_BYTES))
    assert missing["missing_offsets"] == offsets and missing["received_chunks"] == 2
    reject("incomplete_commit", StateMismatchError, COMMIT, pin)
    reject("conflicting_open", StateMismatchError, OPEN, {**request, "media_type": "text/plain"})
    for offset in reversed(offsets):
        value = chunk_request(pin, raw, offset)
        uploaded.append({**value, **invoke(CHUNK, value)})
    value = chunk_request(pin, raw, 0)
    uploaded.append({**value, **invoke(CHUNK, value)})
    assert uploaded[-1]["duplicate"]
    assert invoke(INSPECT, {**pin, "cursor": 0})["missing_offsets"] == []
    altered = bytes([raw[0] ^ 1]) + raw[1:MAX_RANGE_BYTES]
    reject("conflicting_duplicate", StateMismatchError, CHUNK, chunk_request(pin, altered, 0))
    reject(
        "changed_revision",
        StateMismatchError,
        INSPECT,
        {**pin, "artifact_sha256": "c" * 64, "cursor": 0},
    )
    reject("invalid_chunk_hash", InvalidInputError, CHUNK, {**value, "chunk_sha256": "0" * 64})
    reject("unaligned_offset", InvalidInputError, CHUNK, {**value, "offset": 1})
    reject("partial_slot", InvalidInputError, CHUNK, chunk_request(pin, b"x", 0))
    reject("oversized_chunk", InvalidInputError, CHUNK, {**value, "data_base64": "A" * 20000})
    reject("invalid_encoding", InvalidInputError, CHUNK, {**value, "data_base64": "!!"})
    with pytest.raises(ScopeDeniedError):
        ArtifactUploadService(
            app_env.other_db, artifacts, app_env.other_context, policy=policy
        ).inspect({**pin, "cursor": 0}, authorize=lambda: None)
    negatives.append("foreign_workspace")
    with pytest.raises(StateMismatchError):
        ArtifactUploadService(
            app_env.db,
            artifacts,
            app_env.context,
            policy=replace(policy, configuration_hash="c" * 64),
        ).inspect({**pin, "cursor": 0}, authorize=lambda: None)
    negatives.append("changed_policy")

    # Competing clients must both see one identical committed terminal receipt.
    barrier = Barrier(2)

    def concurrent_commit():
        selected = runtime()
        barrier.wait(timeout=15)
        return invoke(COMMIT, pin, selected)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(concurrent_commit) for _ in range(2)]
        committed = [f.result(timeout=60) for f in futures]
    assert committed[0] == committed[1] == invoke(COMMIT, pin)
    artifact_id = UUID(committed[0]["artifact_id"])
    reject("abort_after_commit", StateMismatchError, ABORT, pin)
    reject("chunk_after_commit", ScopeDeniedError, CHUNK, value)
    read_pin = {"schema_version": 1, "lease_id": str(uuid4()), "artifact_sha256": sha}
    invoke(
        OPEN_READ,
        {**read_pin, "artifact_id": str(artifact_id), "byte_length": len(raw), "ttl_seconds": 300},
    )
    readback = [
        invoke(READ, {**read_pin, "offset": off, "length": MAX_RANGE_BYTES})
        for off in range(0, len(raw), MAX_RANGE_BYTES)
    ]
    reconstructed = b"".join(base64.b64decode(c["data_base64"], validate=True) for c in readback)
    assert reconstructed == raw

    def new_upload(data, *, expected=None, ttl=300):
        selected = {
            **request,
            "upload_id": str(uuid4()),
            "artifact_sha256": expected or hashlib.sha256(data).hexdigest(),
            "byte_length": len(data),
            "ttl_seconds": ttl,
        }
        invoke(OPEN, selected)
        return {k: selected[k] for k in pin}

    aborted = new_upload(b"aborted bytes")
    assert invoke(ABORT, aborted) == invoke(ABORT, aborted)
    assert invoke(INSPECT, {**aborted, "cursor": 0})["state"] == "aborted"
    reject("commit_after_abort", ScopeDeniedError, COMMIT, aborted)
    reject(
        "chunk_after_abort", ScopeDeniedError, CHUNK, chunk_request(aborted, b"aborted bytes", 0)
    )
    expired = new_upload(b"expired bytes", ttl=1)
    with app_env.db.transaction(app_env.context) as connection:
        connection.execute("SELECT pg_sleep(1.1)")
    assert invoke(INSPECT, {**expired, "cursor": 0})["state"] == "expired"
    reject("expired_upload", ScopeDeniedError, CHUNK, chunk_request(expired, b"expired bytes", 0))
    assert invoke(ABORT, expired)["state"] == "aborted"

    wrong = new_upload(b"whole digest mismatch", expected="d" * 64)
    invoke(CHUNK, chunk_request(wrong, b"whole digest mismatch", 0))
    reject("whole_digest_mismatch", ArtifactIntegrityError, COMMIT, wrong)
    assert invoke(INSPECT, {**wrong, "cursor": 0})["state"] == "open"
    assert not (
        artifacts.backend.root / artifacts.backend.locator(app_env.context.workspace_id, "d" * 64)
    ).exists()

    # Withdrawal after staging must roll back the SQL slot; no fabricated success.
    revoked_raw = b"host grant withdrawn after staging\xff"
    revoke_pin = new_upload(revoked_raw)

    def revoke_publish(workspace, data):
        result = bounded_publish(workspace, data)
        if data == revoked_raw:
            revoked.append(True)
        return result

    monkeypatch.setattr(artifacts.backend, "publish", revoke_publish)
    reject(
        "host_revoked_during_chunk",
        ScopeDeniedError,
        CHUNK,
        chunk_request(revoke_pin, revoked_raw, 0),
    )
    revoked.clear()
    monkeypatch.setattr(artifacts.backend, "publish", bounded_publish)
    assert invoke(INSPECT, {**revoke_pin, "cursor": 0})["received_chunks"] == 0

    commit_raw = b"first commit chunk\x00" * 700
    commit_pin = new_upload(commit_raw)
    for off in range(0, len(commit_raw), MAX_RANGE_BYTES):
        invoke(CHUNK, chunk_request(commit_pin, commit_raw, off))
    first_sha = hashlib.sha256(commit_raw[:MAX_RANGE_BYTES]).hexdigest()

    def revoke_read(workspace, artifact):
        data = bounded_read(workspace, artifact)
        if artifact.digest == first_sha:
            revoked.append(True)
        return data

    monkeypatch.setattr(artifacts.backend, "read", revoke_read)
    reject("host_revoked_during_commit", ScopeDeniedError, COMMIT, commit_pin)
    revoked.clear()
    monkeypatch.setattr(artifacts.backend, "read", bounded_read)
    assert invoke(INSPECT, {**commit_pin, "cursor": 0})["state"] == "open"
    assert not (
        artifacts.backend.root
        / artifacts.backend.locator(app_env.context.workspace_id, commit_pin["artifact_sha256"])
    ).exists()
    assert not list(artifacts.backend.root.rglob(".pending-*"))
    original_clock = upload_bindings.monotonic
    clock_shift = [0.0]
    monkeypatch.setattr(upload_bindings, "monotonic", lambda: original_clock() + clock_shift[0])

    def overdue_read(workspace, artifact):
        data = bounded_read(workspace, artifact)
        if artifact.digest == first_sha:
            clock_shift[0] = policy.max_wall_ms / 1000 + 1
        return data

    monkeypatch.setattr(artifacts.backend, "read", overdue_read)
    reject("injected_deadline_during_commit", OperationTimeoutError, COMMIT, commit_pin)
    clock_shift[0] = 0
    monkeypatch.setattr(upload_bindings, "monotonic", original_clock)
    monkeypatch.setattr(artifacts.backend, "read", bounded_read)
    assert invoke(INSPECT, {**commit_pin, "cursor": 0})["state"] == "open"
    invoke(COMMIT, commit_pin)

    damaged_raw = b"staged source integrity\x00" * 600
    damaged_pin = new_upload(damaged_raw)
    for off in range(0, len(damaged_raw), MAX_RANGE_BYTES):
        invoke(CHUNK, chunk_request(damaged_pin, damaged_raw, off))
    first = damaged_raw[:MAX_RANGE_BYTES]
    staged_path = artifacts.backend.root / artifacts.backend.locator(
        app_env.context.workspace_id, hashlib.sha256(first).hexdigest()
    )
    staged_path.write_bytes(b"!" + first[1:])
    reject("corrupt_staged_bytes", ArtifactIntegrityError, COMMIT, damaged_pin)
    staged_path.unlink()
    reject("missing_staged_bytes", ArtifactIntegrityError, COMMIT, damaged_pin)
    assert invoke(INSPECT, {**damaged_pin, "cursor": 0})["state"] == "open"
    staged_path.write_bytes(first)
    invoke(COMMIT, damaged_pin)

    # A source larger than one status page never returns an unbounded slot list.
    large = {
        **request,
        "upload_id": str(uuid4()),
        "byte_length": (PAGE_SLOTS + 1) * MAX_RANGE_BYTES,
    }
    invoke(OPEN, large)
    large_pin = {k: large[k] for k in pin}
    page = invoke(INSPECT, {**large_pin, "cursor": 0})
    assert len(page["missing_offsets"]) == PAGE_SLOTS and page["next_cursor"] == PAGE_SLOTS
    tail = invoke(INSPECT, {**large_pin, "cursor": page["next_cursor"]})
    assert tail["missing_offsets"] == [PAGE_SLOTS * MAX_RANGE_BYTES] and tail["next_cursor"] is None
    # Empty files still require a verified SHA and a committed terminal receipt.
    empty = new_upload(b"")
    assert invoke(COMMIT, empty)["byte_length"] == 0

    read = ExecutionEventService(app_env.db)
    events, after = [], 0
    while True:
        page = read.events(app_env.context, after=after, limit=200)
        events.extend(page["items"])
        if not page["has_more"]:
            break
        after = page["next_cursor"]
    for run in {e["run_id"] for e in events}:
        rows = [e for e in events if e["run_id"] == run]
        assert rows[0]["stage"] == "started" and rows[-1]["stage"] in {
            "completed",
            "failed",
            "indeterminate",
        }
        assert any(e["stage"] == "updated" for e in rows)
        assert rows[-1]["telemetry"]["metrics"]["controller_cpu_ms"]["provenance"] == "measured"
    assert {e["telemetry"]["cog_ref"] for e in events if e["stage"] == "completed"} >= {
        str(s.cog_ref) for s in UPLOAD_COGS
    }
    assert any(
        e["parent_run_id"] is not None and e["telemetry"]["cog_ref"] == str(CHUNK.cog_ref)
        for e in events
    )
    app = create_app(
        task_service=object(),
        job_service=object(),
        authenticate=lambda _: app_env.context,
        execution_events=read,
    )
    with TestClient(app) as client:
        response = client.get(
            "/v1/executions/events?limit=200", headers={"Authorization": "Bearer fixture"}
        )
        assert response.status_code == 200 and response.json()["data"]["items"] == events[:200]
        root = next(e["run_id"] for e in events if e["subject_type_ref"] == flow.cog_type_ref)
        root_events = [e for e in events if e["run_id"] == root]
        with client.websocket_connect(f"/v1/executions/{root}/stream") as stream:
            stream.send_json({"type": "authenticate", "token": "fixture", "after": 0})
            assert stream.receive_json()["type"] == "ready"
            assert [stream.receive_json()["event"] for _ in root_events] == root_events

    target_dir = os.environ.get("POTBOT_UPLOAD_EVIDENCE_DIR")
    if target_dir:
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        evidence = {
            "result": "passed",
            "version": __version__,
            "source_commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "implementation_source": {
                name: hashlib.sha256(
                    subprocess.check_output(["git", "show", f"HEAD:{name}"], cwd=ROOT)
                ).hexdigest()
                for name in SOURCES
            },
            "catalog_hash": registry.snapshot.fingerprint,
            "wire_schema_hashes": {ref: host.catalog.schema_hash(ref) for ref in schemas()},
            "artifact_sha256": sha,
            "artifact_bytes": len(raw),
            "uploaded_chunks": uploaded,
            "readback": readback,
            "reconstructed_sha256": hashlib.sha256(reconstructed).hexdigest(),
            "resume_missing_offsets": missing["missing_offsets"],
            "committed": committed[0],
            "events": events,
            "negative_cases": negatives,
            "chunk_bytes": MAX_RANGE_BYTES,
            "inline_limit_bytes": MAX_INLINE_BYTES,
            "status_page_slots": PAGE_SLOTS,
            "controller_instance_resume": "passed",
            "concurrent_commit": "passed",
            "rest_readback": "passed",
            "websocket_readback": "passed",
            "empty_upload": "passed",
            "qualification": "local_posix_resumable_upload_and_range_read_not_network_crash_or_gc",
        }
        (target / "artifact-uploads.json").write_text(
            json.dumps(evidence, indent=2, default=str), encoding="utf-8"
        )
