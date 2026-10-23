"""CLI adapters preserve scope and admission without making paid provider calls."""

from __future__ import annotations

import builtins
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from potbot.api import jobs_cli, server_cli
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import CapabilityUnavailableError, ScopeDeniedError
from potbot.models.hosted_policy import HostedPolicy


@pytest.fixture
def rig(monkeypatch, tmp_path):
    workspace, principal, episode, job_id, invocation_id = (uuid4() for _ in range(5))
    context = WorkspaceContext(workspace, principal)
    dsn = "postgresql://fixture:private%2Fdatabase%3Dvalue@localhost/potbot_test"
    token = "api-token-" + "x" * 40
    provider_key = "provider-key-never-printed"
    scope_checks = []

    @contextmanager
    def transaction(actor):
        scope_checks.append(actor)
        if actor != context:
            raise ScopeDeniedError("private-scope-detail")
        yield object()

    database = SimpleNamespace(dsn=dsn, schema="potbot", transaction=transaction)
    service = SimpleNamespace(
        queue=Mock(return_value={"counts": {"queued": 2}}),
        list=Mock(return_value={"items": [], "next_cursor": "opaque"}),
        inspect=Mock(return_value={"id": str(job_id), "state": "running"}),
        submit=Mock(return_value={"id": str(job_id), "state": "queued"}),
        cancel=Mock(return_value={"id": str(job_id), "state": "cancelling"}),
        events=Mock(return_value={"items": [], "next_cursor": 7, "has_more": False}),
        expire=Mock(return_value=2),
    )
    gateway = SimpleNamespace(
        inspect=Mock(return_value={"id": str(invocation_id), "qualified": False}),
        list_for_job=Mock(
            return_value={"job_id": str(job_id), "items": [{"id": str(invocation_id)}]}
        ),
        reconcile=Mock(return_value={"status": "reconciled"}),
    )
    directory = SimpleNamespace(catalog=Mock(return_value={}), discover=Mock(return_value={}))
    runner = SimpleNamespace(run_once=Mock(return_value={"id": str(job_id), "state": "succeeded"}))

    def resolve(reference):
        if reference == "secret://api/access-token":
            return token
        if reference == "secret://providers/openai":
            return provider_key
        raise CapabilityUnavailableError("missing unrelated provider")

    credentials = SimpleNamespace(resolve=Mock(side_effect=resolve), environment={})
    constructors = {}
    for name, instance in (
        ("potbot.persistence.database.Database", database),
        ("potbot.controller.jobs.JobService", service),
        ("potbot.controller.tasks.TaskService", object()),
        ("potbot.models.hosted_gateway.HostedInferenceGateway", gateway),
        ("potbot.models.hosted_catalog.HostedModelDirectory", directory),
        ("potbot.controller.job_runner.HostedJobRunner", runner),
        ("potbot.service_credentials.ServiceCredentials", credentials),
    ):
        constructor = Mock(return_value=instance)
        monkeypatch.setattr(name, constructor)
        constructors[name.rsplit(".", 1)[1]] = constructor
    monkeypatch.setattr("potbot.credentials.resolve_postgres_dsn", Mock(return_value=dsn))
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(HostedPolicy().to_dict()))
    args = ["--workspace-id", str(workspace), "--principal-id", str(principal)]
    return SimpleNamespace(**locals())


def read_result(capsys):
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize(
    ("module", "args"),
    [(server_cli, ["--help"]), (jobs_cli, ["--help"]), (jobs_cli, ["worker", "--help"])],
)
def test_help_never_imports_optional_runtime_dependencies(module, args, monkeypatch, capsys):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in {"fastapi", "uvicorn", "httpx", "psycopg", "keyring"}:
            raise AssertionError("help imported an optional runtime")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    with pytest.raises(SystemExit) as exited:
        module.main(args)
    assert exited.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_serve_wires_scoped_services_origin_defaults_and_bounded_transport(rig, monkeypatch):
    app = object()
    factory, run = Mock(return_value=app), Mock()
    monkeypatch.setattr("potbot.api.server.create_app", factory)
    monkeypatch.setattr("uvicorn.run", run)
    assert server_cli.main([*rig.args, "--provider-policy", str(rig.policy)]) == 0
    assert rig.scope_checks == [rig.context]
    bound = factory.call_args.kwargs
    assert bound["authenticate"]("Bearer " + rig.token) == rig.context
    assert bound["job_service"] is rig.service
    assert bound["model_catalog"] is rig.directory.catalog
    assert bound["discover_models"] is rig.directory.discover
    assert bound["invocation_reader"] is rig.gateway.inspect
    assert bound["invocation_list_reader"] is rig.gateway.list_for_job
    assert bound["settings"].allowed_origins == (
        "http://127.0.0.1:8765",
        "http://localhost:8765",
    )
    assert run.call_args.args == (app,)
    options = run.call_args.kwargs
    assert options["host"] == "127.0.0.1" and options["port"] == 8765
    assert options["ws_max_size"] == 65536 and options["ws_max_queue"] == 16
    assert options["access_log"] is False and options["proxy_headers"] is False
    protected = rig.constructors["HostedInferenceGateway"].call_args.kwargs["protected_values"]
    assert set(protected) == {rig.dsn, "private/database=value", rig.provider_key, rig.token}
    rig.runner.run_once.assert_not_called()


