"""Real SYN Cogs, SQL read leases, exact bounded bytes and durable event readback."""

import asyncio
import base64
import hashlib
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from potbot import __version__
from potbot.api.server import create_app
from potbot.cogs.artifact_ranges import CLOSE, OPEN, READ
from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.cogs.execution import WORK_COGS
from potbot.cogs.registry import BoundCogRegistry
from potbot.controller.artifact_ranges import ArtifactRangeService
from potbot.controller.execution_cogs import ExecutionCogRuntime, HostOperation
from potbot.controller.execution_events import ExecutionEventService
from potbot.domain.errors import (
    InvalidInputError,
    ScopeDeniedError,
    SourceUnavailableError,
    StateMismatchError,
)
from potbot.domain.serialization import content_hash
from potbot.persistence.artifacts import ArtifactIntegrityError
from potbot.semantics.service import SynService
from potbot.tools.artifact_ranges import MAX_RANGE_BYTES, READ_BLOCK_BYTES, schemas
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
    "src/potbot/tools/artifact_ranges.py",
    "src/potbot/cogs/artifact_ranges.py",
    "src/potbot/controller/artifact_ranges.py",
    "src/potbot/controller/execution_cogs.py",
    "src/potbot/persistence/artifacts.py",
    "src/potbot/persistence/artifact_ranges_schema.py",
)


