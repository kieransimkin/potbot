"""Real PostgreSQL and isolated process acceptance for frozen verification evidence."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from potbot.controller.tasks import TaskService
from potbot.controller.verification import VerificationService
from potbot.domain.errors import (
    CapabilityUnavailableError,
    IndeterminateExecutionError,
    InvalidInputError,
    StateMismatchError,
)
from potbot.domain.execution import CapabilityContext, ToolDefinition, ToolRegistry
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement, TaskStatus
from potbot.domain.verification import CheckDefinition, VerificationProfile
from potbot.evaluation.repository_fixture import (
    FixtureVariant,
    create_repository_fixture,
    repair_repository_fixture,
)
from potbot.models.configuration import EnvironmentManifest, store_environment
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.tools.gateway import ToolGateway
from potbot.tools.receipts import RunReceiptStore
from potbot.tools.repository import CandidateWorkspaceManager, RepositoryStateStore

from .conftest import AppEnvironment

pytestmark = [pytest.mark.postgres, pytest.mark.sandbox]
PYTHON = "/usr/bin/python3"


def specification() -> ContractSpec:
    return ContractSpec(
        "Verify a calculator repair without hiding an unrelated failure",
        "fixture-baseline",
        DeliverableType.PATCH,
        (Requirement("R1", "Calculator works", "The calculator's existing tests pass"),),
        ("calculator.py", "tests"),
        ("test",),
        "fixture-v1",
        (("tokens", 0), ("calls", 30), ("tool_ms", 300000), ("concurrency", 4)),
    )


def definitions() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition("compile", "test", (PYTHON, "-m", "compileall", "-q", "."), timeout_ms=5000),
        ToolDefinition(
            "clean",
            "test",
            (
                PYTHON,
                "-B",
                "-c",
                "from pathlib import Path; import sys; sys.exit(int(any(Path('.').rglob('*.pyc'))))",
            ),
            mutates=False,
            timeout_ms=5000,
        ),
        ToolDefinition(
            "calculator",
            "test",
            (PYTHON, "-B", "-m", "unittest", "tests.test_calculator"),
            timeout_ms=5000,
        ),
        ToolDefinition(
            "unrelated",
            "test",
            (PYTHON, "-B", "-m", "unittest", "tests.test_preexisting"),
            timeout_ms=5000,
        ),
        ToolDefinition(
            "dependency",
            "test",
            (PYTHON, "-B", "-c", "import sys; sys.exit(78)"),
            mutates=False,
            timeout_ms=5000,
        ),
    )


def profile() -> VerificationProfile:
    return VerificationProfile(
        "calculator-v1",
        1,
        (
            CheckDefinition("compile", "syntax-valid", "compile"),
            CheckDefinition("clean", "isolated-input", "clean"),
            CheckDefinition(
                "calculator",
                "calculator-total",
                "calculator",
                ("R1",),
                ("tests/test_calculator.py", "tests/__init__.py"),
            ),
            CheckDefinition(
                "unrelated",
                "known-unrelated-failure",
                "unrelated",
                (),
                ("tests/test_preexisting.py",),
                mandatory=False,
            ),
        ),
    )


def setup(app_env: AppEnvironment, tmp_path: Path) -> SimpleNamespace:
    fixture = create_repository_fixture(
        tmp_path / "source",
        variant=FixtureVariant.PRE_EXISTING_FAILURE,
    )
    tasks = TaskService(app_env.db)
    task = tasks.submit(
        app_env.context, app_env.repository_id, specification(), "verification-task"
    )
    episode, contract = UUID(task["task_id"]), UUID(task["current_contract"]["id"])
    with app_env.db.transaction(app_env.context) as connection:
        environment = store_environment(
            connection,
            app_env.context,
            EnvironmentManifest(
                UUID(task["configuration"]["id"]),
                task["configuration"]["content_hash"],
                "linux",
                "Python verification fixture",
                (("fixture-tools", "v1"),),
            ),
        )
    artifacts = ArtifactStore(app_env.db, LocalArtifactBackend(app_env.artifact_root))
    states = RepositoryStateStore(app_env.db, artifacts)
    base = states.capture(app_env.context, app_env.repository_id, fixture.path, environment["id"])
    candidates = CandidateWorkspaceManager(app_env.db, artifacts, tmp_path / "candidates")
    initialized = tasks.transition(
        app_env.context,
        episode,
        task["update_token"],
        TaskStatus.INITIALIZING,
        "Frozen faulty baseline",
        idempotency_key="initialize",
    )
    tasks.transition(
        app_env.context,
        episode,
        initialized["update_token"],
        TaskStatus.RUNNING,
        "Verification ready",
        idempotency_key="run",
    )
    registry = ToolRegistry(definitions())
    receipts = RunReceiptStore(tmp_path / "receipts")
    gateway = ToolGateway(
        app_env.db,
        artifacts,
        candidates,
        registry,
        CapabilityContext(registry.inventory()),
        receipts,
    )
    return SimpleNamespace(
        env=app_env,
        fixture=fixture,
        tasks=tasks,
        task=task,
        episode=episode,
        contract=contract,
        environment=environment,
        artifacts=artifacts,
        states=states,
        base=base,
        candidates=candidates,
        registry=registry,
        receipts=receipts,
        gateway=gateway,
        service=VerificationService(app_env.db, artifacts, candidates, gateway),
    )


def start_baseline(rig: SimpleNamespace, selected: VerificationProfile | None = None) -> dict:
    frozen = rig.service.create_profile(
        rig.env.context,
        rig.episode,
        rig.contract,
        rig.base["id"],
        selected or profile(),
        "profile",
    )
    return rig.service.start(
        rig.env.context, UUID(frozen["id"]), rig.base["id"], "baseline", "baseline"
    )


def statuses(result: dict) -> dict[str, str]:
    return {row["definition"]["check_id"]: row["observation"]["status"] for row in result["checks"]}


def restarted(rig: SimpleNamespace, definitions_override=None) -> VerificationService:
    candidates = CandidateWorkspaceManager(rig.env.db, rig.artifacts, rig.candidates.root)
    registry = ToolRegistry(definitions() if definitions_override is None else definitions_override)
    gateway = ToolGateway(
        rig.env.db,
        rig.artifacts,
        candidates,
        registry,
        CapabilityContext(registry.inventory()),
        RunReceiptStore(rig.receipts.root),
    )
    return VerificationService(rig.env.db, rig.artifacts, candidates, gateway)


def test_faulty_baseline_repair_optional_failure_fresh_clones_and_restart_replay(
    app_env: AppEnvironment,
    tmp_path: Path,
) -> None:
    rig = setup(app_env, tmp_path)
    baseline = start_baseline(rig)
    run_id = UUID(baseline["id"])
    pending = rig.service.inspect(app_env.context, run_id)
    assert set(statuses(pending).values()) == {"not_run"}
    assert all(row["observation"]["persisted"] is False for row in pending["checks"])
    with app_env.db.transaction(app_env.context) as connection:
        assert connection.execute("SELECT count(*) AS n FROM action_runs").fetchone()["n"] == 0
    observed = rig.service.run_all(app_env.context, run_id)
    assert statuses(observed) == {
        "compile": "passed",
        "clean": "passed",
        "calculator": "failed",
        "unrelated": "failed",
    }
    assert observed["checks"][-1]["definition"]["mandatory"] is False
    with app_env.db.transaction(app_env.context) as connection:
        attempts = connection.execute(
            "SELECT v.*,r.input_state_id FROM verification_attempts v "
            "JOIN action_runs r ON r.workspace_id=v.workspace_id AND r.action_id=v.action_id "
            "WHERE verification_run_id=%s",
            (run_id,),
        ).fetchall()
    assert len({row["candidate_id"] for row in attempts}) == 4
    assert all(row["input_state_id"] == rig.base["id"] for row in attempts)
    paths = {
        row["check_id"]: Path(rig.candidates.get(app_env.context, row["candidate_id"])["path"])
        for row in attempts
    }
    assert list(paths["compile"].rglob("*.pyc"))
    assert list(paths["clean"].rglob("*.pyc")) == []
    assert list(rig.fixture.path.rglob("*.pyc")) == []
    # The only implementation change is the calculator repair; both frozen
    # existing test definitions remain byte-for-byte identical to the baseline.
    repair_repository_fixture(rig.fixture)
    target = rig.states.capture(
        app_env.context,
        app_env.repository_id,
        rig.fixture.path,
        rig.environment["id"],
        parent_ids=(rig.base["id"],),
    )
    candidate = rig.service.start(
        app_env.context,
        UUID(baseline["profile_id"]),
        target["id"],
        "candidate",
        "candidate",
    )
    improved = rig.service.run_all(app_env.context, UUID(candidate["id"]))
    assert statuses(improved) == {
        "compile": "passed",
        "clean": "passed",
        "calculator": "passed",
        "unrelated": "failed",
    }
    fresh = restarted(rig)
    assert fresh.run_all(app_env.context, run_id) == observed
    assert fresh.run_all(app_env.context, UUID(candidate["id"])) == improved
    with app_env.db.transaction(app_env.context) as connection:
        assert connection.execute("SELECT count(*) AS n FROM action_runs").fetchone()["n"] == 8
        assert (
            connection.execute("SELECT count(*) AS n FROM verification_attempts").fetchone()["n"]
            == 8
        )
        assert (
            connection.execute("SELECT count(*) AS n FROM action_completions").fetchone()["n"] == 8
        )
    budget = rig.gateway.budgets.inspect(app_env.context, rig.episode)
    assert budget["used"]["calls"] == 8 and not budget["held"].get("calls", 0)


def test_known_dependency_failure_is_infrastructure_not_a_test_failure(
    app_env: AppEnvironment,
    tmp_path: Path,
) -> None:
    rig = setup(app_env, tmp_path)
    run = start_baseline(
        rig,
        VerificationProfile(
            "dependencies",
            1,
            (CheckDefinition("dependency", "runtime-ready", "dependency", ("R1",)),),
        ),
    )
    result = rig.service.run_check(app_env.context, UUID(run["id"]), "dependency")
    assert result["status"] == "infrastructure_error"
    assert result["details"]["completion"]["exit_code"] == 78
    assert result["execution_run_id"] is not None


@pytest.mark.parametrize("change", ["modified", "removed", "tool", "environment"])
def test_changed_definition_or_environment_cannot_reuse_a_baseline_pass(
    app_env: AppEnvironment,
    tmp_path: Path,
    change: str,
) -> None:
    rig = setup(app_env, tmp_path)
    run = start_baseline(rig)
    environment_id = rig.environment["id"]
    if change == "modified":
        definition = rig.fixture.path / "tests" / "test_calculator.py"
        definition.write_text(definition.read_text() + "\n# Changed test definition.\n")
    elif change == "removed":
        (rig.fixture.path / "tests" / "test_calculator.py").unlink()
    elif change == "tool":
        tools = tuple(
            replace(item, argv_prefix=(PYTHON, "-c", "raise SystemExit(0)"))
            if item.name == "calculator"
            else item
            for item in definitions()
        )
        rig.service = restarted(rig, tools)
    else:
        with app_env.db.transaction(app_env.context) as connection:
            environment_id = store_environment(
                connection,
                app_env.context,
                EnvironmentManifest(
                    UUID(rig.task["configuration"]["id"]),
                    rig.task["configuration"]["content_hash"],
                    "linux",
                    "Different Python environment",
                    (("fixture-tools", "v2"),),
                ),
            )["id"]
    target = rig.states.capture(
        app_env.context,
        app_env.repository_id,
        rig.fixture.path,
        environment_id,
        parent_ids=(rig.base["id"],),
    )
    changed = rig.service.start(
        app_env.context,
        UUID(run["profile_id"]),
        target["id"],
        "candidate",
        "changed-target",
    )
    result = rig.service.run_check(app_env.context, UUID(changed["id"]), "calculator")
    assert result["status"] == ("blocked" if change == "removed" else "inconclusive")
    assert result["execution_run_id"] is None
    with app_env.db.transaction(app_env.context) as connection:
        assert connection.execute("SELECT count(*) AS n FROM action_runs").fetchone()["n"] == 0
        assert (
            connection.execute("SELECT count(*) AS n FROM budget_reservations").fetchone()["n"] == 0
        )


def test_unknown_criterion_and_superseded_contract_cannot_dispatch_checks(
    app_env: AppEnvironment,
    tmp_path: Path,
) -> None:
    rig = setup(app_env, tmp_path)
    unsupported = VerificationProfile(
        "unsupported",
        1,
        (CheckDefinition("calculator", "calculator-total", "calculator", ("R404",)),),
    )
    with pytest.raises(InvalidInputError, match="unknown acceptance criterion"):
        rig.service.create_profile(
            app_env.context,
            rig.episode,
            rig.contract,
            rig.base["id"],
            unsupported,
            "unsupported",
        )
    run = start_baseline(rig)
    rig.tasks.revise(
        app_env.context,
        rig.episode,
        rig.contract,
        replace(specification(), objective="Updated calculator acceptance"),
        "revised",
    )
    with pytest.raises(StateMismatchError, match="stale or inactive"):
        rig.service.run_check(app_env.context, UUID(run["id"]), "calculator")
    with pytest.raises(StateMismatchError, match="current profile"):
        rig.service.start(
            app_env.context,
            UUID(run["profile_id"]),
            rig.base["id"],
            "baseline",
            "stale-start",
        )
    with pytest.raises(StateMismatchError, match="current contract"):
        rig.service.create_profile(
            app_env.context,
            rig.episode,
            rig.contract,
            rig.base["id"],
            profile(),
            "stale-profile",
        )
    with app_env.db.transaction(app_env.context) as connection:
        assert connection.execute("SELECT count(*) AS n FROM action_runs").fetchone()["n"] == 0


@pytest.mark.parametrize("failure", [CapabilityUnavailableError, OSError])
def test_definite_allocation_failure_releases_unstarted_budget_and_records_failure(
    app_env: AppEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[Exception],
) -> None:
    rig = setup(app_env, tmp_path)
    run = start_baseline(rig)

    def unavailable(*args, **kwargs):
        raise failure("test allocation failure containing no user information")

    monkeypatch.setattr(rig.candidates, "allocate", unavailable)
    result = rig.service.run_check(app_env.context, UUID(run["id"]), "calculator")
    assert result["status"] == "infrastructure_error"
    assert result["execution_run_id"] is None
    budget = rig.gateway.budgets.inspect(app_env.context, rig.episode)
    assert not budget["held"].get("calls", 0) and not budget["used"].get("calls", 0)
    with app_env.db.transaction(app_env.context) as connection:
        assert connection.execute("SELECT count(*) AS n FROM action_runs").fetchone()["n"] == 0
        assert (
            connection.execute("SELECT count(*) AS n FROM verification_attempts").fetchone()["n"]
            == 0
        )
    assert rig.service.run_check(app_env.context, UUID(run["id"]), "calculator") == result


def test_unknown_allocation_acknowledgment_keeps_capacity_until_durable_retry(
    app_env: AppEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = setup(app_env, tmp_path)
    run = start_baseline(rig)
    original = rig.candidates.allocate

    def uncertain(*args, **kwargs):
        raise IndeterminateExecutionError("The preparation transaction needs reconciliation.")

    monkeypatch.setattr(rig.candidates, "allocate", uncertain)
    result = rig.service.run_check(app_env.context, UUID(run["id"]), "calculator")
    assert result["status"] == "inconclusive" and "id" not in result
    assert rig.gateway.budgets.inspect(app_env.context, rig.episode)["held"]["calls"] == 1
    with app_env.db.transaction(app_env.context) as connection:
        assert (
            connection.execute("SELECT count(*) AS n FROM verification_observations").fetchone()[
                "n"
            ]
            == 0
        )
    monkeypatch.setattr(rig.candidates, "allocate", original)
    completed = rig.service.run_check(app_env.context, UUID(run["id"]), "calculator")
    assert completed["status"] == "failed"
    budget = rig.gateway.budgets.inspect(app_env.context, rig.episode)
    assert budget["used"]["calls"] == 1 and not budget["held"].get("calls", 0)
    with app_env.db.transaction(app_env.context) as connection:
        assert (
            connection.execute("SELECT count(*) AS n FROM budget_reservations").fetchone()["n"] == 1
        )
        assert connection.execute("SELECT count(*) AS n FROM action_runs").fetchone()["n"] == 1


def test_removed_installed_tool_is_recorded_as_infrastructure_failure(
    app_env: AppEnvironment,
    tmp_path: Path,
) -> None:
    rig = setup(app_env, tmp_path)
    run = start_baseline(rig)
    service = restarted(rig, tuple(item for item in definitions() if item.name != "calculator"))
    result = service.run_check(app_env.context, UUID(run["id"]), "calculator")
    assert result["status"] == "infrastructure_error"
    assert result["execution_run_id"] is None
    assert not rig.gateway.budgets.inspect(app_env.context, rig.episode)["held"].get("calls", 0)
