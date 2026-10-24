"""Pure budget binding and provider qualification fixtures without paid calls."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.api.openai_compat import CompatibilityError, parse_chat, parse_responses
from potbot.controller.reasoning_investigation import ReasoningInvestigator
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError
from potbot.models.hosted_policy import HostedPolicy, RateRule
from potbot.models.providers import ProviderCompleted, ProviderTextDelta, ProviderUsage
from scripts.qualify_hosted_reasoning import qualification_request, qualify


def test_task_binding_is_explicit_and_only_on_potbot() -> None:
    identity = str(uuid4())
    body = {"model": "potbot", "messages": [{"role": "user", "content": "Hello"}],
            "potbot_task_id": identity}
    assert parse_chat(body)["potbot_task_id"] == identity
    assert parse_responses({"model": "potbot", "input": "Hello",
                            "potbot_task_id": identity})["potbot_task_id"] == identity
    with pytest.raises(CompatibilityError):
        parse_chat({**body, "model": "openai/model"})
    with pytest.raises(CompatibilityError):
        parse_chat({**body, "potbot_task_id": "../wrong"})
    with pytest.raises(InvalidInputError):
        ReasoningInvestigator(root=Path("/tmp/reasoning"),
                              budget_journal=object())  # type: ignore[arg-type]


def test_live_qualification_policy_and_provider_stream() -> None:
    policy = HostedPolicy(allow_prompt_disclosure=True, rules=(
        RateRule("openai", "fixture-model", 100000, 100000, protocol="responses"),
        RateRule("gemini", "fixture-model", 100000, 100000, protocol="gemini"),
    ))
    class Credentials:
        def resolve(self, value: str) -> str:
            assert value.startswith("secret://")
            return "credential"

    class FixtureClient:
        def __init__(self, resolver, *, authorize):
            self.resolver, self.authorize = resolver, authorize
            self.closed = False
        def stream(self, request, identity):
            self.authorize(request)
            yield ProviderTextDelta('{"qualification":"ok"}')
            yield ProviderCompleted("stop", ProviderUsage(17, 5, 22), "fixture-model", "fixture-id")
        def close(self):
            self.closed = True

    for provider in ("openai", "gemini"):
        req = qualification_request(provider, "fixture-model", policy)
        assert req.response_format == "json_object"
        result = qualify(req, policy, Credentials(), client_factory=FixtureClient)
        assert result["result"] == "pass"
        assert result["usage"]["total_tokens"] == 22
        assert "credential" not in json.dumps(result)
        assert "qualification" not in result  # no output text persisted
    with pytest.raises(InvalidInputError):
        qualification_request("xai", "fixture-model", policy)


def test_reasoning_budget_migration_keeps_previous_history() -> None:
    from potbot.persistence.schema import application_migrations

    migrations = application_migrations()
    assert [(m.version, m.name) for m in migrations[-2:]] == [
        (24, "reasoning_runtime_schema"), (25, "reasoning_budget_schema")
    ]
    migration = migrations[-1]
    assert migration.workspace_tables == ("reasoning_budget_invocations",)
    assert any("FOREIGN KEY(workspace_id,reservation_id)" in sql
               for sql in migration.statements)


def test_investigator_budget_dispatch_wraps_every_provider_call(tmp_path: Path) -> None:
    from types import SimpleNamespace

    events: list[str] = []
    context = WorkspaceContext(uuid4(), uuid4())
    task_id = uuid4()
    class Store:
        def start(self, *_args):
            return uuid4()
        def checkpoint(self, *_args):
            events.append("checkpoint")

    class Journal:
        policy = SimpleNamespace(max_timeout_seconds=90)
        def prepare(self, *_args, **_kwargs):
            events.append("reserve")
            return object()
        def dispatch(self, *_args):
            events.append("dispatch")
        def observe(self, *_args, **_kwargs):
            events.append("observe")
        def uncertain(self, *_args):
            events.append("uncertain")

    def inference(model, system, prompt, options, on_text, run_id, cancelled):
        events.append("provider")
        packet = json.loads(prompt.split("JSON data):\n", 1)[1])
        if "'hypotheses'" in system:
            answer = json.dumps({"hypotheses": [{
                "hypothesis": "Use an index", "assumptions": [], "questions": [],
            }]})
        elif "'evaluations'" in system:
            answer = json.dumps({"evaluations": [{
                "node_id": branch["node_id"], "relevance": 4,
                "evidence_alignment": 1, "falsification_risk": 1,
                "uncertainty": 1, "recommendation": "keep",
                "rationale": "Plausible", "issues": [], "tests": [],
            } for branch in packet["context"]["branches"]]})
        else:
            answer = "Index may help."
        return answer, {"prompt_tokens": 11, "completion_tokens": 5,
                        "total_tokens": 16}, "stop"

    from potbot.controller.reasoning_investigation import ReasoningInvestigator

    engine = ReasoningInvestigator(root=tmp_path, state_store=Store(),
                                   budget_journal=Journal())  # type: ignore[arg-type]
    result = engine.run(
        context=context, run_id=uuid4().hex, task="Improve DB", system="",
        models=[{"provider": "openai", "model": "fixture-model"}],
        options={"max_output_tokens": 512, "potbot_task_id": str(task_id)},
        invoke=inference, on_text=lambda _text: None,
        emit=lambda *_args, **_kwargs: None,
    )
    assert result.invocations > 1
    assert events.count("reserve") == events.count("provider")
    assert events.count("dispatch") == events.count("provider")
    assert events.count("observe") == events.count("provider")
    assert "uncertain" not in events
