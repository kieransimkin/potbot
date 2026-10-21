"""The operator CLI reaches real PostgreSQL, Git, sandboxed checks and exports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from potbot.domain.errors import IndeterminateExecutionError
from potbot.persistence import database as database_module
from potbot.tools import patch_cli

from .conftest import AppEnvironment

pytestmark = [pytest.mark.postgres, pytest.mark.sandbox]


def arguments(environment: AppEnvironment, root: Path) -> list[str]:
    return [
        "--dev-auth",
        "--workspace",
        str(environment.context.workspace_id),
        "--principal",
        str(environment.context.principal_id),
        "--artifacts",
        str(environment.artifact_root),
        "--candidates",
        str(root / "candidates"),
        "--receipts",
        str(root / "receipts"),
    ]


def call(argv: list[str], capsys: pytest.CaptureFixture[str], expected: int = 0) -> dict[str, Any]:
    result = patch_cli.main(argv)
    captured = capsys.readouterr()
    parsed: dict[str, Any] = json.loads(captured.out)
    assert result == expected, parsed
    return parsed


def test_demo_replay_binds_options_then_resume_exports_a_verified_bundle(
    app_env: AppEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("POTBOT_DSN", app_env.db.dsn)
    common = arguments(app_env, tmp_path)
    fixture_parent = tmp_path / "fixtures with spaces"
    demo = [
        *common,
        "demo",
        "--fixtures",
        str(fixture_parent),
        "--key",
        "operator-demo",
        "--stop-after",
        "baseline",
    ]
    first = call(demo, capsys)["data"]
    second = call(demo, capsys)["data"]
    assert first["pipeline_id"] == second["pipeline_id"]
    assert first["baseline"]["id"] == second["baseline"]["id"]
    assert len(list(fixture_parent.glob("fixture-*"))) == 1
    with app_env.db.transaction(app_env.context) as connection:
        assert connection.execute("SELECT count(*) AS n FROM action_runs").fetchone()["n"] == 3
    rejected = call([*demo, "--preexisting-failure"], capsys, expected=1)
    assert rejected["error"]["code"] == "revision_conflict"
    other_parent = tmp_path / "different fixtures"
    changed = [
        *common,
        "demo",
        "--fixtures",
        str(other_parent),
        "--key",
        "operator-demo",
        "--stop-after",
        "baseline",
    ]
    assert call(changed, capsys, expected=1)["error"]["code"] == "revision_conflict"
    assert not other_parent.exists()

    output = tmp_path / "review bundle"
    resumed = call([*common, "resume", first["pipeline_id"], "--output", str(output)], capsys)[
        "data"
    ]
    assert resumed["outcome"] == "verified"
    assert resumed["task"]["lifecycle"] == "verified"
    assert resumed["bundle"]
    assert list(output.iterdir())
    inspected = call([*common, "inspect", first["pipeline_id"]], capsys)["data"]
    assert inspected["id"] == first["pipeline_id"]
    assert len(inspected["exports"]) == 1
    with app_env.db.transaction(app_env.context) as connection:
        before = connection.execute("SELECT count(*) AS n FROM action_runs").fetchone()["n"]
    replayed = call(demo, capsys)["data"]
    assert replayed["outcome"] == "verified"
    with app_env.db.transaction(app_env.context) as connection:
        assert connection.execute("SELECT count(*) AS n FROM action_runs").fetchone()["n"] == before


def test_demo_recovers_inner_commit_only_for_the_original_source_and_profile(
    app_env: AppEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("POTBOT_DSN", app_env.db.dsn)
    common = arguments(app_env, tmp_path)
    fixture_parent = tmp_path / "fixtures"
    demo = [
        *common,
        "demo",
        "--fixtures",
        str(fixture_parent),
        "--key",
        "interrupted-demo",
        "--stop-after",
        "baseline",
    ]
    original = database_module.idempotent_command

    def interrupted(connection, context, operation, key, request, callback):
        if operation == "create_patch_demo":
            callback()
            raise IndeterminateExecutionError("Outer demo acknowledgment was interrupted.")
        return original(connection, context, operation, key, request, callback)

    monkeypatch.setattr(database_module, "idempotent_command", interrupted)
    assert call(demo, capsys, expected=1)["error"]["code"] == "indeterminate_execution"
    monkeypatch.setattr(database_module, "idempotent_command", original)
    assert len(list(fixture_parent.glob("fixture-*"))) == 1
    with app_env.db.transaction(app_env.context) as connection:
        assert connection.execute("SELECT count(*) AS n FROM patch_pipelines").fetchone()["n"] == 1
        assert connection.execute("SELECT count(*) AS n FROM action_runs").fetchone()["n"] == 0
    assert (
        call([*demo, "--preexisting-failure"], capsys, expected=1)["error"]["code"]
        == "revision_conflict"
    )
    changed = [
        *common,
        "demo",
        "--fixtures",
        str(tmp_path / "other"),
        "--key",
        "interrupted-demo",
        "--stop-after",
        "baseline",
    ]
    assert call(changed, capsys, expected=1)["error"]["code"] == "revision_conflict"
    recovered = call(demo, capsys)["data"]
    assert recovered["checkpoint"] == "baseline"
    assert len(list(fixture_parent.glob("fixture-*"))) == 1
    with app_env.db.transaction(app_env.context) as connection:
        assert connection.execute("SELECT count(*) AS n FROM action_runs").fetchone()["n"] == 3
