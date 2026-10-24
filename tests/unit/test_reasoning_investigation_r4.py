"""R3/R4 provider tests and bounded, admitted evidence provenance."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.controller.reasoning_investigation import (
    EvidenceFinding,
    EvidenceGateway,
    ReasoningInvestigator,
    RegisteredEvidenceTool,
    parse_hypotheses,
)
from potbot.controller.reasoning_tree import ReasoningTree
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError

PROPOSAL_A = json.dumps({"hypotheses": [
    {"hypothesis": "Use indexed lookup", "assumptions": ["Index supported"],
     "questions": [{"channel":"external_libraries", "query":"index guide"}, {"channel":"external_tools", "query":"index tool"}, {"channel":"internal_workflows", "query":"index workflow"}]},
    {"hypothesis": "Try a partial index", "assumptions": [], "questions": [{"channel":"external_libraries", "query":"partial guide"}]},
]})
PROPOSAL_B = json.dumps({"hypotheses": [
    {"hypothesis": "use indexed lookup", "assumptions": [], "questions": []},
    {"hypothesis": "Try a materialized view", "assumptions": [], "questions": [{"channel":"external_libraries", "query":"view docs"}]},
]})


def _critique_for(prompt: str) -> str:
    packet = json.loads(prompt.split("JSON data):\n", 1)[1])
    return json.dumps({"evaluations": [
        {"node_id": b["node_id"], "relevance": 4, "evidence_alignment": 2,
         "falsification_risk": 1, "uncertainty": 2, "recommendation": "keep",
         "rationale": "Evidence needs another independent check", "issues": [], "tests": []}
        for b in packet["context"]["branches"]
    ]})


def test_strict_hypothesis_schema_denies_model_tool_commands() -> None:
    assert len(parse_hypotheses(PROPOSAL_A)) == 2
    with pytest.raises(InvalidInputError):
        parse_hypotheses('{"hypotheses":[{"hypothesis":"abc","assumptions":[],"questions":[],"tool":"shell"}]}')
    with pytest.raises(InvalidInputError):
        parse_hypotheses("```json\n" + PROPOSAL_A + "\n```")


def test_evidence_gateway_requires_bound_registration_and_receipts() -> None:
    with pytest.raises(InvalidInputError):
        RegisteredEvidenceTool("bad", "external_tools", "not-a-syn-type", lambda **k: ())
    gateway = EvidenceGateway()
    assert gateway.search(channel="external_tools", query="hi", run_id="run") == ()
    with pytest.raises(InvalidInputError):
        gateway.search(channel="external_tools", query="x" * 513, run_id="run")
    with pytest.raises(InvalidInputError):
        EvidenceFinding("a", "wrong", "text", "receipt")


def test_r3_generates_distinct_hypotheses_r4_runs_only_admitted_tools(tmp_path: Path) -> None:
    logged = []
    executions = []
    seen_models = []
    context = WorkspaceContext(uuid4(), uuid4())
    models = [{"provider": "openai", "model": "gpt-oss-20b"},
              {"provider": "gemini", "model": "gemma-4-26b-a4b-it"}]

    def cog(*, query, limit, run_id):
        executions.append((query, limit, run_id))
        return (EvidenceFinding("syn://thing/source-1", "a" * 64, "Indexed search works", "sha256:" + "b"*64),)

    gateway = EvidenceGateway((RegisteredEvidenceTool("installed-cog", "external_libraries",
        "syn://type/potbot-contract-search-cog", cog),))
    engine = ReasoningInvestigator(root=tmp_path, gateway=gateway)

    def model(model, system, prompt, options, on_text, run_id, cancelled):
        packet = json.loads(prompt.split("JSON data):\n", 1)[1])
        seen_models.append((model["provider"], system, packet))
        if "'hypotheses'" in system:
            output = PROPOSAL_A if model["provider"] == "openai" else PROPOSAL_B
        elif "'evaluations'" in system:
            output = _critique_for(prompt)
        else:
            output = "Index is the leading candidate; only one recorded source supports it."
        on_text(output)
        return output, {"prompt_tokens": 25, "completion_tokens": 15, "total_tokens": 40}, "stop"

    published = []
    run_id = uuid4().hex
    result = engine.run(context=context, run_id=run_id,
        task="Improve SQL latency", system="Do not modify schema", models=models,
        options={"max_output_tokens": 1024}, invoke=model, on_text=published.append,
        emit=lambda kind, **fields: logged.append((kind, fields)))
    assert result.invocations == 4
    assert result.usage["total_tokens"] == 160
    assert published == [result.text]
    assert result.evidence_status == "executed"
    assert len(executions) == 3  # One query from each of the three unique branches.
    assert set(m[0] for m in seen_models[:2]) == {"openai", "gemini"}
    tree = ReasoningTree.load(result.tree_path)
    assert len(tree.nodes) == 7  # 2 generators + 3 hypotheses + critic + synthesis
    assert {n.state for n in tree.nodes.values()} == {"completed", "pruned"}
    assert sum(n.state == "pruned" for n in tree.nodes.values()) == 1
    assert all(n.prompt_hash for n in tree.nodes.values() if n.role == "hypothesis_generator")
    assert any(n.evidence for n in tree.nodes.values() if n.role == "investigator")
    assert any(e[0] == "reasoning.evidence.completed" for e in logged)
    assert any(e[0] == "reasoning.investigation.complete" for e in logged)
    with pytest.raises(InvalidInputError):
        engine.run(context=context, run_id=run_id, task="retry", system="", models=models,
            options={"max_output_tokens": 1024}, invoke=model,
            on_text=published.append, emit=lambda *args, **kwargs: None)


def test_r4_missing_tools_report_unavailable_and_do_not_fabricate_evidence(tmp_path: Path) -> None:
    context = WorkspaceContext(uuid4(), uuid4())
    engine = ReasoningInvestigator(root=tmp_path, gateway=EvidenceGateway())
    def model(model, system, prompt, options, on_text, run_id, cancelled):
        if "'hypotheses'" in system:
            output = PROPOSAL_A
        elif "'evaluations'" in system:
            output = _critique_for(prompt)
        else:
            output = "Nothing externally verified"
        return output, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, "stop"
    run = engine.run(context=context, run_id=uuid4().hex,
        task="Investigate", system="", models=[{"provider":"openai", "model":"x"}],
        options={"max_output_tokens": 256}, invoke=model, on_text=lambda _s: None,
        emit=lambda *_args, **_kwargs: None)
    assert run.evidence_status == "unavailable"
    tree = ReasoningTree.load(run.tree_path)
    assert all(not node.evidence for node in tree.nodes.values())
    assert any('"status": "unavailable"' in (node.result or "") for node in tree.nodes.values())


def test_bad_model_output_leaves_uncertain_tree_without_retry(tmp_path: Path) -> None:
    engine = ReasoningInvestigator(root=tmp_path)
    context = WorkspaceContext(uuid4(), uuid4())
    def model(*_args):
        return "not-json", {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}, "stop"
    run_id = uuid4().hex
    with pytest.raises(InvalidInputError):
        engine.run(context=context, run_id=run_id, task="Test", system="",
            models=[{"provider":"openai", "model":"x"}],
            options={"max_output_tokens": 256}, invoke=model,
            on_text=lambda _s: None, emit=lambda *_args, **_kwargs: None)
    recovered = ReasoningTree.load(tmp_path / str(context.workspace_id) / (run_id + '.json'))
    assert recovered.claim() is None
    assert recovered.run_status == "failed"


def test_tree_engine_is_available_through_openai_chat_endpoint(tmp_path: Path) -> None:
    """The published model name enters R3/R4 when configured by the server."""
    from fastapi.testclient import TestClient

    from potbot.api.openai_compat import OpenAICompatibility
    from potbot.api.server import ServerSettings, create_app
    from potbot.api.tasks import DevelopmentBearerAuth
    from potbot.models.hosted_policy import HostedPolicy, RateRule
    from potbot.models.providers import ProviderCompleted, ProviderTextDelta, ProviderUsage

    context = WorkspaceContext(uuid4(), uuid4())
    requests = []
    class Credentials:
        def resolve(self, name):
            return "test-credential"  # no real provider is used

    class Provider:
        def __init__(self, resolver, *, authorize):
            self.resolve, self.authorize = resolver, authorize
        def stream(self, request, operation_id, cancelled=None):
            self.authorize(request)
            requests.append(request)
            if "'hypotheses'" in request.system:
                text = PROPOSAL_A
            elif "'evaluations'" in request.system:
                text = _critique_for(request.prompt)
            else:
                text = "Verified evidence was unavailable; indexed lookup is unconfirmed."
            yield ProviderTextDelta(text)
            yield ProviderCompleted("stop", ProviderUsage(10, 10, 20))
        def close(self):
            pass

    def catalog(actor):
        assert actor == context
        return {"models": [{"provider": "openai", "model": "test-model",
                            "roles": ["reasoning"], "protocol": "responses",
                            "generation_enabled": True, "text_generation": True}]}

    adapter = OpenAICompatibility(catalog,
        HostedPolicy(allow_prompt_disclosure=True,
                     rules=(RateRule("openai", "test-model", 0, 0),)),
        Credentials(), client_factory=Provider,
        reasoning_investigator=ReasoningInvestigator(root=tmp_path))
    app = create_app(task_service=None, job_service=None,
                     authenticate=DevelopmentBearerAuth({"secret-token-aaaaaaaaaaaaaaaaaaaaaa": context}),
                     openai_compat=adapter,
                     settings=ServerSettings(allowed_origins=("http://testserver",)))
    with TestClient(app) as client:
        result = client.post("/v1/chat/completions", headers={"Authorization": "Bearer secret-token-aaaaaaaaaaaaaaaaaaaaaa"},
                             json={"model": "potbot", "messages": [{"role": "user", "content": "Find index"}]})
    assert result.status_code == 200, result.text
    assert result.json()["choices"][0]["message"]["content"].startswith("Verified evidence")
    assert result.json()["usage"]["total_tokens"] == 80
    assert len(requests) == 4
    assert len(list(tmp_path.rglob("*.json"))) == 1


def test_canonical_search_cog_bridge_calls_installed_controller(monkeypatch) -> None:
    from types import SimpleNamespace

    from potbot.cogs.registry import BoundCogRegistry
    from potbot.controller.reasoning_investigation import canonical_cog_search_tool

    called = []
    class FakeBound:
        snapshot = SimpleNamespace(fingerprint="a" * 64)
        async def invoke(self, ref, data, ctx, controller):
            called.append((ref, data.value, ctx, controller))
            return SimpleNamespace(
                value=SimpleNamespace(value={"items": [
                    {"id": "syn://thing/approved-source", "text": "Approved versioned documentation",
                     "source_sha256": "d" * 64}
                ]}),
                receipt=SimpleNamespace(fingerprint="e" * 64),
            )

    monkeypatch.setattr(BoundCogRegistry, "tool_contracts", lambda: FakeBound())
    host = SimpleNamespace(task_revision=uuid4(), repository_state=uuid4())
    tool = canonical_cog_search_tool(runtime=host, channel="internal_workflows")
    output = EvidenceGateway((tool,)).search(
        channel="internal_workflows", query="approved", run_id="r34-run", limit=2)
    assert len(output) == 1
    assert output[0].receipt_ref == "sha256:" + "e"*64
    assert called[0][1] == {"schema_version": 1, "text": "approved", "limit": 2}
    assert called[0][3] == host