def test_real_range_cogs_reconstruct_after_restart_and_reject_stale_scope(
    app_env, tmp_path, monkeypatch
):
    _, artifacts, _, _, state, _ = setup(app_env, tmp_path)
    registry = BoundCogRegistry.artifact_ranges()
    sealed = SynService(app_env.db, artifacts).import_snapshot(
        app_env.context, app_env.repository_id, "artifact-range-v1", registry.snapshot
    )
    snapshot = UUID(sealed["scope"]["snapshot_id"])
    policy = ExecutionPolicy("a" * 64, "b" * 64, state["content_digest"], __version__)
    service = ArtifactRangeService(app_env.db, artifacts, app_env.context, policy=policy)
    raw = bytes(range(256)) * 512 + b"\xff end of retained binary evidence\x00"
    artifact = artifacts.put(app_env.context, raw, "application/octet-stream")
    lease_id, task = str(uuid4()), uuid4()
    request = {
        "schema_version": 1,
        "lease_id": lease_id,
        "artifact_id": str(artifact["id"]),
        "artifact_sha256": artifact["digest"],
        "byte_length": len(raw),
        "ttl_seconds": 300,
    }
    pinned = {"schema_version": 1, "lease_id": lease_id, "artifact_sha256": artifact["digest"]}
    chunks, revoked = [], []

    def admit(*args):
        if revoked:
            raise ScopeDeniedError("Fixture host grant revoked")

    async def workflow(call, source):
        await call.child_cog(str(OPEN.cog_ref), TypedCogValue(OPEN.input_type_ref, request))
        for offset in (0, MAX_RANGE_BYTES):
            result = await call.child_cog(
                str(READ.cog_ref),
                TypedCogValue(
                    READ.input_type_ref, {**pinned, "offset": offset, "length": MAX_RANGE_BYTES}
                ),
            )
            chunks.append(result.value.value)
        call.progress("first_two_ranges_retained")
        return TypedCogValue(
            RESULTS,
            {
                "schema_version": 1,
                "query_hash": source.fingerprint,
                "corpus_hash": artifact["digest"],
                "items": [],
                "complete": True,
            },
        )

    operation = HostOperation(
        WorkBinding(
            "workflow",
            "syn://thing/fixture-artifact-reader",
            QUERY,
            RESULTS,
            content_hash({"handler": "fixture-artifact-reader"}),
            "idempotent_write",
            policy,
        ),
        workflow,
    )

    def runtime(selected_service=service):
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
            artifact_ranges=selected_service,
        )

    host = runtime()

    def invoke(signature, value):
        context = CogRunContext(registry.snapshot.fingerprint, task, state["id"], uuid4())
        return asyncio.run(
            registry.invoke(
                str(signature.cog_ref),
                TypedCogValue(signature.input_type_ref, value),
                context,
                controller=host,
            )
        ).value.value

    original_read = artifacts.read

    def no_full_source(context, artifact_id):
        assert artifact_id != artifact["id"], "Range transport called the whole-object reader"
        return original_read(context, artifact_id)

    monkeypatch.setattr(artifacts, "read", no_full_source)
    flow = WORK_COGS[WORK_KINDS.index("workflow")]
    invoke(
        flow,
        {
            "schema_version": 1,
            "operation_ref": operation.binding.operation_ref,
            "input": {
                "type_ref": QUERY,
                "value": {"schema_version": 1, "text": "Open retained binary evidence", "limit": 1},
            },
        },
    )
    # Fresh host/service: only SQL lease state survives the simulated reconnect.
    host = runtime(ArtifactRangeService(app_env.db, artifacts, app_env.context, policy=policy))
    assert invoke(OPEN, request)["lease_id"] == lease_id
    offsets = list(range(2 * MAX_RANGE_BYTES, len(raw), MAX_RANGE_BYTES))
    for offset in reversed(offsets):
        chunks.append(invoke(READ, {**pinned, "offset": offset, "length": MAX_RANGE_BYTES}))
    chunks.append(invoke(READ, {**pinned, "offset": 0, "length": MAX_RANGE_BYTES}))
    pieces = {}
    for chunk in chunks:
        data = base64.b64decode(chunk["data_base64"], validate=True)
        assert len(data) == chunk["returned_length"] <= MAX_RANGE_BYTES
        assert hashlib.sha256(data).hexdigest() == chunk["range_sha256"]
        assert (
            chunk["verified_byte_length"] == len(raw)
            and chunk["maximum_read_request_bytes"] == READ_BLOCK_BYTES
        )
        if chunk["offset"] in pieces:
            assert pieces[chunk["offset"]] == data
        pieces[chunk["offset"]] = data
    reconstructed = b"".join(pieces[offset] for offset in sorted(pieces))
    assert reconstructed == raw and hashlib.sha256(reconstructed).hexdigest() == artifact["digest"]
    assert invoke(READ, {**pinned, "offset": len(raw), "length": 1})["returned_length"] == 0

    with pytest.raises(StateMismatchError):
        invoke(READ, {**pinned, "artifact_sha256": "c" * 64, "offset": 0, "length": 1})
    with pytest.raises(InvalidInputError):
        invoke(READ, {**pinned, "offset": 0, "length": MAX_RANGE_BYTES + 1})
    foreign = ArtifactRangeService(
        app_env.other_db, artifacts, app_env.other_context, policy=policy
    )
    with pytest.raises(ScopeDeniedError):
        foreign.read({**pinned, "offset": 0, "length": 1})
    changed = ArtifactRangeService(
        app_env.db, artifacts, app_env.context, policy=replace(policy, configuration_hash="c" * 64)
    )
    with pytest.raises(StateMismatchError):
        changed.read({**pinned, "offset": 0, "length": 1})
    path = artifacts.backend.root / artifact["locator"]
    with path.open("r+b") as stream:
        stream.seek(len(raw) - 1)
        stream.write(b"!")
    with pytest.raises(ArtifactIntegrityError):
        invoke(READ, {**pinned, "offset": 0, "length": 1})
    path.write_bytes(raw)
    assert invoke(CLOSE, pinned) == invoke(CLOSE, pinned)
    with pytest.raises(ScopeDeniedError):
        invoke(READ, {**pinned, "offset": 0, "length": 1})
    assert artifacts.metadata(app_env.context, artifact["id"])["digest"] == artifact["digest"]
    short = {**request, "lease_id": str(uuid4()), "ttl_seconds": 1}
    invoke(OPEN, short)
    with app_env.db.transaction(app_env.context) as connection:
        connection.execute("SELECT pg_sleep(1.1)")
    with pytest.raises(ScopeDeniedError):
        invoke(READ, {**pinned, "lease_id": short["lease_id"], "offset": 0, "length": 1})
    racing = {**request, "lease_id": str(uuid4())}
    invoke(OPEN, racing)
    original_range = artifacts.read_range

    def close_during_io(*args, **kwargs):
        result = original_range(*args, **kwargs)
        service.close({**pinned, "lease_id": racing["lease_id"]})
        return result

    with monkeypatch.context() as patch:
        patch.setattr(artifacts, "read_range", close_during_io)
        with pytest.raises(ScopeDeniedError):
            invoke(READ, {**pinned, "lease_id": racing["lease_id"], "offset": 0, "length": 1})
    missing = {**request, "lease_id": str(uuid4())}
    grant_race = {**request, "lease_id": str(uuid4())}
    invoke(OPEN, grant_race)

    def revoke_grant_during_io(*args, **kwargs):
        result = original_range(*args, **kwargs)
        revoked.append(True)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(artifacts, "read_range", revoke_grant_during_io)
        with pytest.raises(ScopeDeniedError):
            invoke(READ, {**pinned, "lease_id": grant_race["lease_id"], "offset": 0, "length": 1})
    revoked.clear()
    invoke(CLOSE, {**pinned, "lease_id": grant_race["lease_id"]})
    invoke(OPEN, missing)
    path.unlink()
    with pytest.raises(SourceUnavailableError):
        invoke(READ, {**pinned, "lease_id": missing["lease_id"], "offset": 0, "length": 1})
    path.write_bytes(raw)
    revoked.append(True)
    with pytest.raises(ScopeDeniedError):
        invoke(OPEN, {**request, "lease_id": str(uuid4())})

    events, after = [], 0
    read = ExecutionEventService(app_env.db)
    while True:
        page = read.events(app_env.context, after=after, limit=200)
        events.extend(page["items"])
        if not page["has_more"]:
            break
        after = page["next_cursor"]
    for run_id in {e["run_id"] for e in events}:
        rows = [e for e in events if e["run_id"] == run_id]
        if len(rows) == 1:
            assert rows[0]["stage"] == "failed"
            continue
        assert rows[0]["stage"] == "started" and rows[-1]["stage"] in {"completed", "failed"}
        assert any(e["stage"] == "updated" for e in rows)
        assert rows[-1]["telemetry"]["metrics"]["elapsed_wall_ms"]["provenance"] == "measured"
    assert any(
        e["parent_run_id"] is not None and e["telemetry"]["cog_ref"] == str(READ.cog_ref)
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
    output = os.environ.get("POTBOT_RANGE_EVIDENCE_DIR")
    if output:
        target = Path(output)
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
            "artifact_sha256": artifact["digest"],
            "artifact_bytes": len(raw),
            "reconstructed_sha256": hashlib.sha256(reconstructed).hexdigest(),
            "chunks": chunks,
            "events": events,
            "maximum_range_bytes": MAX_RANGE_BYTES,
            "inline_limit_bytes": MAX_INLINE_BYTES,
            "maximum_read_request_bytes": READ_BLOCK_BYTES,
            "restart_resume": "passed",
            "rest_readback": "passed",
            "websocket_readback": "passed",
            "negative_cases": [
                "changed_revision",
                "oversized_range",
                "foreign_workspace",
                "changed_policy",
                "tampering_outside_range",
                "closed_lease",
                "expired_lease",
                "revoked_during_io",
                "host_grant_revoked_during_io",
                "missing_bytes",
                "revoked_admission",
            ],
            "qualification": "local_posix_backend_bounded_reads_not_upload_or_physical_retention",
        }
        (target / "artifact-ranges.json").write_text(
            json.dumps(evidence, indent=2, default=str), encoding="utf-8"
        )
