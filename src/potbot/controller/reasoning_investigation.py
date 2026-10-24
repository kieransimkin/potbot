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

from potbot.controller.reasoning_evaluation import parse_critiques, rank_branches
from potbot.controller.reasoning_runtime import PostgresReasoningStore
from potbot.controller.reasoning_steering import SteeringInbox
from potbot.controller.reasoning_tree import ReasoningTree
from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import content_hash
from potbot.models.prompt_compiler import ReasoningRole, compile_reasoning_prompt

SearchChannel = Literal["external_libraries", "external_tools", "internal_workflows"]
_CHANNELS: frozenset[SearchChannel] = frozenset(
    ("external_libraries", "external_tools", "internal_workflows")
)
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


def parse_steering(raw: str) -> dict[str, Any]:
    """Parse one bounded model suggestion; this never authorizes execution."""
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 8192:
        raise InvalidInputError("steering response is unbounded")

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate steering key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=no_duplicates,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError):
        raise InvalidInputError("steering decision must be valid JSON") from None
    if not isinstance(value, dict) or set(value) != {
        "decision", "explanation", "branch_id", "hypothesis"
    }:
        raise InvalidInputError("steering decision has unsupported fields")
    if value["decision"] not in {
        "spawn_hypothesis", "reopen_branch", "no_change", "needs_clarification"
    }:
        raise InvalidInputError("invalid steering decision")
    if not isinstance(value["explanation"], str) or not 1 <= len(value["explanation"]) <= 2000:
        raise InvalidInputError("steering explanation must be bounded")
    for field in ("branch_id", "hypothesis"):
        if value[field] is not None and (
            not isinstance(value[field], str) or not 1 <= len(value[field]) <= 2000
        ):
            raise InvalidInputError("steering branch and hypothesis must be bounded")
    return value


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
                 token_budget: int = 40000,
                 max_rounds: int = 1,
                 state_store: PostgresReasoningStore | None = None,
                 steering_inbox: Any | None = None) -> None:
        if type(max_hypotheses) is not int or not 1 <= max_hypotheses <= 12:
            raise InvalidInputError("hypothesis budget is invalid")
        if type(max_tool_calls) is not int or not 0 <= max_tool_calls <= 64:
            raise InvalidInputError("tool-call budget is invalid")
        if type(token_budget) is not int or not 1000 <= token_budget <= 200000:
            raise InvalidInputError("token budget is invalid")
        if type(max_rounds) is not int or not 1 <= max_rounds <= 4:
            raise InvalidInputError("reasoning expansion rounds must be 1-4")
        self.max_rounds = max_rounds
        self.state_store = state_store
        self.root = Path(root)
        self.gateway = gateway or EvidenceGateway()
        self.max_hypotheses = max_hypotheses
        self.max_tool_calls = max_tool_calls
        self.token_budget = token_budget
        self.steering_inbox = steering_inbox or SteeringInbox(self.root)

    def run(self, *, context: Any, run_id: str, task: str, system: str,
            models: list[dict[str, Any]], options: dict[str, Any],
            invoke: InferenceCallback, on_text: Callable[[str], None],
            emit: Callable[..., None], cancelled: Callable[[], bool] | None = None,
            ) -> InvestigationResult:
        """Close a failed interactive run rather than accepting orphan steering."""
        try:
            return self._run_impl(context=context, run_id=run_id, task=task,
                                  system=system, models=models, options=options,
                                  invoke=invoke, on_text=on_text, emit=emit,
                                  cancelled=cancelled)
        except Exception:
            if isinstance(run_id, str) and re.fullmatch(r"[a-f0-9]{32}", run_id):
                workspace = getattr(context, "workspace_id", None)
                if isinstance(workspace, UUID):
                    snapshot = self.root / str(workspace) / (run_id + ".json")
                    if snapshot.is_file() and not snapshot.is_symlink():
                        try:
                            tree = ReasoningTree.load(snapshot)
                            if tree.run_status == "running":
                                tree.close("failed")
                        except (OSError, ValueError, InvalidInputError):
                            # Never mask the original, possibly charged,
                            # provider execution failure with recovery I/O.
                            pass
            raise

    def _run_impl(self, *, context: Any, run_id: str, task: str, system: str,
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
        lease_token = (self.state_store.start(context, run_id)
                       if self.state_store is not None else None)
        checkpoint = (
            (lambda data: self.state_store.checkpoint(context, run_id, lease_token, data))
            if self.state_store is not None and lease_token is not None else None
        )
        tree = ReasoningTree(tree_path, max_nodes=self.max_hypotheses * 3 + 24,
                             max_depth=max(3, self.max_rounds + 2),
                             token_budget=self.token_budget, checkpoint=checkpoint)
        tree.save()  # Make the run discoverable by the steering API immediately.
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

        def model_call(model: dict[str, Any], role: ReasoningRole, payload: dict[str, Any],
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
            claim = tree.claim(node.id)
            if claim is None or claim.claim_token is None:
                raise InvalidInputError("reasoning token reservation unavailable")
            emit("reasoning.node.started", run_id=run_id, node_id=node.id, role=role,
                 model=model["model"], prompt_hash=packet.identity)
            selection_options = {**options, "max_output_tokens": output_limit}
            # OpenAI and Gemini expose native JSON-only generation constraints;
            # other providers still receive the canonical JSON prompt contract.
            if role != "synthesizer" and model["provider"] in {"openai", "gemini"}:
                selection_options["response_format"] = "json_object"
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

        def steering_actor() -> Any:
            return context if getattr(self.steering_inbox, "uses_context", False) else context.workspace_id

        def drain_steering() -> bool:
            """Single-owner checkpoint: evaluate and respond to every queued input."""
            changed = False
            for request in self.steering_inbox.pending(steering_actor(), run_id):
                stopped()
                emit("reasoning.steering.evaluating", run_id=run_id,
                     prompt_id=request["id"], task_revision=tree.revision)
                proposals = [{"id": n.id, "hypothesis": n.hypothesis,
                              "state": n.state, "rejection_reason": n.rejection_reason,
                              "assessment": n.assessment}
                             for n in tree.nodes.values() if n.role == "investigator"]
                raw = model_call(generators[0], "steering_evaluator", {
                    "steering_prompt": request["prompt"], "task_revision": tree.revision,
                    "current_hypotheses": proposals,
                    "allowed_actions": ["spawn_hypothesis", "reopen_branch",
                                        "no_change", "needs_clarification"],
                }, None)
                candidate = parse_steering(raw)
                decision = candidate["decision"]
                branch_id = candidate["branch_id"]
                hypothesis = candidate["hypothesis"]
                # The model only proposes. Validate branch identity, capacity
                # and state before accepting any task-understanding revision.
                if decision == "reopen_branch" and (
                    branch_id not in tree.nodes
                    or tree.nodes[branch_id].role != "investigator"
                    or tree.nodes[branch_id].state not in {"rejected", "pruned"}
                ):
                    decision = "needs_clarification"
                    candidate["explanation"] = (
                        "The suggested branch is not a previously rejected investigation; "
                        "no branch was reopened."
                    )
                if decision == "spawn_hypothesis" and (
                    not hypothesis or len(tree.nodes) >= tree.max_nodes
                ):
                    decision = "needs_clarification"
                    candidate["explanation"] = "A valid new hypothesis or tree capacity is required."
                if decision == "spawn_hypothesis" and hypothesis:
                    if " ".join(hypothesis.casefold().split()) in seen:
                        decision = "no_change"
                        candidate["explanation"] = "This hypothesis is already under investigation."
                before = set(tree.nodes)
                item = tree.steer(request["prompt"], decision=decision,
                                  explanation=candidate["explanation"],
                                  branch_id=branch_id if decision == "reopen_branch" else None,
                                  hypothesis=hypothesis if decision == "spawn_hypothesis" else None,
                                  request_id=request["id"])
                if decision == "spawn_hypothesis" and hypothesis:
                    created = (set(tree.nodes) - before).pop()
                    tree.nodes[created].role = "investigator"
                    tree.save()
                    hypotheses.append((Hypothesis(hypothesis, (), ()), "user-steering"))
                    steered_nodes[hypothesis] = created
                    seen.add(" ".join(hypothesis.casefold().split()))
                    changed = True
                elif decision == "reopen_branch" and branch_id:
                    old = tree.nodes[branch_id]
                    # Do not replay a previously authorized external tool.
                    # Retain prior evidence for a fresh critic evaluation.
                    old_result = old.result or ""
                    old_receipts = tuple(old.evidence)
                    claim = tree.claim(branch_id)
                    if claim is None or claim.claim_token is None:
                        raise InvalidInputError("reopened branch cannot be claimed")
                    tree.finish(branch_id, claim.claim_token, old_result,
                                tokens=0, evidence=old_receipts)
                    changed = True
                response = self.steering_inbox.resolve(steering_actor(), run_id,
                    request, {"decision": decision, "explanation": candidate["explanation"],
                              "branch_id": branch_id, "hypothesis": hypothesis,
                              "task_revision": item["revision"]})
                emit("reasoning.steering.responded", run_id=run_id,
                     prompt_id=response["id"], decision=decision,
                     task_revision=tree.revision)
            return changed

        steered_nodes: dict[str, str] = {}
        lineage: dict[str, str] = {}
        for model in generators:
            drain_steering()
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
                # Reserve capacity for an adaptive R6 expansion after R5 critique.
                initial_cap = (max(2, self.max_hypotheses - 2)
                               if self.max_rounds > 1 else self.max_hypotheses)
                if len(hypotheses) >= initial_cap:
                    break
                hypotheses.append((item, f"{model['provider']}/{model['model']}"))

        drain_steering()
        investigation: list[dict[str, Any]] = []
        tool_calls = 0
        per_run_cache: dict[tuple[str, str], tuple[EvidenceFinding, ...]] = {}
        configured_channels = {t["channel"] for t in self.gateway.available()}
        investigated: set[str] = set()
        cursor = 0
        def investigate_pending() -> None:
            nonlocal cursor, tool_calls
            while cursor < len(hypotheses):
                drain_steering()
                item, model_id = hypotheses[cursor]
                cursor += 1
                if item.hypothesis in investigated:
                    continue
                investigated.add(item.hypothesis)
                stopped()
                if item.hypothesis in steered_nodes:
                    node = tree.nodes[steered_nodes[item.hypothesis]]
                else:
                    node = tree.add(item.hypothesis, parent_id=lineage.get(item.hypothesis),
                                    model_id=model_id, max_tokens=1, role="investigator")
                claim = tree.claim(node.id)
                if claim is None or claim.claim_token is None:
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
        investigate_pending()
        emit("reasoning.investigation.complete", run_id=run_id,
             branches=len(investigation), tool_calls=tool_calls,
             missing_channels=sorted(_CHANNELS - configured_channels))

        def evaluate() -> list[dict[str, Any]]:
            if not investigation:
                raise InvalidInputError("no evidence branches to evaluate")
            # Independent provider where available, otherwise a distinct model.
            critic = next((model for model in preferred
                           if model["provider"] != generators[0]["provider"]),
                          generators[-1])
            raw = model_call(critic, "critic", {
                "branches": investigation, "task_revision": tree.revision,
                "rule": "Evidence requires source_sha256 and admitted tool receipt."
            }, None)
            eligible = [branch for branch in investigation
                        if tree.nodes[branch["node_id"]].state == "completed"]
            critiques = parse_critiques(raw, {b["node_id"] for b in investigation})
            ranked = rank_branches(eligible, critiques, beam_width=2)
            for branch in ranked:
                node = tree.nodes[branch["node_id"]]
                if node.state == "pruned":
                    # Historical pruning remains visible; steering may reopen.
                    continue
                if node.state == "completed":
                    tree.assess(node.id, {"score": branch["score"],
                                          "evidence_count": branch["evidence_count"],
                                          "critique": branch["critique"]})
                    if not branch["selected"]:
                        tree.prune(node.id, "outside R5 evidence-weighted beam")
            emit("reasoning.branches.ranked", run_id=run_id,
                 ranked=[{"node_id": item["node_id"], "score": item["score"],
                          "selected": item["selected"]} for item in ranked])
            return [item for item in ranked if item["selected"]]

        drain_steering()
        investigate_pending()
        selected = evaluate()
        changed_after_critique = drain_steering()
        investigate_pending()
        # Steering arriving while the critic executes triggers an additional
        # evaluation at the next checkpoint. Reopened branches retain receipts.
        if changed_after_critique:
            selected = evaluate()
        # R6: bounded evidence-guided recursive expansion. The critic selects
        # which branches deserve another look; the generator may only propose
        # typed hypotheses, never authorize an external Cog invocation.
        for round_number in range(1, self.max_rounds):
            stopped()
            if len(hypotheses) >= self.max_hypotheses or not selected:
                break
            added = 0
            available = self.max_hypotheses - len(hypotheses)
            for source in selected[:2]:
                if added >= available:
                    break
                parent = source["node_id"]
                # Depth and cost gates are checked before creating new nodes.
                if tree.depth(parent) >= tree.max_depth - 1:
                    continue
                model = preferred[(round_number + added) % len(preferred)]
                raw = model_call(model, "hypothesis_generator", {
                    "round": round_number,
                    "focus_branch": source,
                    "instruction": "Propose falsifiable refinements or alternatives to this branch, not repetitions.",
                    "existing_hypotheses": list(seen)[:self.max_hypotheses],
                    "task_revision": tree.revision,
                }, parent)
                for hypothesis in parse_hypotheses(raw):
                    normalized = " ".join(hypothesis.hypothesis.casefold().split())
                    if normalized in seen or added >= available:
                        continue
                    seen.add(normalized)
                    hypotheses.append((hypothesis, f"{model['provider']}/{model['model']}"))
                    lineage[hypothesis.hypothesis] = parent
                    added += 1
            if not added:
                break
            emit("reasoning.branches.expanded", run_id=run_id,
                 round=round_number, created=added)
            investigate_pending()
            selected = evaluate()
            if drain_steering():
                investigate_pending()
                selected = evaluate()
        synthesis_model = generators[-1]
        last_revision = tree.revision
        answer = model_call(synthesis_model, "synthesizer", {
            "branches": selected,
            "available_evidence_tools": list(self.gateway.available()),
            "verified_scope": "Only findings with source digests and execution receipts are tool evidence.",
        }, None)
        self.steering_inbox.seal(steering_actor(), run_id)
        if drain_steering() and tree.revision != last_revision:
            investigate_pending()
            # A user message arriving during synthesis must influence the
            # final answer, rather than being silently lost on completion.
            selected = evaluate()
            answer = model_call(synthesis_model, "synthesizer", {
                "branches": selected, "task_revision": tree.revision,
                "verified_scope": "Only independently receipted sources count as evidence.",
            }, None)
        tree.close("completed")
        # Buffer synthesis until steering has been addressed; streamed deltas
        # cannot be retracted after the task-understanding revision changes.
        for offset in range(0, len(answer), 512):
            on_text(answer[offset:offset + 512])
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
