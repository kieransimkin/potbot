"""Service-manager and native credential installation never need privileged CI."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot import service
from potbot.credentials import CredentialStore
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError
from potbot.models.hosted_policy import HostedPolicy
from potbot.service_credentials import ServiceCredentials


def test_configuration_round_trip_permissions_and_validation(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "project_root", lambda: tmp_path)
    policy = tmp_path / "providers.json"
    policy.write_text(json.dumps(HostedPolicy().to_dict()))
    config = service.ServiceConfig(str(uuid4()), str(uuid4()), str(policy))
    service._atomic_configuration(config)
    assert service.ServiceConfig.load(service.config_path()) == config
    assert service.config_path().stat().st_mode & 0o077 == 0
    with pytest.raises(InvalidInputError):
        service.ServiceConfig(str(uuid4()), str(uuid4()), "relative/path").validate()


def test_install_generates_token_once_and_uses_user_manager(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "project_root", lambda: tmp_path)
    monkeypatch.setattr(service, "resolve_postgres_dsn", lambda *a, **k: "postgresql://fake")
    monkeypatch.setattr(service, "_manager", lambda: "systemd_user")
    installed: list[str] = []
    monkeypatch.setattr(service, "install_manager", lambda **kwargs: installed.append("installed"))
    monkeypatch.setattr(service, "manager_action", installed.append)
    fake_db = SimpleNamespace(transaction=lambda _ctx: _null_context())
    monkeypatch.setattr("potbot.persistence.database.Database", lambda *a, **k: fake_db)
    monkeypatch.setattr(service, "_token_store", lambda: (True, "safe-key-not-in-config"))
    policy = tmp_path / "providers.json"
    policy.write_text(json.dumps(HostedPolicy().to_dict()))
    opts = SimpleNamespace(
        workspace_id=uuid4(),
        principal_id=uuid4(),
        provider_policy=policy,
        schema="potbot",
        host="127.0.0.1",
        port=8765,
        allowed_origin=[],
        log_content=False,
        migrate=False,
        no_start=False,
        replace=False,
        download_embeddings=False,
        operator_manifest=None,
    )
    assert service.install(opts)["started"] is True
    assert installed == ["installed", "start"]
    assert "safe-key-not-in-config" not in service.config_path().read_text()
    with pytest.raises(InvalidInputError):
        service.install(opts)


def test_install_token_is_readable_by_the_selected_service_profile(monkeypatch):
    values: dict[tuple[str, str], str] = {}
    vault = SimpleNamespace(
        get_password=lambda name, account: values.get((name, account)),
        set_password=lambda name, account, value: values.__setitem__((name, account), value),
    )
    monkeypatch.setattr("potbot.credentials.native_backend", lambda: vault)
    monkeypatch.setenv("POTBOT_CREDENTIAL_PROFILE", "managed-wsl")
    monkeypatch.delenv("POTBOT_API_TOKEN", raising=False)
    CredentialStore().set("api.access_token", "existing-default-token-" * 2)

    created, token = service._token_store()

    assert created and token
    assert ServiceCredentials().resolve("secret://api/access-token") == token
    assert CredentialStore().get("api.access_token") == "existing-default-token-" * 2
    assert service._token_store() == (False, None)


class _null_context:
    def __enter__(self):
        return None

    def __exit__(self, *args):
        return False


def test_service_unit_keeps_user_service_settings(monkeypatch, tmp_path):
    directory = tmp_path / "workspace with %"
    monkeypatch.setattr(service, "project_root", lambda: directory)
    unit = service._systemd_unit()
    assert f"WorkingDirectory={str(directory).replace('%', '%%')}" in unit.splitlines()
    assert "--user" not in unit
    assert "NoNewPrivileges=true" in unit
    assert "potbot.startup" in unit
    assert "Restart=on-failure" in unit


def test_service_unit_preserves_the_virtualenv_interpreter(monkeypatch, tmp_path):
    base = tmp_path / "base-python"
    base.touch()
    executable = tmp_path / "venv-python"
    try:
        executable.symlink_to(base)
    except OSError:
        pytest.skip("The test host does not support creating symlinks")
    monkeypatch.setattr(service.sys, "executable", str(executable))
    monkeypatch.setattr(service, "project_root", lambda: tmp_path)
    escaped = str(executable).replace("\\", "\\\\")
    assert service._service_command()[0] == str(executable)
    assert f'ExecStart="{escaped}"' in service._systemd_unit()


def test_model_install_and_service_status_parse_without_vault(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(service, "project_root", lambda: tmp_path)
    assert service.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["configured"] is False


@pytest.mark.parametrize(
    ("probe_results", "worker_exits", "worker_events"),
    [
        ((False,), False, ["service.worker_blocked"]),
        ((True,), False, ["service.worker_accounting_ready"]),
        ((True, False), True, ["service.worker_accounting_ready", "service.worker_blocked"]),
    ],
)
def test_installed_worker_waits_for_live_scoped_accounting(
    monkeypatch, tmp_path, probe_results, worker_exits, worker_events
):
    from potbot.tools import operator_attachment_s13, operator_cli, worker_telemetry

    monkeypatch.setattr(service, "project_root", lambda: tmp_path)
    monkeypatch.setattr(service, "credential_profile", lambda: "default")
    monkeypatch.setattr(service.signal, "signal", lambda *args: None)
    workspace = uuid4()
    paths = SimpleNamespace(
        workspace_id=workspace,
        image_digest="a" * 64,
        qualification_sha256="b" * 64,
        **{
            name: tmp_path / name
            for name in (
                "rootfs",
                "home",
                "model_staging",
                "shared_models",
                "shared_tools",
                "ai_config_dir",
                "ai_history_dir",
            )
        },
    )
    for path in vars(paths).values():
        if hasattr(path, "mkdir"):
            path.mkdir()
    monkeypatch.setattr(operator_cli, "parse_manifest", lambda path: paths)
    monkeypatch.setattr(operator_attachment_s13, "mount_fingerprint", lambda *paths: "c" * 64)
    monkeypatch.setattr(
        operator_attachment_s13,
        "OperatorAttachmentClient",
        lambda **kwargs: SimpleNamespace(
            mounts_sha256=kwargs["mounts_sha256"],
            healthy=lambda: True,
            require_healthy=lambda: None,
        ),
    )
    sequence = []

    class Stop:
        count = 0

        def is_set(self):
            return self.count >= 3

        def wait(self, delay):
            self.count += 1

        def set(self):
            self.count = 3

    monkeypatch.setattr(
        service,
        "threading",
        SimpleNamespace(Event=Stop, Thread=lambda **kw: SimpleNamespace(start=lambda: None)),
    )

    def popen(command, **kwargs):
        name = command[3]
        sequence.append(name)
        return SimpleNamespace(
            pid=2,
            poll=lambda: 1 if name == "jobs" and worker_exits else None,
            terminate=lambda: None,
            wait=lambda **kw: 0,
        )

    results = iter(probe_results)

    def qualify(endpoint):
        sequence.append("probe")
        if not next(results):
            raise CapabilityUnavailableError("Scoped accounting not available")

    monkeypatch.setattr(service.subprocess, "Popen", popen)
    monkeypatch.setattr(worker_telemetry, "qualify_scoped_worker", qualify)
    config = service.ServiceConfig(
        str(workspace),
        str(uuid4()),
        str(tmp_path / "policy.json"),
        operator_manifest=str(tmp_path / "operator.json"),
        scoped_worker_launcher=True,
    )
    assert service._supervise(config) == 0
    assert sequence == ["operator", "serve", "probe"] + (
        (["jobs"] if probe_results[0] else []) + (["probe"] if worker_exits else [])
    )
    records = (service.service_home() / "supervisor.jsonl").read_text().splitlines()
    events = [json.loads(line)["event"] for line in records]
    assert [event for event in events if event.startswith("service.worker_")] == worker_events


def test_systemd_user_service_only_loads_scoped_monitor_settings_when_requested():
    from potbot.tools.scoped_provision import ENV_FILE

    directive = f"EnvironmentFile=-{ENV_FILE}"
    assert directive not in service._systemd_unit()
    assert directive in service._systemd_unit(scoped_worker_launcher=True)
