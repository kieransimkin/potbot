"""Source-byte replay accepts exact evidence only; it is not code verification."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from potbot.domain.errors import StateMismatchError
from potbot.domain.serialization import canonical_json
from potbot.research.contracts import ResearchOperation
from tests.unit.test_phase7_research import action, context, inspector, query

from potbot.controller.research_audit import replay_source_batch


def _bundle(tmp_path):
    scope = context()
    _root, reader, snapshot = inspector(tmp_path, scope)
    item = action(
        scope=scope,
        q=query(
            operation=ResearchOperation.READ_FILE,
            locator="src/example.py",
            search="whole_file",
            required=(),
        ),
    )
    run_id, source_id = uuid4(), uuid4()
    observations = reader.inspect(item, now=datetime(2026, 10, 22, tzinfo=UTC))
    batch = {
        "schema_version": 1,
        "primary_type_ref": "synny://type/potbot-research-observation-batch",
        "run_id": str(run_id),
        "action_id": str(item.action_id),
        "action_identity": item.identity,
        "source_artifact_id": str(source_id),
        "source_revision": snapshot.content_digest,
        "observations": [value.to_dict() for value in observations],
    }
    return item, snapshot, run_id, batch


def test_exact_source_replay_is_accepted_without_promoting_fix(tmp_path):
    item, snapshot, run_id, batch = _bundle(tmp_path)
    result, observed = replay_source_batch(
        item, snapshot, run_id, canonical_json(batch).encode("utf-8")
    )
    assert result == batch
    assert len(observed) == 1
    assert observed[0].excerpt is not None


@pytest.mark.parametrize("changed", ["excerpt", "content_digest", "line_start", "fingerprint"])
def test_fabricated_result_with_real_snapshot_is_rejected(tmp_path, changed):
    item, snapshot, run_id, batch = _bundle(tmp_path)
    batch["observations"][0][changed] = "forged" if changed != "line_start" else 2
    with pytest.raises(StateMismatchError):
        replay_source_batch(item, snapshot, run_id, canonical_json(batch).encode("utf-8"))


def test_replay_rejects_identity_swap_noncanonical_json_and_wrong_version(tmp_path):
    item, snapshot, run_id, batch = _bundle(tmp_path)
    for mutation in (
        lambda b: b.update({"action_id": str(uuid4())}),
        lambda b: b.update({"run_id": str(uuid4())}),
        lambda b: b.update({"source_revision": "a" * 64}),
        lambda b: b.update({"schema_version": 2}),
        lambda b: b.update({"observations": []}),
    ):
        test = json.loads(canonical_json(batch))
        mutation(test)
        with pytest.raises(StateMismatchError):
            replay_source_batch(item, snapshot, run_id, canonical_json(test).encode())
    with pytest.raises(StateMismatchError):
        replay_source_batch(item, snapshot, run_id, b'{ "schema_version": 1 }')
    with pytest.raises(StateMismatchError):
        replay_source_batch(item, snapshot, run_id, b"{}{}")
    with pytest.raises(StateMismatchError):
        replay_source_batch(
            replace(item, action_id=uuid4()),
            snapshot,
            run_id,
            canonical_json(batch).encode("utf-8"),
        )