@pytest.mark.parametrize(
    "options",
    [
        ["--port", "0"],
        ["--port", "65536"],
        ["--host", "0.0.0.0"],
        ["--host", "https://example.org"],
        ["--allowed-origin", "*"],
        ["--allowed-origin", "https://secret@host.invalid"],
    ],
)
def test_invalid_listener_configuration_never_binds_or_connects(rig, options, monkeypatch, capsys):
    run = Mock()
    monkeypatch.setattr("uvicorn.run", run)
    assert server_cli.main([*rig.args, "--provider-policy", str(rig.policy), *options]) == 1
    assert read_result(capsys)["error"]["code"] == "invalid_input"
    run.assert_not_called()
    assert rig.scope_checks == []


def test_explicit_external_origin_and_scope_aliases(rig, monkeypatch):
    factory = Mock(return_value=object())
    run = Mock()
    monkeypatch.setattr("potbot.api.server.create_app", factory)
    monkeypatch.setattr("uvicorn.run", run)
    assert (
        server_cli.main(
            [
                "--workspace",
                str(rig.workspace),
                "--principal",
                str(rig.principal),
                "--provider-policy",
                str(rig.policy),
                "--host",
                "0.0.0.0",
                "--allowed-origin",
                "https://jobs.example.org",
            ]
        )
        == 0
    )
    assert factory.call_args.kwargs["settings"].allowed_origins == ("https://jobs.example.org",)
    assert run.call_args.kwargs["host"] == "0.0.0.0"


def test_server_rejects_wrong_database_scope_before_serving(rig, monkeypatch, capsys):
    run = Mock()
    monkeypatch.setattr("uvicorn.run", run)
    bad = ["--workspace-id", str(uuid4()), "--principal-id", str(rig.principal)]
    assert server_cli.main([*bad, "--provider-policy", str(rig.policy)]) == 1
    result = capsys.readouterr().out
    assert json.loads(result)["error"]["code"] == "scope_denied"
    assert "private-scope-detail" not in result and rig.dsn not in result
    run.assert_not_called()


def test_jobs_read_commands_preserve_scope_and_pagination(rig, capsys):
    assert jobs_cli.main(["queue", *rig.args]) == 0
    assert read_result(capsys)["data"]["counts"] == {"queued": 2}
    rig.service.queue.assert_called_once_with(rig.context)
    assert (
        jobs_cli.main(
            ["list", *rig.args, "--state", "running", "--after", "opaque", "--limit", "7"]
        )
        == 0
    )
    read_result(capsys)
    rig.service.list.assert_called_once_with(rig.context, state="running", after="opaque", limit=7)
    assert jobs_cli.main(["inspect", str(rig.job_id), *rig.args]) == 0
    read_result(capsys)
    rig.service.inspect.assert_called_once_with(rig.context, rig.job_id)
    assert (
        jobs_cli.main(
            ["events", *rig.args, "--job-id", str(rig.job_id), "--after", "7", "--limit", "6"]
        )
        == 0
    )
    assert read_result(capsys)["data"]["next_cursor"] == 7
    rig.service.events.assert_called_once_with(rig.context, rig.job_id, after=7, limit=6)
    rig.constructors["HostedInferenceGateway"].assert_not_called()


