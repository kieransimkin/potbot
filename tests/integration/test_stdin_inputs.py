"""Frozen artifact stdin is scoped, bounded and verified before process dispatch."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from potbot.domain.errors import ScopeDeniedError
from potbot.domain.execution import ActionProposal, CapabilityContext, ToolDefinition, ToolRegistry
from potbot.domain.serialization import canonical_json
from potbot.persistence.artifacts import ArtifactIntegrityError, ArtifactStore
from potbot.tools.gateway import ToolGateway

from .conftest import AppEnvironment
from .test_execution_gateway import PYTHON, reserve, restarted, setup

pytestmark = [pytest.mark.postgres, pytest.mark.sandbox]

CONSUMER = (
    "import hashlib,sys; from pathlib import Path; data=sys.stdin.buffer.read(); "
    "digest=hashlib.sha256(data).hexdigest(); Path('stdin-result.txt').write_text(digest); "
    "counter=Path('stdin-count.txt'); "
    "counter.write_text(str((int(counter.read_text()) if counter.exists() else 0)+1)); "
    "print(str(len(data))+':'+digest)"
)


def definition(*, accepts_stdin: bool = True) -> ToolDefinition:
    return ToolDefinition(
        "consume_stdin",
        "test",
        (PYTHON, "-B", "-c", CONSUMER),
        timeout_ms=5000,
        accepts_stdin=accepts_stdin,
    )


def propose(rig, artifact_id: UUID) -> UUID:
    inputs = rig.store.create_inputs(
        rig.env.context,
        rig.episode_id,
        rig.contract_id,
        rig.state["id"],
        "stdin-inputs",
    )
    action = rig.store.propose(
        rig.env.context,
        UUID(inputs["node_id"]),
        ActionProposal("consume_stdin"),
        "stdin-action-" + uuid4().hex,
        stdin_artifact_id=artifact_id,
    )
    return UUID(action["id"])


def assert_not_dispatched(rig) -> None:
    assert not (rig.path / "stdin-result.txt").exists()
    assert not (rig.path / "stdin-count.txt").exists()
    with rig.env.db.transaction(rig.env.context) as connection:
        assert connection.execute("SELECT count(*) AS n FROM action_runs").fetchone()["n"] == 0
        assert (
            connection.execute("SELECT count(*) AS n FROM action_completions").fetchone()["n"] == 0
        )
    assert list(rig.receipts.root.rglob("*.json")) == []


def test_binary_stdin_is_exact_receipt_bound_and_replayed_without_a_second_process(
    app_env: AppEnvironment,
    tmp_path: Path,
) -> None:
    rig = setup(app_env, tmp_path, tools=(definition(),))
    # Includes NULs and exceeds ordinary pipe capacity, exercising the actual
    # nonblocking supervisor input path rather than a mocked process adapter.
    payload = bytes(range(256)) * 4096
    artifact = rig.artifacts.put(app_env.context, payload)
    action_id = propose(rig, artifact["id"])
    reservation = reserve(rig, "consume_stdin")
    completed = rig.gateway.execute(app_env.context, action_id, rig.lease, reservation)
    assert completed["completion"]["outcome"] == "completed", completed
    digest = hashlib.sha256(payload).hexdigest()
    assert (rig.path / "stdin-result.txt").read_text() == digest
    assert (rig.path / "stdin-count.txt").read_text() == "1"
    output = rig.artifacts.read(
        app_env.context,
        UUID(completed["completion"]["stdout_artifact_id"]),
    )
    assert output == f"{len(payload)}:{digest}\n".encode()
    receipt = rig.receipts.read(app_env.context.workspace_id, UUID(completed["run"]["id"]))
    assert receipt is not None
    assert receipt["stdin_artifact_id"] == str(artifact["id"])
    assert receipt["stdin_digest"] == artifact["digest"] == digest
    assert completed["action"]["stdin_artifact_id"] == str(artifact["id"])
    assert restarted(rig).execute(app_env.context, action_id, rig.lease, reservation) == completed
    assert (rig.path / "stdin-count.txt").read_text() == "1"
    assert rig.budgets.inspect(app_env.context, rig.episode_id)["used"]["calls"] == 1


def test_other_workspace_artifact_cannot_be_bound_as_stdin(
    app_env: AppEnvironment,
    tmp_path: Path,
) -> None:
    rig = setup(app_env, tmp_path, tools=(definition(),))
    foreign = ArtifactStore(app_env.other_db, rig.artifacts.backend).put(
        app_env.other_context,
        b"another workspace's input",
    )
    with pytest.raises(ScopeDeniedError):
        propose(rig, foreign["id"])
    assert_not_dispatched(rig)
    with app_env.db.transaction(app_env.context) as connection:
        assert connection.execute("SELECT count(*) AS n FROM actions").fetchone()["n"] == 0


@pytest.mark.parametrize("stage", ["proposal", "dispatch"])
def test_tool_must_explicitly_accept_stdin_at_both_boundaries(
    app_env: AppEnvironment,
    tmp_path: Path,
    stage: str,
) -> None:
    original = definition(accepts_stdin=stage == "dispatch")
    rig = setup(app_env, tmp_path, tools=(original,))
    artifact = rig.artifacts.put(app_env.context, b"authorized only for stdin-aware tools")
    if stage == "proposal":
        with pytest.raises(ScopeDeniedError, match="does not accept"):
            propose(rig, artifact["id"])
    else:
        action_id = propose(rig, artifact["id"])
        replacement = ToolRegistry((replace(original, accepts_stdin=False),))
        gateway = ToolGateway(
            app_env.db,
            rig.artifacts,
            rig.candidates,
            replacement,
            CapabilityContext(replacement.inventory()),
            rig.receipts,
        )
        with pytest.raises(ScopeDeniedError, match="does not permit"):
            gateway.execute(app_env.context, action_id, rig.lease, reserve(rig, "consume_stdin"))
    assert_not_dispatched(rig)


def test_corrupt_artifact_hash_is_detected_before_dispatch_or_budget_settlement(
    app_env: AppEnvironment,
    tmp_path: Path,
) -> None:
    rig = setup(app_env, tmp_path, tools=(definition(),))
    payload = b"immutable input artifact\n"
    artifact = rig.artifacts.put(app_env.context, payload)
    action_id = propose(rig, artifact["id"])
    reservation = reserve(rig, "consume_stdin")
    path = rig.artifacts.backend.root / artifact["locator"]
    path.write_bytes(b"X" * len(payload))  # Equal length; only digest validation can detect this.
    with pytest.raises(ArtifactIntegrityError, match="digest"):
        rig.gateway.execute(app_env.context, action_id, rig.lease, reservation)
    assert_not_dispatched(rig)
    budget = rig.budgets.inspect(app_env.context, rig.episode_id)
    assert not budget["used"].get("calls", 0)
    assert budget["held"]["calls"] == 1  # Caller still owns the unstarted reservation.


def test_corrupt_stdin_receipt_cannot_publish_a_completion_during_recovery(
    app_env: AppEnvironment,
    tmp_path: Path,
) -> None:
    rig = setup(app_env, tmp_path, tools=(definition(),))
    artifact = rig.artifacts.put(app_env.context, b"receipt-bound input\n")
    action_id = propose(rig, artifact["id"])
    reservation = reserve(rig, "consume_stdin")

    def corrupt_receipt() -> None:
        paths = list((rig.receipts.root / str(app_env.context.workspace_id)).glob("*.json"))
        assert len(paths) == 1
        import json

        receipt = json.loads(paths[0].read_text())
        receipt["stdin_digest"] = "0" * 64
        paths[0].write_text(canonical_json(receipt))
        raise RuntimeError("Simulated lost acknowledgment with a corrupted private receipt")

    with pytest.raises(RuntimeError, match="lost acknowledgment"):
        rig.gateway.execute(
            app_env.context,
            action_id,
            rig.lease,
            reservation,
            after_receipt=corrupt_receipt,
        )
    run = rig.store.inspect(app_env.context, action_id)["run"]
    recovery = restarted(rig).reconcile(app_env.context, UUID(run["id"]))
    assert recovery["reconciliation"] == "unresolved"
    assert recovery["automatic_retry_permitted"] is False
    assert rig.store.inspect(app_env.context, action_id)["completion"] is None
    assert (rig.path / "stdin-count.txt").read_text() == "1"
    with app_env.db.transaction(app_env.context) as connection:
        assert connection.execute("SELECT count(*) AS n FROM action_runs").fetchone()["n"] == 1
        assert (
            connection.execute("SELECT count(*) AS n FROM action_completions").fetchone()["n"] == 0
        )
