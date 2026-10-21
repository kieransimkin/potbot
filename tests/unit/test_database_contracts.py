"""Pure authentication and canonical serialization contracts."""

from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.persistence.database import Database


def test_canonical_identity_ignores_object_key_order_but_preserves_sequence_order() -> None:
    assert canonical_json({"b": 2, "a": [1, 2]}) == '{"a":[1,2],"b":2}'
    assert content_hash({"b": 2, "a": [1, 2]}) == content_hash({"a": [1, 2], "b": 2})
    assert content_hash([1, 2]) != content_hash([2, 1])
    assert content_hash({"unicode": "é"}) == content_hash({"unicode": "é"})


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), {1: "bad"}, uuid4(), {"x": object()}]
)
def test_nonfinite_and_implicit_serializations_are_rejected(value: object) -> None:
    with pytest.raises(InvalidInputError):
        canonical_json(value)


def test_context_is_immutable_and_accepts_only_typed_identities() -> None:
    context = WorkspaceContext(uuid4(), uuid4())
    with pytest.raises(FrozenInstanceError):
        context.workspace_id = uuid4()  # type: ignore[misc]
    with pytest.raises(InvalidInputError):
        WorkspaceContext("not-a-uuid", uuid4())  # type: ignore[arg-type]


def test_database_identity_rejects_sql_fragments_and_hides_credentials() -> None:
    database = Database("postgresql://secret-password@localhost/example")
    assert "secret-password" not in repr(database)
    with pytest.raises(InvalidInputError):
        Database("example", "potbot; DROP SCHEMA public")


def test_administrative_help_requires_no_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from potbot.persistence.admin_cli import main

    monkeypatch.delenv("POTBOT_ADMIN_DSN", raising=False)
    monkeypatch.delenv("POTBOT_DSN", raising=False)
    with pytest.raises(SystemExit) as outcome:
        main(["--help"])
    assert outcome.value.code == 0
    assert "principal-bind" in capsys.readouterr().out


def test_administrative_missing_configuration_is_a_versioned_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from potbot.persistence.admin_cli import main

    monkeypatch.delenv("POTBOT_ADMIN_DSN", raising=False)
    assert main(["migrate"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["error"]["code"] == "invalid_input"
    assert payload["error"]["retryable"] is False