def test_submit_reads_private_payload_and_preserves_idempotency_key(rig, tmp_path, capsys):
    payload = {
        "job_type": "model.generate",
        "episode_id": str(rig.episode),
        "input": {"prompt": "private prompt must not be echoed"},
        "selected_models": [{"provider": "openai", "model": "test-model", "role": "reasoning"}],
    }
    path = tmp_path / "job.json"
    path.write_text(json.dumps(payload))
    assert jobs_cli.main(["submit", *rig.args, "--input", str(path), "--key", "stable-key"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["data"]["state"] == "queued"
    assert payload["input"]["prompt"] not in output
    rig.service.submit.assert_called_once_with(rig.context, payload, "stable-key")


@pytest.mark.parametrize(
    "raw", ['{"prompt":1,"prompt":2}', '{"a":NaN}', "[]", '"secret-canary"', "{} trailing"]
)
def test_bad_job_document_fails_before_database_and_does_not_echo(raw, rig, tmp_path, capsys):
    path = tmp_path / "bad-job.json"
    path.write_text(raw)
    assert jobs_cli.main(["submit", *rig.args, "--input", str(path), "--key", "fixture"]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["error"]["code"] == "invalid_input"
    assert "secret-canary" not in output
    rig.constructors["Database"].assert_not_called()
    rig.service.submit.assert_not_called()


def test_cancel_expire_and_invocation_are_explicit_scoped_operations(rig, capsys):
    assert jobs_cli.main(["cancel", str(rig.job_id), *rig.args, "--key", "cancel-key"]) == 0
    assert read_result(capsys)["data"]["state"] == "cancelling"
    rig.service.cancel.assert_called_once_with(rig.context, rig.job_id, "cancel-key")
    assert jobs_cli.main(["expire", *rig.args, "--limit", "4"]) == 0
    assert read_result(capsys)["data"] == {"expired": 2, "automatic_retry": False}
    rig.service.expire.assert_called_once_with(rig.context, limit=4)
    assert jobs_cli.main(["invocation", str(rig.invocation_id), *rig.args]) == 0
    assert read_result(capsys)["data"]["qualified"] is False
    rig.gateway.inspect.assert_called_once_with(rig.context, rig.invocation_id)
    assert (
        rig.constructors["HostedInferenceGateway"].call_args.args[1].allow_prompt_disclosure
        is False
    )


def test_reconcile_reads_complete_usage_document_without_provider_policy(rig, tmp_path, capsys):
    actual = {"calls": 1, "tokens": 450, "inference_ms": 800, "money_microunits": 50}
    path = tmp_path / "usage.json"
    path.write_text(json.dumps(actual))
    assert (
        jobs_cli.main(["reconcile", str(rig.invocation_id), *rig.args, "--actual", str(path)]) == 0
    )
    assert read_result(capsys)["data"]["status"] == "reconciled"
    rig.gateway.reconcile.assert_called_once_with(rig.context, rig.invocation_id, actual=actual)
    rig.runner.run_once.assert_not_called()


def test_invocations_finds_recovery_identifiers_for_a_job(rig, capsys):
    assert jobs_cli.main(["invocations", str(rig.job_id), *rig.args]) == 0
    assert read_result(capsys)["data"] == {
        "job_id": str(rig.job_id),
        "items": [{"id": str(rig.invocation_id)}],
    }
    rig.gateway.list_for_job.assert_called_once_with(rig.context, rig.job_id)
    rig.runner.run_once.assert_not_called()


def test_worker_once_is_explicit_and_allows_unconfigured_unrelated_credentials(rig, capsys):
    assert (
        jobs_cli.main(
            [
                "worker",
                *rig.args,
                "--provider-policy",
                str(rig.policy),
                "--worker-id",
                "fixture-worker",
                "--once",
            ]
        )
        == 0
    )
    result = read_result(capsys)["data"]
    assert result["job"]["state"] == "succeeded" and result["idle"] is False
    assert rig.scope_checks == [rig.context]
    rig.runner.run_once.assert_called_once_with(rig.context, "fixture-worker")
    assert rig.constructors["HostedJobRunner"].call_args.args == (rig.service, rig.gateway)
    assert (
        rig.provider_key
        in rig.constructors["HostedInferenceGateway"].call_args.kwargs["protected_values"]
    )


def test_worker_idle_once_and_continuous_shutdown(rig, monkeypatch, capsys):
    rig.runner.run_once.return_value = None
    base = ["worker", *rig.args, "--provider-policy", str(rig.policy)]
    assert jobs_cli.main([*base, "--once"]) == 0
    assert read_result(capsys)["data"] == {"job": None, "idle": True}
    sleep = Mock(side_effect=KeyboardInterrupt)
    monkeypatch.setattr(jobs_cli.time, "sleep", sleep)
    assert jobs_cli.main([*base, "--poll-interval", "0.2"]) == 130
    results = [json.loads(line)["data"] for line in capsys.readouterr().out.splitlines()]
    assert results[0]["status"] == "listening" and results[1]["status"] == "stopped"
    sleep.assert_called_once_with(0.2)


@pytest.mark.parametrize(
    "tail",
    [
        ["list", "--limit", "0"],
        ["events", "--after", "-1"],
        ["events", "--after", str(2**53)],
        ["worker", "--poll-interval", "nan"],
        ["worker", "--poll-interval", "60"],
    ],
)
def test_cli_numeric_bounds_fail_before_runtime(tail, rig):
    with pytest.raises(SystemExit) as exited:
        jobs_cli.main(
            [*tail, *rig.args, "--provider-policy", str(rig.policy)]
            if tail[0] == "worker"
            else [*tail, *rig.args]
        )
    assert exited.value.code == 2
    rig.constructors["Database"].assert_not_called()


def test_unexpected_job_error_uses_safe_wire_response(rig, capsys):
    rig.service.queue.side_effect = RuntimeError(rig.dsn + rig.provider_key)
    assert jobs_cli.main(["queue", *rig.args]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["error"]["code"] == "internal_error"
    assert rig.dsn not in output and rig.provider_key not in output
