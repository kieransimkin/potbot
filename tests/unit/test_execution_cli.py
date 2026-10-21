"""Operator exit codes and lease ownership at the dispatch boundary."""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from potbot.domain.errors import BudgetUnavailableError, ScopeDeniedError
from potbot.tools import execution_cli


@pytest.fixture
def cli_rig(monkeypatch, tmp_path):
    monkeypatch.setenv("POTBOT_DSN", "postgresql://fixture@localhost/potbot_test")
    task_id, candidate_id, action_id, run_id, reservation_id = (uuid4() for _ in range(5))
    lease = object()
    connection = Mock()
    connection.execute.return_value.fetchone.return_value = None

    @contextmanager
    def transaction(_context):
        yield connection

    database = SimpleNamespace(transaction=transaction)
    task = {
        "task_id": str(task_id),
        "current_contract": {"id": str(uuid4())},
        "lifecycle": "running",
    }
    action = {"id": str(action_id), "episode_id": str(task_id), "kind": "test.python"}
    result = {
        "action": action,
        "run": {"id": str(run_id), "candidate_id": str(candidate_id)},
        "completion": {"outcome": "completed", "exit_code": 0},
    }
    manager = SimpleNamespace(
        get=Mock(return_value={"state_id": uuid4()}),
        claim=Mock(return_value=lease),
        release=Mock(),
    )
    store = SimpleNamespace(
        create_inputs=Mock(return_value={"node_id": str(uuid4())}),
        propose=Mock(return_value=action),
        inspect=Mock(return_value=result),
    )
    budgets = SimpleNamespace(
        reserve=Mock(return_value={"id": str(reservation_id)}), release=Mock()
    )
    gateway = SimpleNamespace(
        execute=Mock(return_value=result), reconcile=Mock(return_value=result), store=store
    )
    for target, instance in (
        ("potbot.persistence.database.Database", database),
        ("potbot.persistence.artifacts.LocalArtifactBackend", object()),
        ("potbot.persistence.artifacts.ArtifactStore", object()),
        ("potbot.tools.repository.CandidateWorkspaceManager", manager),
        ("potbot.tools.receipts.RunReceiptStore", object()),
        ("potbot.tools.gateway.ToolGateway", gateway),
        ("potbot.controller.tasks.TaskService", SimpleNamespace(inspect=Mock(return_value=task))),
        ("potbot.controller.execution.ExecutionStore", store),
        ("potbot.controller.budgets.BudgetLedger", budgets),
    ):
        monkeypatch.setattr(target, Mock(return_value=instance))
    arguments = [
        "--dev-auth",
        "--workspace",
        str(uuid4()),
        "--principal",
        str(uuid4()),
        "--artifacts",
        str(tmp_path / "artifacts"),
        "--candidates",
        str(tmp_path / "candidates"),
        "--receipts",
        str(tmp_path / "receipts"),
        "run",
        str(task_id),
        "--candidate",
        str(candidate_id),
        "--tool",
        "test.python",
        "--key",
        "fixture-command",
    ]
    return SimpleNamespace(
        arguments=arguments,
        manager=manager,
        store=store,
        budgets=budgets,
        gateway=gateway,
        connection=connection,
        result=result,
        lease=lease,
        run_id=run_id,
    )


def test_reservation_denial_releases_lease_without_dispatch(cli_rig, capsys):
    cli_rig.budgets.reserve.side_effect = BudgetUnavailableError("Task allowance is unavailable.")
    assert execution_cli.main(cli_rig.arguments) == 1
    cli_rig.manager.release.assert_called_once()
    assert cli_rig.manager.release.call_args.args[1] is cli_rig.lease
    cli_rig.gateway.execute.assert_not_called()
    cli_rig.budgets.release.assert_not_called()
    assert "error" in json.loads(capsys.readouterr().out)


def test_pre_dispatch_denial_releases_reservation_and_lease(cli_rig, capsys):
    cli_rig.gateway.execute.side_effect = ScopeDeniedError("The task does not permit this tool.")
    cli_rig.store.inspect.return_value = {"run": None}
    assert execution_cli.main(cli_rig.arguments) == 1
    cli_rig.budgets.release.assert_called_once()
    assert cli_rig.budgets.release.call_args.kwargs["confirmed_not_started"] is True
    cli_rig.manager.release.assert_called_once()
    assert "error" in json.loads(capsys.readouterr().out)


@pytest.mark.parametrize(
    ("outcome", "tool_code", "expected"),
    [
        ("completed", 0, 0),
        ("nonzero_exit", 7, 7),
        ("nonzero_exit", -9, 137),
        ("timeout", -9, 124),
        ("cancelled", -15, 130),
        ("inconclusive", None, 1),
    ],
)
def test_tool_outcome_sets_operator_exit_status(cli_rig, capsys, outcome, tool_code, expected):
    cli_rig.result["completion"] = {"outcome": outcome, "exit_code": tool_code}
    assert execution_cli.main(cli_rig.arguments) == expected
    assert json.loads(capsys.readouterr().out)["data"]["completion"]["outcome"] == outcome
    cli_rig.manager.release.assert_called_once()


def test_completed_replay_preserves_failure_status_and_only_reconciles_its_run(cli_rig, capsys):
    cli_rig.connection.execute.return_value.fetchone.return_value = {
        "result": cli_rig.result["action"]
    }
    cli_rig.result["completion"] = {"outcome": "nonzero_exit", "exit_code": 5}
    assert execution_cli.main(cli_rig.arguments) == 5
    cli_rig.manager.claim.assert_not_called()
    cli_rig.budgets.reserve.assert_not_called()
    cli_rig.gateway.execute.assert_not_called()
    cli_rig.gateway.reconcile.assert_called_once()
    assert cli_rig.gateway.reconcile.call_args.args[1] == cli_rig.run_id
    assert json.loads(capsys.readouterr().out)["data"]["completion"]["exit_code"] == 5


def test_read_only_inspection_is_successful_even_when_the_inspected_tool_failed(cli_rig, capsys):
    prefix = cli_rig.arguments[: cli_rig.arguments.index("run")]
    arguments = [*prefix, "inspect", cli_rig.result["action"]["id"]]
    cli_rig.result["completion"] = {"outcome": "nonzero_exit", "exit_code": 4}
    assert execution_cli.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["data"]["completion"]["exit_code"] == 4
