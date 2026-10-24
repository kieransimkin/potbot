"""Live steering is durable, request-scoped and independent of final synthesis."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from potbot.api.openai_compat import OpenAICompatibility
from potbot.api.server import ServerSettings, create_app
from potbot.api.tasks import DevelopmentBearerAuth
from potbot.controller.reasoning_investigation import ReasoningInvestigator, parse_steering
from potbot.controller.reasoning_steering import SteeringInbox
from potbot.controller.reasoning_tree import ReasoningTree
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.models.hosted_policy import HostedPolicy, RateRule


def _proposal() -> str:
    return json.dumps({"hypotheses": [
        {"hypothesis": "Use a small index", "assumptions": [], "questions": []},
    ]})


def _critique(prompt: str) -> str:
    packet = json.loads(prompt.split("JSON data):\n", 1)[1])
    return json.dumps({"evaluations": [
        {"node_id": b["node_id"], "relevance": 3, "evidence_alignment": 2,
         "falsification_risk": 1, "uncertainty": 2, "recommendation": "keep",
         "rationale": "Not externally tested", "issues": [], "tests": []}
        for b in packet["context"]["branches"]
    ]})


def test_mailbox_scoping_idempotency_and_terminal_rejection(tmp_path: Path) -> None:
    workspace, other = uuid4(), uuid4()
    run = uuid4().hex
    path = tmp_path / str(workspace) / (run + ".json")
    tree = ReasoningTree(path)
    tree.save()
    mailbox = SteeringInbox(tmp_path)
    queued = mailbox.submit(workspace, run, request_id="request-1", prompt="Use an ARM CPU")
    assert queued["status"] == "queued"
    assert mailbox.submit(workspace, run, request_id="request-1", prompt="Use an ARM CPU") == queued
    with pytest.raises(InvalidInputError):
        mailbox.submit(workspace, run, request_id="request-1", prompt="Use a GPU")
    with pytest.raises(ScopeDeniedError):
        mailbox.get(other, run, "request-1")
    with pytest.raises(InvalidInputError):
        mailbox.pending(workspace, "../escape")
    assert mailbox.pending(workspace, run) == (queued,)
    answer = mailbox.resolve(workspace, run, queued, {"decision": "no_change", "explanation": "Already handled"})
    assert mailbox.get(workspace, run, "request-1") == answer
    assert not mailbox.pending(workspace, run)
    tree.close("completed")
    assert mailbox.submit(workspace, run, request_id="request-1", prompt="Use an ARM CPU") == answer
    with pytest.raises(InvalidInputError):
        mailbox.submit(workspace, run, request_id="request-2", prompt="Late")


def test_rejects_untrusted_steering_model_decisions() -> None:
    with pytest.raises(InvalidInputError):
        parse_steering('{"decision":"run_shell","explanation":"go","branch_id":null,"hypothesis":null}')
    with pytest.raises(InvalidInputError):
        parse_steering('{"decision":"no_change","explanation":"ok","branch_id":null,"hypothesis":null,"tool":"delete"}')
    with pytest.raises(InvalidInputError):
        parse_steering('{"decision":"no_change","decision":"reopen_branch","explanation":"ok","branch_id":null,"hypothesis":null}')


def test_steering_during_active_run_revises_branch_and_replies(tmp_path: Path) -> None:
    actor = WorkspaceContext(uuid4(), uuid4())
    investigator = ReasoningInvestigator(root=tmp_path)
    run = uuid4().hex
    sent = False
    seen = []

    def provider(model, system, prompt, options, on_text, run_id, cancelled):
        nonlocal sent
        if "'hypotheses'" in system:
            if not sent:
                investigator.steering_inbox.submit(actor.workspace_id, run,
                    request_id="user-steer-1", prompt="Actually, we have only 2GB RAM")
                sent = True
            reply = _proposal()
        elif "'decision'" in system:
            reply = json.dumps({"decision": "spawn_hypothesis", "explanation":
                "Low memory makes a streaming index worth investigating.",
                "branch_id": None, "hypothesis": "Try streaming lookup"})
        elif "'evaluations'" in system:
            reply = _critique(prompt)
        else:
            reply = "Use compact indexes; streaming is an unverified alternative."
        return reply, {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7}, "stop"

    result = investigator.run(context=actor, run_id=run, task="Index the dataset",
        system="Do not exceed 2GB", models=[{"provider": "openai", "model": "fixture"}],
        options={"max_output_tokens": 400}, invoke=provider, on_text=seen.append,
        emit=lambda *_args, **_kwargs: None)
    response = investigator.steering_inbox.get(actor.workspace_id, run, "user-steer-1")
    assert response["decision"] == "spawn_hypothesis"
    assert "Low memory" in response["explanation"]
    tree = ReasoningTree.load(result.tree_path)
    assert tree.run_status == "completed"
    assert tree.revision == 1
    assert any(n.hypothesis == "Try streaming lookup" for n in tree.nodes.values())
    assert result.text == "".join(seen)


def test_http_steering_endpoints_use_authenticated_workspace(tmp_path: Path) -> None:
    actor = WorkspaceContext(uuid4(), uuid4())
    run = uuid4().hex
    tree = ReasoningTree(tmp_path / str(actor.workspace_id) / (run + ".json"))
    tree.save()
    investigator = ReasoningInvestigator(root=tmp_path)

    class Credentials:
        def resolve(self, _name):
            return "secret"

    compat = OpenAICompatibility(lambda _actor: {"models": []},
        HostedPolicy(allow_prompt_disclosure=True, rules=(RateRule("openai", "test", 0, 0),)),
        Credentials(), reasoning_investigator=investigator)
    app = create_app(task_service=None, job_service=None,
        authenticate=DevelopmentBearerAuth({"local-token-abcdefghijklmnopqrstuvwxyz123456": actor}), openai_compat=compat,
        settings=ServerSettings(allowed_origins=("http://testserver",)))
    with TestClient(app) as client:
        unauthorized = client.post(f"/v1/reasoning/{run}/steer",
            json={"request_id": "a", "prompt": "new"})
        assert unauthorized.status_code in (401, 403, 404)
        headers = {"Authorization": "Bearer local-token-abcdefghijklmnopqrstuvwxyz123456"}
        submitted = client.post(f"/v1/reasoning/{run}/steer", headers=headers,
            json={"request_id": "a", "prompt": "Consider 2GB RAM"})
        assert submitted.status_code == 202, submitted.text
        fetched = client.get(f"/v1/reasoning/{run}/steering/a", headers=headers)
        assert fetched.status_code == 200
        assert fetched.json()["status"] == "queued"
        history = client.get(f"/v1/reasoning/{run}/steering", headers=headers)
        assert history.status_code == 200
        assert len(history.json()["data"]) == 1


def test_pruned_branch_can_reopen_after_steering_during_synthesis(tmp_path: Path) -> None:
    actor = WorkspaceContext(uuid4(), uuid4())
    run = uuid4().hex
    investigator = ReasoningInvestigator(root=tmp_path)
    prompt_sent = False
    reopened_id = ""
    critique_round = 0
    events = []

    def provider(model, system, prompt, options, on_text, run_id, cancelled):
        nonlocal prompt_sent, reopened_id, critique_round
        if "'hypotheses'" in system:
            output = json.dumps({"hypotheses": [
                {"hypothesis": title, "assumptions": [], "questions": []}
                for title in ("Lightweight index", "Materialized view", "Streaming scan")
            ]})
        elif "'evaluations'" in system:
            packet = json.loads(prompt.split("JSON data):\n", 1)[1])
            ids = [b["node_id"] for b in packet["context"]["branches"]]
            if critique_round == 0:
                reopened_id = ids[-1]
            critique_round += 1
            output = json.dumps({"evaluations": [
                {"node_id": node_id,
                 "relevance": (4 if node_id == reopened_id and critique_round > 1 else 2),
                 "evidence_alignment": 2, "falsification_risk": 1, "uncertainty": 3,
                 "recommendation": "keep", "rationale": "Requires a test",
                 "issues": [], "tests": []}
                for node_id in ids
            ]})
        elif "'decision'" in system:
            output = json.dumps({"decision": "reopen_branch", "explanation":
                "New hardware information makes the streaming branch relevant again.",
                "branch_id": reopened_id, "hypothesis": None})
        else:
            if not prompt_sent:
                snapshot = json.loads((tmp_path / str(actor.workspace_id) / (run + ".json")).read_text())
                reopened_id = next(n["id"] for n in snapshot["nodes"] if n["state"] == "pruned")
                investigator.steering_inbox.submit(actor.workspace_id, run,
                    request_id="revisit", prompt="We cannot create indexes")
                prompt_sent = True
            output = "Prefer the newly reconsidered option"
        return output, {"prompt_tokens": 3, "completion_tokens": 3,
                        "total_tokens": 6}, "stop"

    result = investigator.run(context=actor, run_id=run, task="Find a fast lookup",
        system="", models=[{"provider": "openai", "model": "fixture"}],
        options={"max_output_tokens": 300}, invoke=provider,
        on_text=lambda _chunk: None,
        emit=lambda kind, **fields: events.append(kind))
    reply = investigator.steering_inbox.get(actor.workspace_id, run, "revisit")
    assert reply["decision"] == "reopen_branch"
    assert reply["branch_id"] == reopened_id
    assert critique_round == 2
    assert "branch.reopened" in [entry["type"] for entry in ReasoningTree.load(result.tree_path).events]
    assert result.text == "Prefer the newly reconsidered option"
    assert investigator.steering_inbox.history(actor.workspace_id, run)[0]["status"] == "decided"


def test_invalid_reopen_is_explained_without_mutating_tree(tmp_path: Path) -> None:
    actor = WorkspaceContext(uuid4(), uuid4())
    run = uuid4().hex
    investigator = ReasoningInvestigator(root=tmp_path)

    def provider(model, system, prompt, options, on_text, run_id, cancelled):
        if "'hypotheses'" in system:
            investigator.steering_inbox.submit(actor.workspace_id, run,
                request_id="unknown-branch", prompt="Revisit branch ABC") if not sent[0] else None
            sent[0] = True
            output = _proposal()
        elif "'decision'" in system:
            output = json.dumps({"decision": "reopen_branch", "explanation": "Reopen it",
                                 "branch_id": "unknown", "hypothesis": None})
        elif "'evaluations'" in system:
            output = _critique(prompt)
        else:
            output = "No suitable branch found"
        return output, {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4}, "stop"

    sent = [False]
    result = investigator.run(context=actor, run_id=run, task="Investigate",
        system="", models=[{"provider": "openai", "model": "fixture"}],
        options={"max_output_tokens": 300}, invoke=provider,
        on_text=lambda _text: None, emit=lambda *_args, **_fields: None)
    response = investigator.steering_inbox.get(actor.workspace_id, run, "unknown-branch")
    assert response["decision"] == "needs_clarification"
    assert "not a previously rejected investigation" in response["explanation"]
    assert ReasoningTree.load(result.tree_path).revision == 0
