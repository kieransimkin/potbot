"""Service-manager and native credential installation never need privileged CI."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot import service
from potbot.domain.errors import InvalidInputError
from potbot.models.hosted_policy import HostedPolicy


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
    monkeypatch.setattr(service, "install_manager", lambda: installed.append("installed"))
    monkeypatch.setattr(service, "manager_action", installed.append)
    fake_db = SimpleNamespace(transaction=lambda _ctx: _null_context())
    monkeypatch.setattr("potbot.persistence.database.Database", lambda *a, **k: fake_db)
    monkeypatch.setattr(service, "_token_store", lambda: (True, "safe-key-not-in-config"))
    policy = tmp_path / "providers.json"
    policy.write_text(json.dumps(HostedPolicy().to_dict()))
    opts = SimpleNamespace(workspace_id=uuid4(), principal_id=uuid4(), provider_policy=policy,
                           schema="potbot", host="127.0.0.1", port=8765, allowed_origin=[],
                           log_content=False, migrate=False, no_start=False, replace=False,
                           download_embeddings=False)
    assert service.install(opts)["started"] is True
    assert installed == ["installed", "start"]
    assert "safe-key-not-in-config" not in service.config_path().read_text()
    with pytest.raises(InvalidInputError):
        service.install(opts)


class _null_context:
    def __enter__(self):
        return None
    def __exit__(self, *args):
        return False


def test_service_unit_uses_resolved_python_and_not_root(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "project_root", lambda: tmp_path)
    unit = service._systemd_unit()
    assert "--user" not in unit
    assert "NoNewPrivileges=true" in unit
    assert "potbot.startup" in unit
    assert "Restart=on-failure" in unit


def test_model_install_and_service_status_parse_without_vault(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(service, "project_root", lambda: tmp_path)
    assert service.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["configured"] is False
