"""Phase 6 controller policy and deterministic graph identities.

Pure contracts: they do not authorize tool/model invocations, infer semantic truth,
or grant access to information from other workspaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from potbot.domain.errors import InvalidInputError, RevisionConflictError
from potbot.domain.serialization import content_hash


class NodeKind(StrEnum):
    ROOT = "root"
    PROPOSAL = "proposal"
    INFORMATION_NEED = "information_need"
    OBSERVATION = "observation"
    DECISION = "decision"
    CANDIDATE = "candidate"
    VERIFICATION = "verification"


class EdgeKind(StrEnum):
    DERIVED_FROM = "derived_from"
    REQUIRES = "requires"
    OBSERVES = "observes"
    CONTINUES = "continues"
    IMPORTS = "imports"
    VERIFIES = "verifies"


@dataclass(frozen=True, slots=True)
class SolvePolicy:
    """Immutable, budgeted run configuration; selectors are not qualifications."""

    workspace_id: UUID
    task_id: UUID
    contract_revision_id: UUID
    base_state_id: UUID
    semantic_release_digest: str
    reasoning_models: tuple[str, ...]
    coding_models: tuple[str, ...]
    max_depth: int = 8
    max_nodes: int = 64
    max_repairs_per_branch: int = 2
    max_model_calls: int = 32
    max_tool_calls: int = 64
    max_output_tokens: int = 65536
    max_money_microunits: int = 5000000
    schema_version: int = 1

    def __post_init__(self) -> None:
        for field in ("workspace_id", "task_id", "contract_revision_id", "base_state_id"):
            if not isinstance(getattr(self, field), UUID):
                raise InvalidInputError(f"{field} requires a UUID")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise InvalidInputError("unsupported solve policy schema")
        if (
            not isinstance(self.semantic_release_digest, str)
            or len(self.semantic_release_digest) != 64
            or any(c not in "0123456789abcdef" for c in self.semantic_release_digest)
        ):
            raise InvalidInputError("semantic release requires a lowercase SHA-256 digest")
        for field in ("reasoning_models", "coding_models"):
            models = getattr(self, field)
            if (
                not isinstance(models, tuple)
                or not models
                or len(models) > 16
                or any(not isinstance(m, str) or not m.strip() or len(m) > 256 for m in models)
                or len(set(models)) != len(models)
            ):
                raise InvalidInputError(f"{field} must contain distinct nonempty model selectors")
        limits = {
            "max_depth": (1, 32),
            "max_nodes": (1, 1024),
            "max_repairs_per_branch": (0, 8),
            "max_model_calls": (1, 1024),
            "max_tool_calls": (0, 4096),
            "max_output_tokens": (1, 10000000),
            "max_money_microunits": (0, 10**12),
        }
        for field, (low, high) in limits.items():
            number = getattr(self, field)
            if type(number) is not int or not low <= number <= high:
                raise InvalidInputError(f"{field} must be within [{low}, {high}]")
        if self.max_nodes < self.max_depth:
            raise InvalidInputError("graph node allowance must cover the maximum depth")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            **{
                name: str(getattr(self, name))
                for name in ("workspace_id", "task_id", "contract_revision_id", "base_state_id")
            },
            "semantic_release_digest": self.semantic_release_digest,
            "reasoning_models": list(self.reasoning_models),
            "coding_models": list(self.coding_models),
            **{
                name: getattr(self, name)
                for name in (
                    "max_depth",
                    "max_nodes",
                    "max_repairs_per_branch",
                    "max_model_calls",
                    "max_tool_calls",
                    "max_output_tokens",
                    "max_money_microunits",
                )
            },
        }

    @property
    def digest(self) -> str:
        return content_hash(self.to_dict())


@dataclass(frozen=True, slots=True)
class KnowledgeView:
    """A pinned scope and explicit evidence IDs. No inference from model agreement."""

    workspace_id: UUID
    task_id: UUID
    contract_revision_id: UUID
    repository_state_id: UUID
    semantic_release_digest: str
    evidence_digests: tuple[str, ...]
    source_snapshot_digests: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field in ("workspace_id", "task_id", "contract_revision_id", "repository_state_id"):
            if not isinstance(getattr(self, field), UUID):
                raise InvalidInputError(f"{field} requires a UUID")
        for field in (
            self.semantic_release_digest,
            *self.evidence_digests,
            *self.source_snapshot_digests,
        ):
            if (
                not isinstance(field, str)
                or len(field) != 64
                or any(c not in "0123456789abcdef" for c in field)
            ):
                raise InvalidInputError("knowledge identity requires SHA-256 digests")
        for field in ("evidence_digests", "source_snapshot_digests"):
            items = getattr(self, field)
            if not isinstance(items, tuple) or len(items) > 1000 or len(set(items)) != len(items):
                raise InvalidInputError(f"{field} must be a unique bounded tuple")

    @property
    def digest(self) -> str:
        return content_hash(
            {
                "workspace": str(self.workspace_id),
                "task": str(self.task_id),
                "revision": str(self.contract_revision_id),
                "state": str(self.repository_state_id),
                "semantic_release": self.semantic_release_digest,
                "evidence": sorted(self.evidence_digests),
                "sources": sorted(self.source_snapshot_digests),
            }
        )


@dataclass(frozen=True, slots=True)
class GraphNode:
    id: UUID
    kind: NodeKind
    view: KnowledgeView
    producer: str
    payload_digest: str
    depth: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.id, UUID)
            or not isinstance(self.kind, NodeKind)
            or not isinstance(self.view, KnowledgeView)
        ):
            raise InvalidInputError("invalid graph node identity or kind")
        if (
            not isinstance(self.producer, str)
            or not self.producer.strip()
            or len(self.producer) > 256
        ):
            raise InvalidInputError("graph node requires a bounded producer")
        if (
            not isinstance(self.payload_digest, str)
            or len(self.payload_digest) != 64
            or any(c not in "0123456789abcdef" for c in self.payload_digest)
        ):
            raise InvalidInputError("graph node requires a payload digest")
        if type(self.depth) is not int or not 0 <= self.depth <= 32:
            raise InvalidInputError("invalid graph depth")

    @property
    def identity(self) -> str:
        return content_hash(
            {
                "kind": self.kind.value,
                "knowledge_view": self.view.digest,
                "payload": self.payload_digest,
                "depth": self.depth,
            }
        )


@dataclass(frozen=True, slots=True)
class GraphEdge:
    source: UUID
    target: UUID
    kind: EdgeKind

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source, UUID)
            or not isinstance(self.target, UUID)
            or self.source == self.target
            or not isinstance(self.kind, EdgeKind)
        ):
            raise InvalidInputError("invalid graph edge")


class GraphProjection:
    """Pure deterministic admission for a single pinned run; persistence/fencing later."""

    def __init__(self, policy: SolvePolicy):
        self.policy = policy
        self.nodes: dict[UUID, GraphNode] = {}
        self.edges: set[GraphEdge] = set()
        self.identities: dict[str, UUID] = {}

    def add_node(self, node: GraphNode) -> UUID:
        view = node.view
        p = self.policy
        if (
            view.workspace_id,
            view.task_id,
            view.contract_revision_id,
            view.semantic_release_digest,
        ) != (
            p.workspace_id,
            p.task_id,
            p.contract_revision_id,
            p.semantic_release_digest,
        ):
            raise RevisionConflictError("node belongs to another run revision or semantic release")
        if node.depth > p.max_depth:
            raise InvalidInputError("node exceeds run depth")
        existing = self.identities.get(node.identity)
        if existing is not None:
            return existing
        if node.id in self.nodes or len(self.nodes) >= p.max_nodes:
            raise InvalidInputError("duplicate node ID or exhausted graph allowance")
        self.nodes[node.id] = node
        self.identities[node.identity] = node.id
        return node.id

    def add_edge(self, edge: GraphEdge) -> None:
        if edge.source not in self.nodes or edge.target not in self.nodes:
            raise InvalidInputError("graph edge references an unknown node")
        if edge in self.edges:
            return
        if self.nodes[edge.source].view.repository_state_id != self.nodes[
            edge.target
        ].view.repository_state_id and edge.kind not in (EdgeKind.CONTINUES, EdgeKind.VERIFIES):
            raise InvalidInputError(
                "cross-state edges require explicit continuation or verification"
            )
        adjacency: dict[UUID, set[UUID]] = {}
        for existing in self.edges:
            adjacency.setdefault(existing.source, set()).add(existing.target)
        pending = [edge.target]
        visited: set[UUID] = set()
        while pending:
            item = pending.pop()
            if item == edge.source:
                raise InvalidInputError("graph edges cannot introduce a cycle")
            if item not in visited:
                visited.add(item)
                pending.extend(adjacency.get(item, ()))
        self.edges.add(edge)
