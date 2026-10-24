"""Bounded durable reasoning tree with revisioned steering and explicit claims.

This controller does not invoke a model. A qualified worker must execute a
claimed node and report its result with the matching claim token. Store the
state only inside a provisioned workspace's sandbox directory.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from potbot.domain.errors import InvalidInputError

NodeState = Literal["pending", "running", "completed", "rejected", "pruned", "indeterminate"]
SteeringDecision = Literal["spawn_hypothesis", "reopen_branch", "no_change", "needs_clarification"]


@dataclass
class ReasoningNode:
    id: str
    parent_id: str | None
    hypothesis: str
    revision: int
    state: NodeState = "pending"
    claim_token: str | None = None
    result: str | None = None
    evidence: list[str] = field(default_factory=list)
    rejection_reason: str | None = None
    model_id: str | None = None
    max_tokens: int = 1024


class ReasoningTree:
    """Single-writer snapshot controller; never run concurrent writers on one path.

    The atomic replace protects against torn writes, but this is not a
    distributed lease or PostgreSQL transaction manager. Running nodes become
    indeterminate on recovery, never silently retried.
    """

    def __init__(self, path: Path, *, max_nodes: int = 32, max_depth: int = 5,
                 token_budget: int = 20000) -> None:
        if max_nodes < 1 or max_depth < 1 or token_budget < 1:
            raise InvalidInputError("reasoning limits must be positive")
        self.path = Path(path)
        self.max_nodes = max_nodes
        self.max_depth = max_depth
        self.token_budget = token_budget
        self.spent_tokens = 0
        self.revision = 0
        self.nodes: dict[str, ReasoningNode] = {}
        self.steering: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []

    @classmethod
    def load(cls, path: Path) -> "ReasoningTree":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        tree = cls(path, max_nodes=data["max_nodes"], max_depth=data["max_depth"],
                   token_budget=data["token_budget"])
        tree.revision = data["revision"]
        tree.spent_tokens = data["spent_tokens"]
        tree.nodes = {n["id"]: ReasoningNode(**n) for n in data["nodes"]}
        tree.steering = data["steering"]
        tree.events = data["events"]
        for node in tree.nodes.values():
            if node.state == "running":
                node.state = "indeterminate"
                tree._event("node.indeterminate", node.id)
        tree.save()
        return tree

    def _event(self, kind: str, subject: str) -> None:
        self.events.append({"sequence": len(self.events) + 1, "type": kind,
                            "subject": subject, "revision": self.revision})

    def save(self) -> None:
        from dataclasses import asdict
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"max_nodes": self.max_nodes, "max_depth": self.max_depth,
                   "token_budget": self.token_budget, "spent_tokens": self.spent_tokens,
                   "revision": self.revision,
                   "nodes": [asdict(n) for n in self.nodes.values()],
                   "steering": self.steering, "events": self.events}
        fd, temporary = tempfile.mkstemp(prefix=".reasoning-", dir=self.path.parent)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def depth(self, parent_id: str | None) -> int:
        depth = 0
        seen: set[str] = set()
        while parent_id is not None:
            if parent_id in seen or parent_id not in self.nodes:
                raise InvalidInputError("invalid reasoning parent")
            seen.add(parent_id)
            depth += 1
            parent_id = self.nodes[parent_id].parent_id
        return depth

    def add(self, hypothesis: str, *, parent_id: str | None = None,
            model_id: str | None = None, max_tokens: int = 1024) -> ReasoningNode:
        if not hypothesis.strip() or len(hypothesis) > 10000:
            raise InvalidInputError("hypothesis is required and bounded")
        if len(self.nodes) >= self.max_nodes or self.depth(parent_id) >= self.max_depth:
            raise InvalidInputError("reasoning tree size or depth exhausted")
        if max_tokens < 1 or max_tokens > self.token_budget:
            raise InvalidInputError("invalid node budget")
        node = ReasoningNode(str(uuid4()), parent_id, hypothesis, self.revision,
                             model_id=model_id, max_tokens=max_tokens)
        self.nodes[node.id] = node
        self._event("node.created", node.id)
        self.save()
        return node

    def claim(self) -> ReasoningNode | None:
        # Reserve the full allowance; avoid overcommitting concurrent nodes.
        reserved = sum(n.max_tokens for n in self.nodes.values() if n.state == "running")
        for node in self.nodes.values():
            if node.state == "pending" and self.spent_tokens + reserved + node.max_tokens <= self.token_budget:
                node.state = "running"
                node.claim_token = str(uuid4())
                self._event("node.started", node.id)
                self.save()
                return node
        return None

    def finish(self, node_id: str, claim_token: str, result: str, *, tokens: int,
               evidence: tuple[str, ...] = ()) -> None:
        node = self.nodes[node_id]
        if node.state != "running" or node.claim_token != claim_token:
            raise InvalidInputError("stale or invalid reasoning claim")
        if not 0 <= tokens <= node.max_tokens:
            raise InvalidInputError("invalid measured usage; reconcile externally")
        self.spent_tokens += tokens
        node.state = "completed" if node.revision == self.revision else "indeterminate"
        node.result = result
        node.evidence = list(evidence)
        self._event("node.completed" if node.state == "completed" else "node.stale", node.id)
        self.save()

    def reject(self, node_id: str, reason: str) -> None:
        node = self.nodes[node_id]
        if node.state not in {"pending", "completed"} or not reason.strip():
            raise InvalidInputError("invalid rejection")
        node.state, node.rejection_reason = "rejected", reason
        self._event("node.rejected", node_id)
        self.save()

    def steer(self, prompt: str, *, decision: SteeringDecision,
              explanation: str, branch_id: str | None = None,
              hypothesis: str | None = None, request_id: str | None = None) -> dict[str, Any]:
        if not prompt.strip() or not explanation.strip():
            raise InvalidInputError("steering prompt and explanation are required")
        if decision not in {"spawn_hypothesis", "reopen_branch", "no_change", "needs_clarification"}:
            raise InvalidInputError("unknown steering decision")
        if request_id:
            for item in self.steering:
                if item["request_id"] == request_id:
                    if item["prompt"] != prompt:
                        raise InvalidInputError("steering idempotency conflict")
                    return item
        if decision == "spawn_hypothesis" and (not hypothesis or len(self.nodes) >= self.max_nodes):
            raise InvalidInputError("new hypothesis required and capacity available")
        if decision == "reopen_branch" and (branch_id not in self.nodes or
             self.nodes[branch_id].state not in {"rejected", "pruned"}):
            raise InvalidInputError("only rejected or pruned branches may reopen")
        if decision in {"spawn_hypothesis", "reopen_branch"}:
            self.revision += 1
        if decision == "spawn_hypothesis":
            self.add(hypothesis or "", parent_id=branch_id,
                     max_tokens=min(1024, self.token_budget))
        elif decision == "reopen_branch":
            old = self.nodes[branch_id]
            old.state = "pending"
            old.revision = self.revision
            old.claim_token = None
            self._event("branch.reopened", branch_id)
        item = {"id": str(uuid4()), "request_id": request_id, "prompt": prompt,
                "decision": decision, "explanation": explanation,
                "branch_id": branch_id, "revision": self.revision}
        self.steering.append(item)
        self._event("steering.responded", item["id"])
        self.save()
        return item
