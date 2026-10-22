"""Phase 6 search controller: immutable graph and fenced mutable frontier.

All mutations use an authenticated workspace PostgreSQL transaction, lock the
run before its subordinate rows, and bind replay to the request digest. A lost
external effect is quarantined, not replayed by the frontier worker.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from potbot.controller.tasks import json_record
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.search_graph import (
    EdgeKind,
    GraphEdge,
    GraphNode,
    GraphProjection,
    KnowledgeView,
    NodeKind,
    SolvePolicy,
)
from potbot.domain.serialization import content_hash
from potbot.persistence.database import Database, idempotent_command


def _view_dict(view: KnowledgeView) -> dict[str, Any]:
    return {
        "workspace_id": str(view.workspace_id),
        "task_id": str(view.task_id),
        "contract_revision_id": str(view.contract_revision_id),
        "repository_state_id": str(view.repository_state_id),
        "semantic_release_digest": view.semantic_release_digest,
        "evidence_digests": list(view.evidence_digests),
        "source_snapshot_digests": list(view.source_snapshot_digests),
    }


def _view_from_dict(value: dict[str, Any]) -> KnowledgeView:
    return KnowledgeView(
        UUID(value["workspace_id"]), UUID(value["task_id"]),
        UUID(value["contract_revision_id"]), UUID(value["repository_state_id"]),
        value["semantic_release_digest"], tuple(value["evidence_digests"]),
        tuple(value["source_snapshot_digests"]),
    )


def _policy_from_dict(value: dict[str, Any]) -> SolvePolicy:
    return SolvePolicy(
        UUID(value["workspace_id"]), UUID(value["task_id"]),
        UUID(value["contract_revision_id"]), UUID(value["base_state_id"]),
        value["semantic_release_digest"], tuple(value["reasoning_models"]),
        tuple(value["coding_models"]),
        **{key: value[key] for key in (
            "max_depth", "max_nodes", "max_repairs_per_branch", "max_model_calls",
            "max_tool_calls", "max_output_tokens", "max_money_microunits",
        )},
        schema_version=value["schema_version"],
    )


def _run(conn: Any, context: WorkspaceContext, identity: UUID, *, lock: bool = False) -> dict[str, Any]:
    if not isinstance(identity, UUID):
        raise InvalidInputError("Solve run identity must be a UUID")
    row = conn.execute(
        "SELECT * FROM solve_runs WHERE workspace_id=%s AND id=%s" + (" FOR UPDATE" if lock else ""),
        (context.workspace_id, identity),
    ).fetchone()
    if row is None:
        raise ScopeDeniedError("The solve run is unavailable")
    return dict(row)


def _require_task_current(conn: Any, context: WorkspaceContext, run: dict[str, Any]) -> None:
    """Prevent changed or paused tasks from publishing new graph transitions."""
    episode = conn.execute(
        "SELECT active_contract_id,status FROM episodes WHERE workspace_id=%s AND id=%s FOR SHARE",
        (context.workspace_id, run["episode_id"]),
    ).fetchone()
    if (episode is None or episode["active_contract_id"] != run["contract_revision_id"]
            or episode["status"] != "running"):
        raise RevisionConflictError("Task revision or lifecycle changed during search")


def _view(conn: Any, context: WorkspaceContext, run_id: UUID, view_id: UUID) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM solve_views WHERE workspace_id=%s AND run_id=%s AND id=%s",
        (context.workspace_id, run_id, view_id),
    ).fetchone()
    if row is None:
        raise ScopeDeniedError("The branch knowledge view is unavailable")
    return dict(row)


def _node(conn: Any, context: WorkspaceContext, run_id: UUID, node_id: UUID) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM solve_nodes WHERE workspace_id=%s AND run_id=%s AND id=%s",
        (context.workspace_id, run_id, node_id),
    ).fetchone()
    if row is None:
        raise ScopeDeniedError("The graph node is unavailable")
    return dict(row)


def _evidence_checks(conn: Any, context: WorkspaceContext, policy: SolvePolicy, view: KnowledgeView) -> None:
    if (
        view.workspace_id, view.task_id, view.contract_revision_id, view.semantic_release_digest
    ) != (
        policy.workspace_id, policy.task_id, policy.contract_revision_id,
        policy.semantic_release_digest,
    ):
        raise RevisionConflictError("Knowledge belongs to a different task or release")
    state = conn.execute(
        "SELECT repository_id FROM repository_states WHERE workspace_id=%s AND id=%s",
        (context.workspace_id, view.repository_state_id),
    ).fetchone()
    base = conn.execute(
        "SELECT repository_id FROM repository_states WHERE workspace_id=%s AND id=%s",
        (context.workspace_id, policy.base_state_id),
    ).fetchone()
    if state is None or base is None or state["repository_id"] != base["repository_id"]:
        raise ScopeDeniedError("Knowledge state is not from the selected repository")
    if view.evidence_digests:
        rows = conn.execute(
            "SELECT DISTINCT content_digest FROM evidence_events "
            "WHERE workspace_id=%s AND episode_id=%s AND content_digest=ANY(%s)",
            (context.workspace_id, policy.task_id, list(view.evidence_digests)),
        ).fetchall()
        if {r["content_digest"] for r in rows} != set(view.evidence_digests):
            raise ScopeDeniedError("Knowledge includes unsupported evidence")
    if view.source_snapshot_digests:
        rows = conn.execute(
            "SELECT DISTINCT content_hash FROM embedding_sources "
            "WHERE workspace_id=%s AND repository_id=%s AND content_hash=ANY(%s)",
            (context.workspace_id, base["repository_id"], list(view.source_snapshot_digests)),
        ).fetchall()
        if {r["content_hash"] for r in rows} != set(view.source_snapshot_digests):
            raise ScopeDeniedError("Knowledge includes unknown source versions")


def _event(conn: Any, context: WorkspaceContext, run_id: UUID, node_id: UUID,
           generation: int, event_type: str, detail: dict[str, Any]) -> None:
    from psycopg.types.json import Jsonb
    conn.execute(
        "INSERT INTO solve_frontier_events(workspace_id,id,run_id,node_id,generation,"
        "event_type,actor_id,detail) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
        (context.workspace_id, uuid4(), run_id, node_id, generation, event_type,
         context.principal_id, Jsonb(detail)),
    )


@dataclass(frozen=True, slots=True)
class FrontierLease:
    run_id: UUID
    node_id: UUID
    owner_id: UUID
    generation: int
    deadline: datetime

    def __post_init__(self) -> None:
        if any(not isinstance(getattr(self, f), UUID) for f in ("run_id", "node_id", "owner_id")):
            raise InvalidInputError("Frontier lease requires UUID identities")
        if type(self.generation) is not int or self.generation < 1:
            raise InvalidInputError("Frontier lease requires a positive generation")
        if not isinstance(self.deadline, datetime) or self.deadline.tzinfo is None:
            raise InvalidInputError("Frontier lease requires a timezone-aware deadline")


class SolveStore:
    def __init__(self, database: Database):
        self.database = database

    def create(self, context: WorkspaceContext, policy: SolvePolicy,
               root_view: KnowledgeView, key: str) -> dict[str, Any]:
        """Publish immutable policy, initial knowledge and root atomically."""
        from psycopg.types.json import Jsonb
        if not isinstance(policy, SolvePolicy) or not isinstance(root_view, KnowledgeView):
            raise InvalidInputError("Run creation requires typed policy and knowledge")
        if context.workspace_id != policy.workspace_id or root_view.repository_state_id != policy.base_state_id:
            raise ScopeDeniedError("Run scope or initial state mismatch")
        request = {"policy": policy.to_dict(), "root_view": _view_dict(root_view)}
        with self.database.transaction(context) as conn:
            def create() -> dict[str, Any]:
                episode = conn.execute(
                    "SELECT active_contract_id,status,repository_id FROM episodes "
                    "WHERE workspace_id=%s AND id=%s FOR SHARE",
                    (context.workspace_id, policy.task_id),
                ).fetchone()
                repository = conn.execute(
                    "SELECT repository_id FROM repository_states WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, policy.base_state_id),
                ).fetchone()
                pin = conn.execute(
                    "SELECT r.content_hash FROM task_semantic_pins p "
                    "JOIN semantic_releases r ON r.workspace_id=p.workspace_id AND r.id=p.release_id "
                    "WHERE p.workspace_id=%s AND p.episode_id=%s",
                    (context.workspace_id, policy.task_id),
                ).fetchone()
                if (episode is None or episode["active_contract_id"] != policy.contract_revision_id
                        or episode["status"] != "running" or repository is None
                        or repository["repository_id"] != episode["repository_id"]
                        or pin is None or pin["content_hash"] != policy.semantic_release_digest):
                    raise StateMismatchError("Run requires a current task, repository and semantic pin")
                _evidence_checks(conn, context, policy, root_view)
                run_id, view_id, root_id = uuid4(), uuid4(), uuid4()
                conn.execute(
                    "INSERT INTO solve_runs(workspace_id,id,episode_id,contract_revision_id,"
                    "repository_state_id,semantic_release_digest,policy,policy_digest)"
                    " VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (context.workspace_id, run_id, policy.task_id, policy.contract_revision_id,
                     policy.base_state_id, policy.semantic_release_digest,
                     Jsonb(policy.to_dict()), policy.digest),
                )
                conn.execute(
                    "INSERT INTO solve_views(workspace_id,id,run_id,episode_id,repository_state_id,"
                    "content,view_digest) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (context.workspace_id, view_id, run_id, policy.task_id,
                     root_view.repository_state_id, Jsonb(_view_dict(root_view)), root_view.digest),
                )
                conn.execute(
                    "INSERT INTO solve_nodes(workspace_id,id,run_id,episode_id,view_id,kind,depth,"
                    "producer,payload_digest,identity_hash) VALUES(%s,%s,%s,%s,%s,'root',0,'controller',%s,%s)",
                    (context.workspace_id, root_id, run_id, policy.task_id, view_id,
                     policy.digest, GraphNode(root_id, NodeKind.ROOT, root_view,
                                              "controller", policy.digest, 0).identity),
                )
                conn.execute(
                    "INSERT INTO solve_frontier(workspace_id,run_id,node_id) VALUES(%s,%s,%s)",
                    (context.workspace_id, run_id, root_id),
                )
                _event(conn, context, run_id, root_id, 0, "created", {"policy_digest": policy.digest})
                return {"run_id": str(run_id), "root_node_id": str(root_id),
                        "root_view_id": str(view_id), "policy_digest": policy.digest}
            return idempotent_command(conn, context, "solve_create", key, request, create)

    def add_view(self, context: WorkspaceContext, run_id: UUID,
                 parent_view_id: UUID, view: KnowledgeView, key: str) -> dict[str, Any]:
        from psycopg.types.json import Jsonb
        if not isinstance(view, KnowledgeView):
            raise InvalidInputError("A typed knowledge view is required")
        request = {"run": str(run_id), "parent": str(parent_view_id), "view": _view_dict(view)}
        with self.database.transaction(context) as conn:
            def create() -> dict[str, Any]:
                record = _run(conn, context, run_id, lock=True)
                _require_task_current(conn, context, record)
                if record["status"] not in {"running", "ready"}:
                    raise StateMismatchError("The run cannot publish a new view")
                policy = _policy_from_dict(record["policy"])
                _view(conn, context, run_id, parent_view_id)
                _evidence_checks(conn, context, policy, view)
                existing = conn.execute(
                    "SELECT id FROM solve_views WHERE workspace_id=%s AND run_id=%s AND view_digest=%s",
                    (context.workspace_id, run_id, view.digest),
                ).fetchone()
                if existing:
                    return {"id": str(existing["id"]), "view_digest": view.digest}
                identity = uuid4()
                conn.execute(
                    "INSERT INTO solve_views(workspace_id,id,run_id,episode_id,parent_id,"
                    "repository_state_id,content,view_digest) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (context.workspace_id, identity, run_id, policy.task_id, parent_view_id,
                     view.repository_state_id, Jsonb(_view_dict(view)), view.digest),
                )
                return {"id": str(identity), "view_digest": view.digest}
            return idempotent_command(conn, context, "solve_view", key, request, create)

    def add_node(self, context: WorkspaceContext, run_id: UUID, parent_id: UUID,
                 view_id: UUID, kind: NodeKind, producer: str,
                 payload_digest: str, key: str) -> dict[str, Any]:
        request = {"run": str(run_id), "parent": str(parent_id), "view": str(view_id),
                   "kind": str(kind), "producer": producer, "payload": payload_digest}
        with self.database.transaction(context) as conn:
            def create() -> dict[str, Any]:
                run = _run(conn, context, run_id, lock=True)
                _require_task_current(conn, context, run)
                if run["status"] not in {"ready", "running"}:
                    raise StateMismatchError("The solve run is not accepting graph nodes")
                policy = _policy_from_dict(run["policy"])
                previous = _node(conn, context, run_id, parent_id)
                stored_view = _view(conn, context, run_id, view_id)
                view = _view_from_dict(stored_view["content"])
                candidate = GraphNode(uuid4(), kind, view, producer, payload_digest,
                                      previous["depth"] + 1)
                projection = GraphProjection(policy)
                projection.add_node(candidate)
                if candidate.depth > policy.max_depth:
                    raise StateMismatchError("Search depth exhausted")
                existing = conn.execute(
                    "SELECT id FROM solve_nodes WHERE workspace_id=%s AND run_id=%s AND identity_hash=%s",
                    (context.workspace_id, run_id, candidate.identity),
                ).fetchone()
                if existing:
                    node_id = existing["id"]
                else:
                    count = conn.execute(
                        "SELECT count(*) AS n FROM solve_nodes WHERE workspace_id=%s AND run_id=%s",
                        (context.workspace_id, run_id),
                    ).fetchone()["n"]
                    if count >= policy.max_nodes:
                        raise StateMismatchError("Search node allowance exhausted")
                    node_id = candidate.id
                    conn.execute(
                        "INSERT INTO solve_nodes(workspace_id,id,run_id,episode_id,view_id,"
                        "parent_id,kind,depth,producer,payload_digest,identity_hash) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (context.workspace_id, node_id, run_id, policy.task_id, view_id,
                         parent_id, kind.value, candidate.depth, producer,
                         payload_digest, candidate.identity),
                    )
                    conn.execute(
                        "INSERT INTO solve_frontier(workspace_id,run_id,node_id) VALUES(%s,%s,%s)",
                        (context.workspace_id, run_id, node_id),
                    )
                    _event(conn, context, run_id, node_id, 0, "created", {"kind": kind.value})
                conn.execute(
                    "INSERT INTO solve_node_contributions(workspace_id,id,run_id,node_id,"
                    "producer,evidence_digest) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (context.workspace_id, uuid4(), run_id, node_id, producer, payload_digest),
                )
                return {"id": str(node_id), "identity": candidate.identity}
            return idempotent_command(conn, context, "solve_add_node", key, request, create)

    def add_edge(self, context: WorkspaceContext, run_id: UUID,
                 source: UUID, target: UUID, kind: EdgeKind, key: str) -> dict[str, Any]:
        if not isinstance(kind, EdgeKind):
            raise InvalidInputError("A registered graph edge kind is required")
        request = {"run": str(run_id), "source": str(source), "target": str(target),
                   "kind": kind.value}
        with self.database.transaction(context) as conn:
            def create() -> dict[str, Any]:
                run = _run(conn, context, run_id, lock=True)
                _require_task_current(conn, context, run)
                if run["status"] not in {"ready", "running"}:
                    raise StateMismatchError("Search run is not accepting graph edges")
                graph = GraphProjection(_policy_from_dict(run["policy"]))
                records = conn.execute(
                    "SELECT n.*,v.content AS view_content FROM solve_nodes n "
                    "JOIN solve_views v ON v.workspace_id=n.workspace_id AND v.run_id=n.run_id AND v.id=n.view_id "
                    "WHERE n.workspace_id=%s AND n.run_id=%s ORDER BY n.created_at,n.id",
                    (context.workspace_id, run_id),
                ).fetchall()
                for row in records:
                    graph.add_node(GraphNode(row["id"], NodeKind(row["kind"]),
                                             _view_from_dict(row["view_content"]), row["producer"],
                                             row["payload_digest"], row["depth"]))
                rows = conn.execute(
                    "SELECT source_id,target_id,kind FROM solve_edges WHERE workspace_id=%s AND run_id=%s",
                    (context.workspace_id, run_id),
                ).fetchall()
                for row in rows:
                    graph.add_edge(GraphEdge(row["source_id"], row["target_id"], EdgeKind(row["kind"])))
                graph.add_edge(GraphEdge(source, target, kind))
                conn.execute(
                    "INSERT INTO solve_edges(workspace_id,run_id,source_id,target_id,kind) "
                    "VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (context.workspace_id, run_id, source, target, kind.value),
                )
                return request
            return idempotent_command(conn, context, "solve_add_edge", key, request, create)

    def claim(self, context: WorkspaceContext, run_id: UUID, owner_id: UUID,
              lease_seconds: int = 45) -> FrontierLease | None:
        if not isinstance(owner_id, UUID) or type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise InvalidInputError("A bounded worker lease and UUID owner are required")
        with self.database.transaction(context) as conn:
            run = _run(conn, context, run_id, lock=True)
            _require_task_current(conn, context, run)
            if run["status"] not in {"ready", "running"}:
                return None
            # The run lock serializes these reservations across worker processes.
            # There is deliberately no reset for an expired external-effect claim.
            used_calls = conn.execute(
                "SELECT count(*) AS n FROM solve_frontier_events WHERE workspace_id=%s "
                "AND run_id=%s AND event_type='claimed'",
                (context.workspace_id, run_id),
            ).fetchone()["n"]
            if used_calls >= _policy_from_dict(run["policy"]).max_model_calls:
                return None
            row = conn.execute(
                "SELECT * FROM solve_frontier WHERE workspace_id=%s AND run_id=%s "
                "AND status='ready' ORDER BY updated_at DESC,node_id FOR UPDATE SKIP LOCKED LIMIT 1",
                (context.workspace_id, run_id),
            ).fetchone()
            if row is None:
                return None
            claimed = conn.execute(
                "UPDATE solve_frontier SET status='claimed',generation=generation+1,"
                "owner_id=%s,lease_until=clock_timestamp()+(%s * interval '1 second'),"
                "updated_at=clock_timestamp() WHERE workspace_id=%s AND run_id=%s AND node_id=%s "
                "AND status='ready' RETURNING generation,lease_until",
                (owner_id, lease_seconds, context.workspace_id, run_id, row["node_id"]),
            ).fetchone()
            if claimed is None:
                raise RevisionConflictError("The frontier node was claimed concurrently")
            if run["status"] == "ready":
                conn.execute(
                    "UPDATE solve_runs SET status='running',generation=generation+1,"
                    "updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, run_id),
                )
            _event(conn, context, run_id, row["node_id"], claimed["generation"], "claimed",
                   {"owner_id": str(owner_id)})
            return FrontierLease(run_id, row["node_id"], owner_id,
                                 claimed["generation"], claimed["lease_until"])

    @staticmethod
    def _lease_row(conn: Any, context: WorkspaceContext, lease: FrontierLease) -> dict[str, Any]:
        row = conn.execute(
            "SELECT *,lease_until>clock_timestamp() AS valid_lease FROM solve_frontier "
            "WHERE workspace_id=%s AND run_id=%s AND node_id=%s FOR UPDATE",
            (context.workspace_id, lease.run_id, lease.node_id),
        ).fetchone()
        if (row is None or row["status"] != "claimed" or row["generation"] != lease.generation
                or row["owner_id"] != lease.owner_id or not row["valid_lease"]):
            raise RevisionConflictError("The frontier claim is not current")
        return dict(row)

    def finish(self, context: WorkspaceContext, lease: FrontierLease,
               outcome_digest: str, key: str) -> dict[str, Any]:
        request = {"run": str(lease.run_id), "node": str(lease.node_id),
                   "owner": str(lease.owner_id), "generation": lease.generation,
                   "outcome_digest": outcome_digest}
        with self.database.transaction(context) as conn:
            def settle() -> dict[str, Any]:
                run = _run(conn, context, lease.run_id, lock=True)
                _require_task_current(conn, context, run)
                if run["status"] != "running":
                    raise StateMismatchError("The run is not active")
                self._lease_row(conn, context, lease)
                if conn.execute(
                    "SELECT 1 FROM artifacts WHERE workspace_id=%s AND digest=%s",
                    (context.workspace_id, outcome_digest),
                ).fetchone() is None:
                    raise ScopeDeniedError("A frontier outcome requires a retained artifact")
                updated = conn.execute(
                    "UPDATE solve_frontier SET status='finished',owner_id=NULL,lease_until=NULL,"
                    "outcome_digest=%s,updated_at=clock_timestamp() WHERE workspace_id=%s "
                    "AND run_id=%s AND node_id=%s AND owner_id=%s AND generation=%s "
                    "AND status='claimed' AND lease_until>clock_timestamp()",
                    (outcome_digest, context.workspace_id, lease.run_id, lease.node_id,
                     lease.owner_id, lease.generation),
                )
                if updated.rowcount != 1:
                    raise RevisionConflictError("The frontier claim expired before settlement")
                _event(conn, context, lease.run_id, lease.node_id, lease.generation,
                       "settled", {"artifact_digest": outcome_digest})
                return {"run_id": str(lease.run_id), "node_id": str(lease.node_id),
                        "generation": lease.generation, "status": "finished"}
            return idempotent_command(conn, context, "solve_finish_frontier", key, request, settle)

    def quarantine_expired(self, context: WorkspaceContext, run_id: UUID) -> list[str]:
        with self.database.transaction(context) as conn:
            _run(conn, context, run_id, lock=True)
            rows = conn.execute(
                "UPDATE solve_frontier SET status='quarantined',owner_id=NULL,lease_until=NULL,"
                "updated_at=clock_timestamp() WHERE workspace_id=%s AND run_id=%s "
                "AND status='claimed' AND lease_until<=clock_timestamp() "
                "RETURNING node_id,generation",
                (context.workspace_id, run_id),
            ).fetchall()
            for row in rows:
                _event(conn, context, run_id, row["node_id"], row["generation"],
                       "expired", {"replay_allowed": False})
            if rows:
                conn.execute("UPDATE solve_runs SET status='indeterminate',generation=generation+1,"
                             "updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s",
                             (context.workspace_id, run_id))
            return [str(row["node_id"]) for row in rows]

    def inspect(self, context: WorkspaceContext, run_id: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            run = _run(conn, context, run_id)
            nodes = conn.execute(
                "SELECT n.*,coalesce(f.status,'ready') AS frontier_state, f.generation "
                "FROM solve_nodes n LEFT JOIN solve_frontier f ON f.workspace_id=n.workspace_id "
                "AND f.run_id=n.run_id AND f.node_id=n.id WHERE n.workspace_id=%s AND n.run_id=%s "
                "ORDER BY n.created_at,n.id LIMIT 1025",
                (context.workspace_id, run_id),
            ).fetchall()
            return {"run": json_record(run), "nodes": json_record(list(nodes)),
                    "node_count": len(nodes), "policy_digest": run["policy_digest"]}
