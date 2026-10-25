"""Advisory atomic-task vocabulary and bounded recursive graph construction.

Models may propose decomposition, but only controller-registered operations may
be admitted as executable leaves. This module performs no I/O or tool dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from potbot.domain.errors import InvalidInputError

ExecutionClass = Literal["deterministic", "admitted_cog", "model_classifier", "reasoning"]


@dataclass(frozen=True, slots=True)
class AtomicKind:
    name: str
    category: str
    executor: ExecutionClass
    verifiable_by: str
    notes: str = ""


# Vocabulary of proposed leaf-task *types*, not automatically installed Cogs.
# A leaf is executable only with an exact registered operation and permission.
ATOMIC_KINDS: tuple[AtomicKind, ...] = (
    AtomicKind("parse_json", "input", "deterministic", "json_schema"),
    AtomicKind("extract_field", "input", "deterministic", "exact_field"),
    AtomicKind("classify_steering", "steering", "model_classifier", "human_labels"),
    AtomicKind("detect_urgent_stop", "steering", "deterministic", "interrupt_rules"),
    AtomicKind("classify_task_intent", "routing", "model_classifier", "human_labels"),
    AtomicKind("classify_task_priority", "routing", "model_classifier", "human_labels"),
    AtomicKind("classify_atomicity", "planning", "model_classifier", "independent_review"),
    AtomicKind("select_known_operation", "planning", "model_classifier", "tool_catalogue"),
    AtomicKind("identify_missing_input", "planning", "model_classifier", "acceptance_rubric"),
    AtomicKind("identify_dependency", "planning", "model_classifier", "dag_constraints"),
    AtomicKind("check_dependency_status", "scheduling", "deterministic", "task_ledger"),
    AtomicKind("check_job_status", "scheduling", "deterministic", "job_ledger"),
    AtomicKind("check_budget", "scheduling", "deterministic", "budget_ledger"),
    AtomicKind("check_permissions", "scheduling", "deterministic", "access_policy"),
    AtomicKind("choose_executor", "routing", "model_classifier", "model_benchmark"),
    AtomicKind("lookup_cached_result", "execution", "deterministic", "cache_digest"),
    AtomicKind("read_artifact_range", "execution", "admitted_cog", "artifact_receipt"),
    AtomicKind("run_registered_tool", "execution", "admitted_cog", "tool_receipt"),
    AtomicKind("run_test_case", "verification", "admitted_cog", "test_receipt"),
    AtomicKind("check_exit_status", "verification", "deterministic", "process_receipt"),
    AtomicKind("classify_failure", "verification", "model_classifier", "known_failure_labels"),
    AtomicKind("check_schema", "verification", "deterministic", "schema_validator"),
    AtomicKind("check_hash", "verification", "deterministic", "sha256"),
    AtomicKind("check_test_coverage", "verification", "model_classifier", "independent_tests"),
    AtomicKind("inspect_diff", "coding", "model_classifier", "structured_diff_rubric"),
    AtomicKind("propose_code_patch", "coding", "reasoning", "isolated_test_suite"),
    AtomicKind("review_patch", "coding", "reasoning", "admitted_checks"),
    AtomicKind("rank_source_relevance", "evidence", "model_classifier", "retrieval_labels"),
    AtomicKind("deduplicate_evidence", "evidence", "deterministic", "source_fingerprints"),
    AtomicKind("classify_evidence_type", "evidence", "model_classifier", "labelled_sources"),
    AtomicKind("check_staleness", "evidence", "deterministic", "source_time"),
    AtomicKind("classify_hypothesis", "reasoning", "model_classifier", "gold_labels"),
    AtomicKind("check_assumption", "reasoning", "model_classifier", "external_evidence"),
    AtomicKind("compare_two_answers", "evaluation", "model_classifier", "blind_pairwise_gold"),
    AtomicKind("judge_result_quality", "evaluation", "reasoning", "independent_rubric"),
    AtomicKind("detect_contradiction", "evaluation", "model_classifier", "entailment_gold"),
    AtomicKind("verify_workflow_output", "evaluation", "admitted_cog", "workflow_receipt"),
    AtomicKind("record_health_event", "monitoring", "deterministic", "signed_record"),
    AtomicKind("detect_performance_regression", "monitoring", "deterministic", "paired_benchmarks"),
    AtomicKind("suggest_prompt_variant", "monitoring", "model_classifier", "held_out_metric"),
    AtomicKind("request_human_approval", "steering", "deterministic", "approval_receipt"),
)


@dataclass(frozen=True, slots=True)
class TaskNode:
    task_id: str
    description: str
    parent_id: str | None
    depth: int
    operation: str | None = None
    depends_on: tuple[str, ...] = ()
    state: Literal["needs_decomposition", "ready", "blocked"] = "needs_decomposition"


@dataclass(frozen=True, slots=True)
class AtomicOperation:
    """Exactly one host-registered, bounded and verifiable operation."""

    name: str
    category: str
    max_input_bytes: int
    max_runtime_ms: int
    requires_approval: bool = False

    def __post_init__(self) -> None:
        if (
            not self.name or self.name not in {x.name for x in ATOMIC_KINDS}
            or not isinstance(self.category, str) or not self.category
            or type(self.max_input_bytes) is not int or not 1 <= self.max_input_bytes <= 16_777_216
            or type(self.max_runtime_ms) is not int or not 1 <= self.max_runtime_ms <= 3_600_000
            or type(self.requires_approval) is not bool
        ):
            raise InvalidInputError("atomic operations require an exact bounded host contract")


class TaskGraph:
    """A validated decomposition DAG; dispatch is performed by another controller."""

    def __init__(self, objective: str, *, max_depth: int = 8, max_nodes: int = 128) -> None:
        if not objective or len(objective.encode("utf-8")) > 32768:
            raise InvalidInputError("objective must be bounded text")
        if type(max_depth) is not int or not 1 <= max_depth <= 16:
            raise InvalidInputError("invalid maximum decomposition depth")
        if type(max_nodes) is not int or not 2 <= max_nodes <= 1024:
            raise InvalidInputError("invalid maximum graph size")
        self.max_depth, self.max_nodes = max_depth, max_nodes
        self.nodes: dict[str, TaskNode] = {"root": TaskNode("root", objective, None, 0)}

    def decompose(
        self,
        task_id: str,
        children: tuple[tuple[str, str, tuple[str, ...]], ...],
    ) -> None:
        """Replace one unplanned node with multiple smaller owned sub-tasks.

        Dependencies refer only to earlier known nodes or siblings in this
        proposal. An exact leaf operation is never recursively decomposed.
        """
        parent = self.nodes.get(task_id)
        if parent is None or parent.state != "needs_decomposition" or parent.operation:
            raise InvalidInputError("parent is unknown or already an atomic leaf")
        if parent.depth >= self.max_depth or not 2 <= len(children) <= 16:
            raise InvalidInputError("decomposition reached its depth or branching limit")
        if len(self.nodes) + len(children) > self.max_nodes:
            raise InvalidInputError("graph node bound exceeded")
        known = set(self.nodes)
        proposed: dict[str, TaskNode] = {}
        for name, description, dependencies in children:
            if (
                not isinstance(name, str) or not name or not name.isascii()
                or len(name) > 64 or not name.replace("_", "").replace("-", "").isalnum()
                or name in known or name in proposed
                or not isinstance(description, str) or not description.strip()
                or len(description.encode("utf-8")) > 4096
                or not isinstance(dependencies, tuple) or len(dependencies) > 16
            ):
                raise InvalidInputError("invalid or duplicate decomposition node")
            proposed[name] = TaskNode(name, description, parent.task_id, parent.depth + 1,
                                      depends_on=dependencies)
        allowed = known | set(proposed)
        for node in proposed.values():
            if node.task_id in node.depends_on or len(set(node.depends_on)) != len(node.depends_on):
                raise InvalidInputError("cyclic or duplicate dependencies")
            if not set(node.depends_on) <= allowed:
                raise InvalidInputError("unresolved task dependency")
        # Detect cycles across old and newly proposed nodes, transactionally.
        combined = {**self.nodes, **proposed}
        visiting: set[str] = set()
        visited: set[str] = set()
        def walk(current: str) -> None:
            if current in visiting:
                raise InvalidInputError("cyclic execution graph")
            if current in visited:
                return
            visiting.add(current)
            for dep in combined[current].depends_on:
                walk(dep)
            visiting.remove(current)
            visited.add(current)
        for name in proposed:
            walk(name)
        self.nodes.update(proposed)
        # Parent remains a decomposition record; it is not an executable leaf.
        self.nodes[task_id] = TaskNode(
            parent.task_id, parent.description, parent.parent_id, parent.depth,
            state="blocked",
        )

    def admit_leaf(self, task_id: str, *, operation: AtomicOperation,
                   estimated_input_bytes: int, approved: bool = False) -> None:
        """Only exact registered operations can stop recursive decomposition."""
        node = self.nodes.get(task_id)
        if node is None or node.state != "needs_decomposition" or node.operation:
            raise InvalidInputError("only an unresolved leaf can be made atomic")
        if (
            type(estimated_input_bytes) is not int or not 0 <= estimated_input_bytes <= operation.max_input_bytes
            or (operation.requires_approval and approved is not True)
        ):
            raise InvalidInputError("leaf violates resource or approval policy")
        self.nodes[task_id] = TaskNode(node.task_id, node.description, node.parent_id, node.depth,
                                       operation.name, node.depends_on, "ready")

    def unresolved(self) -> tuple[str, ...]:
        return tuple(x.task_id for x in self.nodes.values() if x.state == "needs_decomposition")

    def executable(self, completed: set[str]) -> tuple[str, ...]:
        return tuple(x.task_id for x in self.nodes.values()
                     if x.state == "ready" and x.task_id not in completed
                     and set(x.depends_on) <= completed)
