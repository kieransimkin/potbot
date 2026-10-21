"""Task durability, contract CAS, lifecycle control and public scope boundaries."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Thread
from typing import Any
from uuid import UUID, uuid4
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest

from potbot.api.tasks import DevelopmentBearerAuth, TaskAPI, TaskWSGI
from potbot.controller.budgets import BudgetLedger
from potbot.controller.tasks import TaskService
from potbot.domain.errors import (
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement, TaskStatus

pytestmark = pytest.mark.postgres


def specification() -> ContractSpec:
    return ContractSpec(
        objective="Repair cleanup",
        baseline_ref="git:" + "1" * 40,
        deliverable_type=DeliverableType.PATCH,
        requirements=(Requirement("R1", "Connection closes", "Cancellation closes exactly once"),),
        change_scope=("src/client.py",),
        capabilities=("test",),
        verification_policy="tests-v1",
        budget=(("tokens", 1000), ("calls", 10), ("tool_ms", 10000), ("concurrency", 2)),
        compatibility_constraints=("Retain public API",),
        nonfunctional_requirements=("No extra requests",),
    )


def envelope(payload: dict, key: str | None = None) -> dict:
    return {
        "schema_version": 1,
        "request_id": str(uuid4()),
        "idempotency_key": key,
        "payload": payload,
    }


def counts(env: Any) -> dict[str, int]:
    with env.db.transaction(env.context) as conn:
        return {
            table: conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
            for table in (
                "episodes",
                "contract_revisions",
                "evidence_events",
                "budget_limits",
                "resolved_configurations",
                "command_results",
            )
        }


def test_concurrent_submission_commits_exactly_one_task_contract_event_and_config(
    app_env: Any,
) -> None:
    barrier = Barrier(2)

    def submit() -> dict:
        barrier.wait(timeout=10)
        return TaskService(app_env.db).submit(
            app_env.context, app_env.repository_id, specification(), "same-request"
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: submit(), range(2)))
    assert results[0] == results[1]
    assert all(value == 1 for value in counts(app_env).values())
    result = TaskService(app_env.db).inspect(app_env.context, UUID(results[0]["task_id"]))
    assert result["current_contract"]["content"] == specification().to_dict()
    assert result["lifecycle"] == "submitted"
    with pytest.raises(RevisionConflictError):
        TaskService(app_env.db).submit(
            app_env.context,
            app_env.repository_id,
            replace(specification(), objective="A different request"),
            "same-request",
        )


def test_creation_event_failure_rolls_back_all_task_state_and_allows_retry(
    app_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import potbot.persistence.evidence as evidence

    original = evidence.append_event

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("injected between state writes and event append")

    monkeypatch.setattr(evidence, "append_event", fail)
    with pytest.raises(RuntimeError, match="injected"):
        TaskService(app_env.db).submit(
            app_env.context, app_env.repository_id, specification(), "rollback"
        )
    assert all(value == 0 for value in counts(app_env).values())
    monkeypatch.setattr(evidence, "append_event", original)
    TaskService(app_env.db).submit(
        app_env.context, app_env.repository_id, specification(), "rollback"
    )
    assert all(value == 1 for value in counts(app_env).values())


def test_task_config_is_frozen_and_later_defaults_do_not_change_idempotent_replay(
    app_env: Any,
) -> None:
    layers = {
        "workspace": {
            "max_packet_tokens": 1024,
            "secret_refs": {"provider": "secret://environment/provider"},
        }
    }
    first = TaskService(app_env.db, configuration_layers=layers).submit(
        app_env.context, app_env.repository_id, specification(), "frozen-config"
    )
    layers["workspace"]["max_packet_tokens"] = 512
    changed = TaskService(app_env.db, configuration_layers=layers)
    assert (
        changed.submit(app_env.context, app_env.repository_id, specification(), "frozen-config")
        == first
    )
    assert (
        changed.inspect(app_env.context, UUID(first["task_id"]))["configuration"]["content"][
            "max_packet_tokens"
        ]
        == 1024
    )
    second = changed.submit(app_env.context, app_env.repository_id, specification(), "next-task")
    assert second["configuration"]["content"]["max_packet_tokens"] == 512
    assert second["configuration"]["content"]["secret_refs"] == {
        "provider": "secret://environment/provider"
    }


def test_contract_revision_is_immutable_scoped_and_cas_preserves_history(app_env: Any) -> None:
    service = TaskService(app_env.db)
    first = service.submit(app_env.context, app_env.repository_id, specification(), "first")
    episode_id = UUID(first["task_id"])
    old_id = UUID(first["current_contract"]["id"])
    revised = replace(
        specification(),
        requirements=(
            *specification().requirements,
            Requirement("R2", "Normal requests work", "The success path is unchanged"),
        ),
    )
    second = service.revise(app_env.context, episode_id, old_id, revised, "revision-one")
    assert second["current_contract"]["predecessor_id"] == str(old_id)
    assert second["current_contract"]["content"] == revised.to_dict()
    with pytest.raises(RevisionConflictError):
        service.revise(app_env.context, episode_id, old_id, specification(), "stale")
    with pytest.raises(RevisionConflictError):
        service.revise(app_env.context, episode_id, uuid4(), specification(), "foreign-predecessor")
    history = TaskService(app_env.db).history(app_env.context, episode_id)
    assert len(history) == 2
    assert history[0]["content"] == specification().to_dict()
    with pytest.raises((ScopeDeniedError, RevisionConflictError)):
        with app_env.db.transaction(app_env.context) as conn:
            conn.execute("UPDATE contract_revisions SET content='{}' WHERE id=%s", (old_id,))
    with app_env.db.transaction(app_env.context) as conn:
        events = conn.execute(
            "SELECT details FROM evidence_events WHERE event_type='contract_revised'"
        ).fetchall()
    assert events == [
        {
            "details": {
                "predecessor_id": str(old_id),
                "contract_revision_id": second["current_contract"]["id"],
                "contract_hash": second["current_contract"]["content_hash"],
            }
        }
    ]


def test_cross_workspace_repository_and_task_access_is_indistinguishable_from_missing(
    app_env: Any,
) -> None:
    with pytest.raises(ScopeDeniedError):
        TaskService(app_env.db).submit(
            app_env.context, app_env.other_repository_id, specification(), "cross-scope"
        )
    assert all(value == 0 for value in counts(app_env).values())
    foreign = TaskService(app_env.other_db).submit(
        app_env.other_context, app_env.other_repository_id, specification(), "foreign"
    )
    token = "dev-test-token-" + "q" * 32
    api = TaskAPI(TaskService(app_env.db), DevelopmentBearerAuth({token: app_env.context}))
    responses = [
        api.handle("GET", f"/v1/tasks/{identifier}", envelope({}), authorization=f"Bearer {token}")
        for identifier in (foreign["task_id"], str(uuid4()))
    ]
    assert responses[0].status == responses[1].status == 404
    assert responses[0].body["error"] == responses[1].body["error"]
    for path in (
        f"/v1/tasks/{foreign['task_id']}/contracts",
        f"/v1/tasks/{foreign['task_id']}/budget",
    ):
        assert api.handle("GET", path, envelope({}), authorization=f"Bearer {token}").status == 404


def test_pause_restart_resume_and_blocked_dependency_stop_all_new_admissions(app_env: Any) -> None:
    service = TaskService(app_env.db)
    created = service.submit(app_env.context, app_env.repository_id, specification(), "task")
    episode_id = UUID(created["task_id"])
    initializing = service.transition(
        app_env.context,
        episode_id,
        0,
        TaskStatus.INITIALIZING,
        "Capture baseline",
        idempotency_key="init",
    )
    running = service.transition(
        app_env.context,
        episode_id,
        initializing["update_token"],
        TaskStatus.RUNNING,
        "Ready",
        idempotency_key="run",
    )
    paused = service.control(
        app_env.context,
        episode_id,
        running["update_token"],
        "pause",
        "User paused",
        idempotency_key="pause",
    )
    restarted = TaskService(app_env.db)
    assert restarted.inspect(app_env.context, episode_id)["lifecycle"] == "paused"
    with pytest.raises(StateMismatchError):
        BudgetLedger(app_env.db).reserve(app_env.context, episode_id, {"calls": 1}, "paused-work")
    resumed = restarted.control(
        app_env.context,
        episode_id,
        paused["update_token"],
        "resume",
        "Continue",
        idempotency_key="resume",
    )
    assert resumed["lifecycle"] == "running"
    assert resumed["current_contract"]["id"] == created["current_contract"]["id"]
    BudgetLedger(app_env.db).reserve(app_env.context, episode_id, {"calls": 1}, "admitted")
    blocked = restarted.transition(
        app_env.context,
        episode_id,
        resumed["update_token"],
        TaskStatus.BLOCKED,
        "Need a local model",
        blocked_on="model-cache",
        idempotency_key="block",
    )
    assert blocked["blockers"] == ["model-cache"]
    with pytest.raises(StateMismatchError):
        BudgetLedger(app_env.db).reserve(app_env.context, episode_id, {"calls": 1}, "blocked-work")
    with pytest.raises(RevisionConflictError):
        restarted.control(
            app_env.context,
            episode_id,
            resumed["update_token"],
            "cancel",
            "Stale",
            idempotency_key="stale-cancel",
        )
    cancelled = restarted.control(
        app_env.context,
        episode_id,
        blocked["update_token"],
        "cancel",
        "Stop",
        idempotency_key="cancel",
    )
    with pytest.raises(StateMismatchError):
        restarted.control(
            app_env.context,
            episode_id,
            cancelled["update_token"],
            "resume",
            "Cannot restart terminal",
            idempotency_key="after-terminal",
        )


def test_v1_api_rejects_unknown_versions_untrusted_scope_missing_budget_and_unsafe_control(
    app_env: Any,
) -> None:
    token = "dev-test-token-" + "r" * 32
    api = TaskAPI(TaskService(app_env.db), DevelopmentBearerAuth({token: app_env.context}))
    valid = envelope(
        {"repository_id": str(app_env.repository_id), "contract": specification().to_dict()}, "task"
    )
    unknown = valid | {"schema_version": 2}
    assert (
        api.handle("POST", "/v1/tasks", unknown, authorization=f"Bearer {token}").body["error"][
            "code"
        ]
        == "schema_violation"
    )
    injected = envelope(
        valid["payload"] | {"workspace_id": str(app_env.other_context.workspace_id)}, "task"
    )
    assert api.handle("POST", "/v1/tasks", injected, authorization=f"Bearer {token}").status == 400
    missing = specification().to_dict()
    del missing["budget"]
    assert (
        api.handle(
            "POST",
            "/v1/tasks",
            envelope({"repository_id": str(app_env.repository_id), "contract": missing}, "task"),
            authorization=f"Bearer {token}",
        ).status
        == 400
    )
    assert counts(app_env)["episodes"] == 0
    submitted = api.handle("POST", "/v1/tasks", valid, authorization=f"Bearer {token}")
    assert submitted.status == 202
    assert api.handle("POST", "/v1/tasks", valid, authorization=f"Bearer {token}") == submitted
    task = submitted.body["data"]
    unsafe = api.handle(
        "POST",
        f"/v1/tasks/{task['task_id']}/control",
        envelope({"expected_token": 0, "action": "verified", "reason": "I said so"}, "unsafe"),
        authorization=f"Bearer {token}",
    )
    assert unsafe.status == 400


def test_cli_submit_and_inspect_survive_real_client_process_restarts(
    app_env: Any, tmp_path: Path
) -> None:
    source = tmp_path / "contract.json"
    source.write_text(json.dumps(specification().to_dict()), encoding="utf-8")
    arguments = [
        sys.executable,
        "-m",
        "potbot.api.task_cli",
        "--dev-auth",
        "--workspace",
        str(app_env.context.workspace_id),
        "--principal",
        str(app_env.context.principal_id),
    ]
    environment = os.environ.copy() | {"POTBOT_DSN": app_env.db.dsn}
    first = subprocess.run(
        [
            *arguments,
            "submit",
            "--repository",
            str(app_env.repository_id),
            "--contract",
            str(source),
            "--key",
            "cli-submission",
        ],
        capture_output=True,
        text=True,
        env=environment,
        timeout=15,
        check=True,
    )
    submitted = json.loads(first.stdout)["data"]
    paused = TaskService(app_env.db).control(
        app_env.context,
        UUID(submitted["task_id"]),
        0,
        "pause",
        "Wait for review",
        idempotency_key="cli-pause",
    )
    second = subprocess.run(
        [*arguments, "inspect", submitted["task_id"]],
        capture_output=True,
        text=True,
        env=environment,
        timeout=15,
        check=True,
    )
    inspected = json.loads(second.stdout)["data"]
    assert inspected["lifecycle"] == "paused"
    assert inspected["current_contract"] == submitted["current_contract"]
    third = subprocess.run(
        [
            *arguments,
            "control",
            submitted["task_id"],
            "resume",
            "--expected-token",
            str(paused["update_token"]),
            "--reason",
            "Reviewed",
            "--key",
            "cli-resume",
        ],
        capture_output=True,
        text=True,
        env=environment,
        timeout=15,
        check=True,
    )
    assert json.loads(third.stdout)["data"]["lifecycle"] == "submitted"


def test_http_adapter_authenticates_and_returns_the_same_durable_task(app_env: Any) -> None:
    from urllib.request import Request, urlopen

    class QuietHandler(WSGIRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

    token = "dev-http-token-" + "h" * 32
    api = TaskAPI(TaskService(app_env.db), DevelopmentBearerAuth({token: app_env.context}))
    server = make_server("127.0.0.1", 0, TaskWSGI(api), handler_class=QuietHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = json.dumps(
            envelope(
                {
                    "repository_id": str(app_env.repository_id),
                    "contract": specification().to_dict(),
                },
                "http-task",
            )
        ).encode()
        base = f"http://127.0.0.1:{server.server_port}"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        with urlopen(
            Request(base + "/v1/tasks", data=body, headers=headers), timeout=10
        ) as response:
            assert response.status == 202
            created = json.load(response)["data"]
        with urlopen(
            Request(base + f"/v1/tasks/{created['task_id']}", headers=headers), timeout=10
        ) as response:
            assert json.load(response)["data"] == created
    finally:
        server.shutdown()
        thread.join(timeout=10)
        server.server_close()
