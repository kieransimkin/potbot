"""R3/R4 bounded multi-model hypothesis generation and admitted evidence discovery.

This is a *single-owner* reasoning runner over ReasoningTree snapshots. It does
not provide distributed PostgreSQL fencing, task-budget accounting or autonomous
MCP installation. Only explicitly bound, host-authorized evidence tools may run;
model output is data, never an execution authorization.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID

from potbot.controller.reasoning_tree import ReasoningTree
from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import content_hash
from potbot.models.prompt_compiler import compile_reasoning_prompt

SearchChannel = Literal["external_libraries", "external_tools", "internal_workflows"]
_CHANNELS = frozenset(SearchChannel.__args__)
_SOURCE = re.compile(r"(?:syn|synny)://[a-zA-Z0-9_./:-]{1,256}\Z")
_SOURCE_ID = re.compile(r"[a-zA-Z0-9_.:/-]{1,256}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class EvidenceFinding:
    """Evidence from a trusted registered tool, not text supplied by the model."""

    source_ref: str
    source_sha256: str
    excerpt: str
    receipt_ref: str

    def __post_init__(self) -> None:
        if not _SOURCE_ID.fullmatch(self.source_ref) or not _DIGEST.fullmatch(self.source_sha256):
            raise InvalidInputError("evidence requires a typed source and source digest")
        if not isinstance(self.excerpt, str) or not 1 <= len(self.excerpt.encode()) <= 4096:
            raise InvalidInputError("evidence excerpt must be bounded")
        if not isinstance(self.receipt_ref, str) or not (
            re.fullmatch(r"sha256:[0-9a-f]{64}", self.receipt_ref) or
            re.fullmatch(r"syn://event/[a-zA-Z0-9_.:/-]{1,128}", self.receipt_ref)
        ):
            raise InvalidInputError("evidence requires a bounded invocation receipt")

    def to_dict(self) -> dict[str, str]:
        return {"source_ref": self.source_ref, "source_sha256": self.source_sha256,
                "excerpt": self.excerpt, "receipt_ref": self.receipt_ref}


class EvidenceInvoker(Protocol):
    """Host-owned executor must enforce workspace, Cog scope and tool policy."""

    def __call__(self, *, query: str, limit: int, run_id: str) -> tuple[EvidenceFinding, ...]: ...


@dataclass(frozen=True, slots=True)
class RegisteredEvidenceTool:
    tool_id: str
    channel: SearchChannel
    type_ref: str
    invoke: EvidenceInvoker

    def __post_init__(self) -> None:
        if (not isinstance(self.tool_id, str) or not self.tool_id or len(self.tool_id) > 128
                or self.channel not in _CHANNELS or not _SOURCE.fullmatch(self.type_ref)
                or not callable(self.invoke)):
            raise InvalidInputError("evidence tool must be a typed, admitted registration")


class EvidenceGateway:
    """No tool auto-discovery: registration is a trusted host operation.

    Tool results must carry the source digest and an admitted execution receipt.
    Failure is not evidence. The gateway never retries an uncertain invocation.
    """

    def __init__(self, tools: tuple[RegisteredEvidenceTool, ...] = ()) -> None:
        if not isinstance(tools, tuple) or len(tools) > 12:
            raise InvalidInputError("evidence registrations must be bounded")
        if any(not isinstance(t, RegisteredEvidenceTool) for t in tools):
            raise InvalidInputError("evidence tools must be registered instances")
        identities = [(t.channel, t.tool_id) for t in tools]
        if len(set(identities)) != len(identities):
            raise InvalidInputError("evidence tools must have unique channel identities")
        self._tools = tools

    def available(self) -> tuple[dict[str, str], ...]:
        return tuple({"tool_id": tool.tool_id, "channel": tool.channel,
                      "type_ref": tool.type_ref} for tool in self._tools)

    def search(self, *, channel: SearchChannel, query: str, run_id: str,
               limit: int = 3) -> tuple[EvidenceFinding, ...]:
        if channel not in _CHANNELS or not isinstance(query, str) or not query.strip() or len(query) > 512:
            raise InvalidInputError("evidence query is invalid or unbounded")
        if type(limit) is not int or not 1 <= limit <= 5:
            raise InvalidInputError("evidence query result bound is invalid")
        if not isinstance(run_id, str) or not run_id or len(run_id) > 128:
            raise InvalidInputError("evidence run identity is invalid")
        tools = [t for t in self._tools if t.channel == channel]
        if not tools:
            return ()
        # An installed Cog binding is *not* implied by a model's chosen name.
        result = tools[0].invoke(query=query, limit=limit, run_id=run_id)
        if not isinstance(result, tuple) or len(result) > limit or any(
            not isinstance(entry, EvidenceFinding) for entry in result
        ):
            raise InvalidInputError("admitted evidence tool returned invalid records")
        return result


@dataclass(frozen=True, slots=True)
class EvidenceQuery:
    channel: SearchChannel
    query: str


@dataclass(frozen=True, slots=True)
class Hypothesis:
    hypothesis: str
    assumptions: tuple[str, ...]
    questions: tuple[EvidenceQuery, ...]


def parse_hypotheses(raw: str) -> tuple[Hypothesis, ...]:
    """Validate model proposals as data; reject extra authority or tool commands."""
    if not isinstance(raw, str) or len(raw.encode()) > 32768:
        raise InvalidInputError("hypothesis response must be bounded text")
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        record: dict[str, Any] = {}
        for key, value in pairs:
            if key in record:
                raise ValueError("duplicate JSON key")
            record[key] = value
        return record

    try:
        record = json.loads(raw, object_pairs_hook=reject_duplicate_keys,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError):
        raise InvalidInputError("hypothesis response must be a JSON object") from None
    if not isinstance(record, dict) or set(record) != {"hypotheses"}:
        raise InvalidInputError("hypothesis response has unsupported fields")
    items = record["hypotheses"]
    if not isinstance(items, list) or not 1 <= len(items) <= 3:
        raise InvalidInputError("expected one to three hypotheses")
    found: list[Hypothesis] = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"hypothesis", "assumptions", "questions"}:
            raise InvalidInputError("hypothesis record has unsupported fields")
        title = item["hypothesis"]
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 2000:
            raise InvalidInputError("hypothesis text is missing or unbounded")
        assumptions = item["assumptions"]
        questions = item["questions"]
        if not isinstance(assumptions, list) or len(assumptions) > 3 or any(
            not isinstance(value, str) or not value.strip() or len(value) > 512
            for value in assumptions
        ):
            raise InvalidInputError("hypothesis assumptions must be bounded")
        if not isinstance(questions, list) or len(questions) > 3:
            raise InvalidInputError("hypothesis questions must be bounded")
        parsed: list[EvidenceQuery] = []
        for question in questions:
            if (not isinstance(question, dict) or set(question) != {"channel", "query"}
                    or question["channel"] not in _CHANNELS
                    or not isinstance(question["query"], str)
                    or not question["query"].strip()
                    or len(question["query"]) > 512):
                raise InvalidInputError("hypothesis evidence query must select a known channel")
            parsed.append(EvidenceQuery(question["channel"], question["query"]))
        found.append(Hypothesis(title.strip(), tuple(assumptions), tuple(parsed)))
    return tuple(found)


class InferenceCallback(Protocol):
    def __call__(self, model: dict[str, Any], system: str, prompt: str,
                 options: dict[str, Any], on_text: Callable[[str], None],
                 run_id: str, cancelled: Callable[[], bool] | None,
                 ) -> tuple[str, dict[str, int], str]: ...


@dataclass(frozen=True, slots=True)
class InvestigationResult:
    text: str
    usage: dict[str, int]
    finish_reason: str
    provider: str
    invocations: int
    tree_path: Path
    evidence_status: str


class ReasoningInvestigator:
    """Run two independent generators, admitted investigations and synthesis.

    A separate persistent snapshot is created per authorized request. Runtime
    owns one writer; restarts leave interrupted calls indeterminate and never
    replay them. No private reasoning trace or raw chain of thought is stored.
    """

    def __init__(self, *, root: Path, gateway: EvidenceGateway | None = None,
                 max_hypotheses: int = 6, max_tool_calls: int = 12,
                 token_budget: int = 40000) -> None:
        if type(max_hypotheses) is not int or not 1 <= max_hypotheses <= 12:
            raise InvalidInputError("hypothesis budget is invalid")
        if type(max_tool_calls) is not int or not 0 <= max_tool_calls <= 64:
            raise InvalidInputError("tool-call budget is invalid")
        if type(token_budget) is not int or not 1000 <= token_budget <= 200000:
            raise InvalidInputError("token budget is invalid")
        self.root = Path(root)
        self.gateway = gateway or EvidenceGateway()
        self.max_hypotheses = max_hypotheses
        self.max_tool_calls = max_tool_calls
        self.token_budget = token_budget

    def run(self, *, context: Any, run_id: str, task: str, system: str,
            models: list[dict[str, Any]], options: dict[str, Any],
            invoke: InferenceCallback, on_text: Callable[[str], None],
            emit: Callable[..., None], cancelled: Callable[[], bool] | None = None,
            ) -> InvestigationResult:
        from potbot.domain.errors import CapabilityUnavailableError

        if not isinstance(context.workspace_id, UUID) or not isinstance(context.principal_id, UUID):
            raise InvalidInputError("reasoning requires provisioned workspace identity")
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise InvalidInputError("reasoning run must have an opaque generated identity")
        if not models or any(not isinstance(m, dict) or not m.get("model") for m in models):
            raise CapabilityUnavailableError("no eligible reasoning models")
        if not isinstance(task, str) or not task.strip():
            raise InvalidInputError("reasoning task is missing")
        if not isinstance(system, str) or len(system) > 65536:
            raise InvalidInputError("system instructions are unbounded")
        # Only explicit, trusted path configuration; request content cannot set it.
        tree_path = self.root / str(context.workspace_id) / (run_id + ".json")
        if tree_path.exists() or tree_path.is_symlink():
            raise InvalidInputError("reasoning run already exists; never replay")
        if self.root.is_symlink() or tree_path.parent.is_symlink():
            raise InvalidInputError("reasoning directory cannot be a symlink")
        tree_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(tree_path.parent, 0o700)
        tree = ReasoningTree(tree_path, max_nodes=self.max_hypotheses + 4,
                             max_depth=3, token_budget=self.token_budget)
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        invocations = 0
        hypotheses: list[tuple[Hypothesis, str]] = []
        seen: set[str] = set()
        # Distinct providers first, deterministic model fallback if only one exists.
        preferred: list[dict[str, Any]] = []
        for candidate in models:
            if candidate["provider"] not in {m["provider"] for m in preferred}:
                preferred.append(candidate)
        for candidate in models:
            if candidate not in preferred:
                preferred.append(candidate)
        generators = (preferred * 2)[:2]

        def stopped() -> None:
            if cancelled is not None and cancelled():
                raise CapabilityUnavailableError("reasoning run cancelled before dispatch")

        def model_call(model: dict[str, Any], role: str, payload: dict[str, Any],
                       parent: str | None, final: bool = False) -> str:
            nonlocal invocations
            stopped()
            packet = compile_reasoning_prompt(
                model=f"{model['provider']}/{model['model']}", role=role, task=task,
                constraints=tuple(system[i:i+2000] for i in range(0, len(system), 2000)),
                context=payload,
            )
            allowance = min(8192, max(1024, self.token_budget // 4))
            output_limit = min(options["max_output_tokens"], 2048, allowance // 2)
            node = tree.add(f"{role}:{model['provider']}/{model['model']}",
                            parent_id=parent, model_id=f"{model['provider']}/{model['model']}", max_tokens=allowance,
                            prompt_hash=packet.identity, role=role)
            claim = tree.claim()
            if claim is None or claim.id != node.id or claim.claim_token is None:
                raise InvalidInputError("reasoning token reservation unavailable")
            emit("reasoning.node.started", run_id=run_id, node_id=node.id, role=role,
                 model=model["model"], prompt_hash=packet.identity)
            selection_options = {**options, "max_output_tokens": output_limit}
            # Emit *only* the final synthesis deltas to the OpenAI stream.
            output, measured, finish = invoke(
                model, packet.system, packet.user, selection_options,
                on_text if final else (lambda _delta: None), run_id, cancelled,
            )
            invocations += 1
            if finish != "stop":
                raise CapabilityUnavailableError("reasoning model completion was interrupted")
            if any(type(measured.get(k)) is not int or measured[k] < 0 for k in usage):
                raise InvalidInputError("provider returned invalid usage")
            if measured["total_tokens"] > allowance:
                raise InvalidInputError("reported tokens exceed the reserved node allowance")
            tree.finish(node.id, claim.claim_token, output[:32768],
                        tokens=measured["total_tokens"])
            for key in usage:
                usage[key] += measured[key]
            emit("reasoning.node.completed", run_id=run_id, node_id=node.id,
                 role=role, usage=measured, prompt_hash=packet.identity)
            return output

        for model in generators:
            output = model_call(model, "hypothesis_generator", {
                "task_revision": tree.revision,
                "registered_tools": list(self.gateway.available()),
            }, None)
            for item in parse_hypotheses(output):
                normalized = " ".join(item.hypothesis.casefold().split())
                if normalized in seen:
                    emit("reasoning.hypothesis.duplicate", run_id=run_id,
                         hypothesis_hash=content_hash(normalized))
                    continue
                seen.add(normalized)
                if len(hypotheses) >= self.max_hypotheses:
                    break
                hypotheses.append((item, f"{model['provider']}/{model['model']}"))

        investigation: list[dict[str, Any]] = []
        tool_calls = 0
        per_run_cache: dict[tuple[str, str], tuple[EvidenceFinding, ...]] = {}
        configured_channels = {t["channel"] for t in self.gateway.available()}
        for item, model_id in hypotheses:
            stopped()
            node = tree.add(item.hypothesis, model_id=model_id, max_tokens=1,
                            role="investigator")
            claim = tree.claim()
            if claim is None or claim.id != node.id or claim.claim_token is None:
                raise InvalidInputError("evidence node could not be claimed")
            records: list[dict[str, Any]] = []
            # Each branch has at most three questions; the gateway dispatches
            # only explicitly registered, host-authorized tools.
            for question_record in item.questions[:3]:
                channel, question = question_record.channel, question_record.query
                if tool_calls >= self.max_tool_calls:
                    records.append({"channel": channel, "status": "budget_limited"})
                    continue
                if channel not in configured_channels:
                    records.append({"channel": channel, "status": "unavailable"})
                    continue
                cache_key = (channel, question.casefold().strip())
                if cache_key in per_run_cache:
                    found = per_run_cache[cache_key]
                    records.append({"channel": channel, "status": "completed",
                                    "reused": True,
                                    "findings": [entry.to_dict() for entry in found]})
                    continue
                tool_calls += 1
                emit("reasoning.evidence.started", run_id=run_id, node_id=node.id,
                     channel=channel, query_hash=content_hash(question))
                # An exception here leaves an indeterminate node on recovery;
                # never retry an uncertain external action automatically.
                found = self.gateway.search(channel=channel, query=question,
                                            run_id=run_id, limit=3)
                per_run_cache[cache_key] = found
                records.append({"channel": channel, "status": "completed",
                                "findings": [entry.to_dict() for entry in found]})
                emit("reasoning.evidence.completed", run_id=run_id, node_id=node.id,
                     channel=channel, count=len(found))
            receipts = tuple(result["receipt_ref"] for record in records
                             for result in record.get("findings", []))
            tree.finish(node.id, claim.claim_token,
                        json.dumps({"investigation": records}, sort_keys=True),
                        tokens=0, evidence=receipts)
            investigation.append({"hypothesis": item.hypothesis,
                                  "assumptions": list(item.assumptions),
                                  "investigation": records,
                                  "node_id": node.id})
        emit("reasoning.investigation.complete", run_id=run_id,
             branches=len(investigation), tool_calls=tool_calls,
             missing_channels=sorted(_CHANNELS - configured_channels))
        synthesis_model = generators[-1]
        answer = model_call(synthesis_model, "synthesizer", {
            "branches": investigation,
            "available_evidence_tools": list(self.gateway.available()),
            "verified_scope": "Only findings with source digests and execution receipts are tool evidence.",
        }, None, final=True)
        return InvestigationResult(answer, usage, "stop", synthesis_model["provider"],
                                   invocations, tree_path,
                                   "executed" if tool_calls else "unavailable")


def canonical_cog_search_tool(*, runtime: Any, channel: SearchChannel) -> RegisteredEvidenceTool:
    """Bind the installed, admitted SYN search Cog to R4 evidence collection.

    The host must have constructed ToolContractRuntime.from_workspace with
    provisioned actor, sealed SYN snapshot, approved corpus, and admission hook.
    This adapter never executes an unregistered model-supplied command.
    """
    import asyncio
    from uuid import uuid4

    from potbot.cogs.contracts import CogRunContext, TypedCogValue
    from potbot.cogs.registry import BoundCogRegistry
    from potbot.cogs.tool_contracts import SEARCH

    registry = BoundCogRegistry.tool_contracts()

    def admitted_lookup(*, query: str, limit: int, run_id: str) -> tuple[EvidenceFinding, ...]:
        invocation = uuid4()
        scope = CogRunContext(
            registry.snapshot.fingerprint, runtime.task_revision,
            runtime.repository_state, invocation,
        )
        outcome = asyncio.run(registry.invoke(
            str(SEARCH.cog_ref),
            TypedCogValue(SEARCH.input_type_ref,
                          {"schema_version": 1, "text": query, "limit": limit}),
            scope, controller=runtime,
        ))
        items = outcome.value.value["items"]
        if not isinstance(items, list):
            raise InvalidInputError("Cog returned a malformed search result")
        return tuple(EvidenceFinding(
            source_ref=item["id"], source_sha256=item["source_sha256"],
            excerpt=item["text"], receipt_ref="sha256:" + outcome.receipt.fingerprint,
        ) for item in items)

    return RegisteredEvidenceTool("potbot.cog.contract-search.v1", channel,
                                  SEARCH.cog_type_ref, admitted_lookup)
