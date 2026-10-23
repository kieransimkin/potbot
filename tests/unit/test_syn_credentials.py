"""Syn secrets cannot cross installation, workspace or destination boundaries."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import quote
from uuid import uuid4

import pytest
from syn_client import SynClient

from potbot import credentials as management
from potbot.api.jobs_cli import protected_values
from potbot.controller.syn_service import ScopedSynClient, configured_client
from potbot.credentials import CredentialStore
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError
from potbot.domain.syn_credentials import SynCredentialScope, service_origin
from potbot.service_credentials import ServiceCredentials
from potbot.tools.secrets import CaptureRedactor

TOKEN = "synthetic-Syn-credential-canary+/=" + "a" * 32


class Vault:
    def __init__(self):
        self.values = {}

    def get_password(self, service, username):
        return self.values.get((service, username))

    def set_password(self, service, username, value):
        self.values[service, username] = value

    def delete_password(self, service, username):
        self.values.pop((service, username), None)


def configuration(**changes):
    return {
        "POTBOT_INSTALLATION_ID": str(uuid4()),
        "POTBOT_SYN_WORKSPACE_ID": str(uuid4()),
        "POTBOT_SYN_URL": "https://syn.example.test",
        **changes,
    }


def stored_credentials(environment):
    vault = Vault()
    store = CredentialStore("synthetic-test", backend=vault)
    scope = SynCredentialScope.from_environment(environment)
    store.set(scope.vault_name, TOKEN)
    return ServiceCredentials(environment=environment, store=store), scope, vault


@pytest.mark.parametrize(
    "origin,canonical",
    [
        ("https://SYN.example.test:443/", "https://syn.example.test"),
        ("http://127.0.0.1:80/", "http://127.0.0.1"),
        ("http://[::1]:8000", "http://[::1]:8000"),
        ("https://syn.example.test:8443", "https://syn.example.test:8443"),
    ],
)
def test_origin_identity_uses_one_canonical_transport_destination(origin, canonical):
    assert service_origin(origin) == canonical
    installation, workspace = uuid4(), uuid4()
    assert SynCredentialScope.for_service(
        installation, workspace, origin
    ) == SynCredentialScope.for_service(installation, workspace, canonical)


@pytest.mark.parametrize(
    "origin",
    [
        "http://syn.example.test",
        "https://token@host.test",
        "https://host.test/path",
        "https://host.test?token=secret",
        "https://host.test#fragment",
        "https://host.test:99999",
        "https://host.test\n",
        "https://host.test\\other",
        "",
        None,
    ],
)
def test_bad_destinations_are_rejected_without_echoing_input(origin):
    with pytest.raises(InvalidInputError) as error:
        service_origin(origin)
    assert "token@" not in str(error.value) and "fragment" not in str(error.value)


def test_native_secret_is_profile_and_scope_bound_and_environment_wins():
    environment = configuration()
    credentials, scope, vault = stored_credentials(environment)
    assert credentials.resolve(scope.reference) == TOKEN
    environment["POTBOT_SYN_TOKEN"] = TOKEN + "-rotated"
    assert credentials.resolve(scope.reference) == TOKEN + "-rotated"
    assert len(vault.values) == 1
    assert (
        ServiceCredentials(
            environment=environment, store=CredentialStore("other", backend=vault)
        ).resolve(scope.reference)
        == TOKEN + "-rotated"
    )
    environment.pop("POTBOT_SYN_TOKEN")
    with pytest.raises(CapabilityUnavailableError):
        ServiceCredentials(
            environment=environment, store=CredentialStore("other", backend=vault)
        ).resolve(scope.reference)


@pytest.mark.parametrize(
    "field,value",
    [
        ("POTBOT_INSTALLATION_ID", str(uuid4())),
        ("POTBOT_SYN_WORKSPACE_ID", str(uuid4())),
        ("POTBOT_SYN_URL", "https://different.example.test"),
    ],
)
def test_scope_mismatch_denies_before_vault_or_environment_secret_use(field, value):
    environment = configuration(POTBOT_SYN_TOKEN=TOKEN)
    scope = SynCredentialScope.from_environment(environment)
    environment[field] = value
    store = Mock(spec=CredentialStore)
    credentials = ServiceCredentials(environment=environment, store=store)
    with pytest.raises(ScopeDeniedError):
        credentials.resolve(scope.reference)
    store.get.assert_not_called()


@pytest.mark.parametrize("field", ["POTBOT_INSTALLATION_ID", "POTBOT_SYN_WORKSPACE_ID"])
def test_legacy_unbound_token_requires_explicit_scope(field):
    environment = configuration(POTBOT_SYN_TOKEN=TOKEN)
    environment.pop(field)
    with pytest.raises(InvalidInputError):
        configured_client(environment)


@pytest.mark.parametrize("token", ["short", "x" * 4097, "x" * 32 + "\n", "é" * 40])
def test_syn_token_bounds_match_shared_client_without_leaking_value(token):
    environment = configuration(POTBOT_SYN_TOKEN=token)
    credentials = ServiceCredentials(environment=environment, store=Mock())
    with pytest.raises(CapabilityUnavailableError) as error:
        credentials.resolve(SynCredentialScope.from_environment(environment).reference)
    assert token not in str(error.value)


def test_every_adapter_operation_rejects_another_workspace_before_network():
    scope = SynCredentialScope.from_environment(configuration())
    transport = Mock(spec=SynClient)
    client = ScopedSynClient(scope, transport)
    workspace = str(uuid4())
    for operation in (
        lambda: client.head(workspace),
        lambda: client.messages(workspace),
        lambda: client.verify(workspace),
        lambda: client.send(
            workspace, message_id=str(uuid4()), channel="test", sender="potbot", payload={}
        ),
    ):
        with pytest.raises(ScopeDeniedError):
            operation()
    assert transport.mock_calls == []
    client.head(str(scope.workspace_id))
    transport.head.assert_called_once_with(str(scope.workspace_id))


def test_configured_client_resolves_native_secret_and_rejects_foreign_authenticated_context():
    environment = configuration()
    credentials, scope, _vault = stored_credentials(environment)
    client = configured_client(
        environment, workspace_id=scope.workspace_id, credentials=credentials
    )
    assert client.scope == scope
    with pytest.raises(ScopeDeniedError):
        configured_client(environment, workspace_id=uuid4(), credentials=credentials)
    assert TOKEN not in repr(client)


@pytest.mark.parametrize("native", [False, True])
def test_capture_filters_include_syn_secret_and_common_encodings(native):
    environment = configuration() if native else {"POTBOT_SYN_TOKEN": TOKEN}
    if native:
        credentials, _scope, _vault = stored_credentials(environment)
    else:
        credentials = ServiceCredentials(
            environment=environment, store=Mock(get=Mock(return_value=None))
        )
    database = SimpleNamespace(dsn="postgresql://fixture:database-canary@localhost/test")
    capture = CaptureRedactor(protected_values(database, credentials))
    for value in (TOKEN, quote(TOKEN, safe=""), base64.b64encode(TOKEN.encode()).decode()):
        assert capture.redact_bytes(value.encode()) == b"[REDACTED]"


def test_scoped_credential_management_has_hidden_entry_presence_and_confirmed_delete(
    monkeypatch, capsys
):
    vault = Vault()
    monkeypatch.setattr(management, "native_backend", lambda: vault)
    monkeypatch.setattr(management, "_hidden_prompt", lambda _: TOKEN)
    installation, workspace = uuid4(), uuid4()
    options = [
        "--installation-id",
        str(installation),
        "--workspace-id",
        str(workspace),
        "--url",
        "https://syn.example.test",
    ]
    for command in ("syn-set", "syn-status", "syn-delete", "syn-status"):
        assert management.main(["--profile", "syn-test", command, *options]) == 0
    output = capsys.readouterr()
    records = [json.loads(line)["data"] for line in output.out.splitlines()]
    assert records[0]["saved"] and records[1]["configured"] and records[2]["deleted"]
    assert records[3]["configured"] is False
    assert TOKEN not in output.out + output.err and vault.values == {}


def test_reference_parser_rejects_noncanonical_and_unbounded_credentials():
    scope = SynCredentialScope.for_service(uuid4(), uuid4(), "https://syn.example.test")
    assert SynCredentialScope.from_reference(scope.reference) == scope
    store = CredentialStore(backend=Vault())
    for reference in (
        scope.reference + "/extra",
        scope.reference.upper(),
        "secret://syn/" + "x" * 10000,
    ):
        with pytest.raises(InvalidInputError):
            SynCredentialScope.from_reference(reference)
    with pytest.raises(InvalidInputError):
        store.set("syn.arbitrary.name", TOKEN)


def test_missing_or_locked_scoped_vault_fails_without_echoing_backend_detail():
    environment = configuration()
    scope = SynCredentialScope.from_environment(environment)
    for store in (
        Mock(get=Mock(return_value=None)),
        Mock(get=Mock(side_effect=CapabilityUnavailableError("private-backend-canary"))),
    ):
        with pytest.raises(CapabilityUnavailableError) as error:
            configured_client(
                environment, credentials=ServiceCredentials(environment=environment, store=store)
            )
        assert "private-backend-canary" not in str(error.value)
        store.get.assert_called_once_with(scope.vault_name)


def test_rest_scope_rejection_preserves_access_denial_without_network():
    from typing import cast

    from fastapi.testclient import TestClient

    from potbot.api.server import JobOperations, create_app
    from potbot.api.tasks import DevelopmentBearerAuth
    from potbot.controller.tasks import TaskService
    from potbot.domain.auth import WorkspaceContext

    environment = configuration()
    scope = SynCredentialScope.from_environment(environment)
    transport = Mock(spec=SynClient)
    actor = WorkspaceContext(uuid4(), uuid4())
    token = "synthetic-browser-token-" + "a" * 40
    app = create_app(
        task_service=cast(TaskService, object()),
        job_service=cast(JobOperations, object()),
        authenticate=DevelopmentBearerAuth({token: actor}),
        syn_service=ScopedSynClient(scope, transport),
    )
    with TestClient(app, raise_server_exceptions=False) as http:
        response = http.get("/v1/syn/head", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 404
    assert str(scope.workspace_id) not in response.text and transport.mock_calls == []
