"""R6 durable run schema and provider-native structured output contracts."""

from __future__ import annotations

from uuid import uuid4

import pytest

from potbot.controller.reasoning_runtime import PostgresReasoningStore, PostgresSteeringInbox
from potbot.domain.errors import InvalidInputError
from potbot.models.providers import ProviderRequest, ProviderSelection
from potbot.models.providers.protocols import request_body
from potbot.persistence.schema import application_migrations


def test_new_migration_is_append_only_and_workspace_scoped() -> None:
    revisions = application_migrations()
    migration = next(m for m in revisions if m.version == 24)
    assert migration.version == 24 and migration.name == "reasoning_runtime_schema"
    assert set(migration.workspace_tables) == {
        "reasoning_runtime_runs", "reasoning_runtime_steering", "reasoning_runtime_events"
    }
    assert migration.append_only_tables == ("reasoning_runtime_events",)
    assert "FOR UPDATE SKIP LOCKED" in __import__(
        "inspect"
    ).getsource(PostgresReasoningStore.expire)


def test_json_only_model_calls_use_native_provider_protocols() -> None:
    for provider, protocol in (("openai", "responses"), ("openai", "chat_completions"),
                               ("gemini", "gemini")):
        request = ProviderRequest(ProviderSelection(provider, "test-model", protocol),
                                  prompt="Return a JSON object", response_format="json_object")
        path, body = request_body(request)
        if protocol == "responses":
            assert path == "/v1/responses"
            assert body["text"]["format"]["type"] == "json_object"
        elif protocol == "chat_completions":
            assert body["response_format"] == {"type": "json_object"}
        else:
            assert path.endswith(":streamGenerateContent?alt=sse")
            assert body["generationConfig"]["responseMimeType"] == "application/json"
        _, ordinary = request_body(ProviderRequest(ProviderSelection(provider, "test-model", protocol), prompt="Hello"))
        assert "text" not in ordinary or protocol != "responses"
    with pytest.raises(InvalidInputError):
        ProviderRequest(ProviderSelection("anthropic", "claude-test"), prompt="hi", response_format="json_object")
    with pytest.raises(InvalidInputError):
        ProviderRequest(ProviderSelection("gemini", "gemini-test"), prompt="hi", response_format="yaml")


def test_reasoning_runtime_validates_run_ids_and_steering_keys() -> None:
    assert len(PostgresSteeringInbox._key("steer-1")) == 64
    with pytest.raises(InvalidInputError):
        PostgresSteeringInbox._key("../escape")
    from potbot.controller.reasoning_runtime import _run_id

    assert _run_id(uuid4().hex)
    with pytest.raises(InvalidInputError):
        _run_id("../escape")


def test_reasoning_tree_publishes_fenced_checkpoint_hook(tmp_path) -> None:
    from potbot.controller.reasoning_tree import ReasoningTree

    emitted = []
    tree = ReasoningTree(tmp_path / "run.json", checkpoint=emitted.append)
    tree.save()
    node = tree.add("Check A")
    assert len(emitted) == 2
    assert emitted[-1]["nodes"][0]["id"] == node.id
    assert emitted[-1]["events"][0]["type"] == "node.created"
