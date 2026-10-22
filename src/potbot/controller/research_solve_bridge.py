"""Explicitly attach audited repository research to a pinned solve knowledge view.

This controller-only path performs no model/tool dispatch and never resolves an
information need, approves a code change, or activates an unregistered SYN Cog.
It binds an immutable source audit to an observation graph node, not to a
verification/candidate node. The source evidence event remains the authority.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

from potbot.controller.research_audit import ResearchAuditStore
from potbot.controller.research_worker import _action, _current
from potbot.controller.research_worker import _run as _research_run
from potbot.controller.solve_store import (
    _event,
    _evidence_checks,
    _policy_from_dict,
    _require_task_current,
    _view_dict,
    _view_from_dict,
)
from potbot.controller.solve_store import (
    _run as _solve_run,
)
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.search_graph import GraphNode, KnowledgeView, NodeKind
from potbot.domain.serialization import content_hash
from potbot.persistence.artifacts import ArtifactStore
from potbot.persistence.database import Database, idempotent_command
from potbot.research.contracts import ResearchCategory, ResearchScope

_PRODUCER = "potbot.repository_research"
_TYPE_REF = "synny://type/potbot-research-solve-link"


def observation_view(
    parent: KnowledgeView, scope: ResearchScope, event_digest: str
) -> KnowledgeView:
    """Add exactly one *scoped* source evidence hash, retaining all prior knowledge.

    This pure function is a representation transform; it does not authenticate the
    digest. The bridge fetches the checked event from the restricted database.
    """
    if not isinstance(parent, KnowledgeView) or not isinstance(scope, ResearchScope):
        raise InvalidInputError("Research knowledge requires typed scope and parent view")
    if (
        parent.workspace_id != scope.workspace_id
        or parent.task_id != scope.task_id
        or parent.contract_revision_id != scope.contract_revision_id
        or parent.repository_state_id != scope.repository_state_id
        or parent.semantic_release_digest != scope.semantic_release_digest
    ):
        raise RevisionConflictError("Research observation is from a different knowledge state")
    if (
        not isinstance(event_digest, str)
        or len(event_digest) != 64
        or any(letter not in "0123456789abcdef" for letter in event_digest)
    ):
        raise InvalidInputError("Research evidence requires an exact SHA-256 identity")
    return replace(
        parent,
        evidence_digests=tuple(sorted({*parent.evidence_digests, event_digest})),
    )


class ResearchSolveBridge:
    """Atomic evidence-only solve graph publication, with source-byte audit.

    The caller must be a trusted controller with an authenticated restricted
    WorkspaceContext. Model-generated UUIDs, receipts or grant claims never
    authorize this operation. Only completed local repository research qualifies.
    """

    def __init__(self, database: Database, artifacts: ArtifactStore) -> None:
        if not isinstance(database, Database) or artifacts.db != database:
            raise InvalidInputError("Research/Solve link must use one authenticated database")
        self.database = database
        self.artifacts = artifacts
        self.audit = ResearchAuditStore(database, artifacts)

    def link(
        self,
        context: WorkspaceContext,
        research_run_id: UUID,
        solve_run_id: UUID,
        parent_node_id: UUID,
        key: str,
    ) -> dict[str, Any]:
        if any(
            not isinstance(identity, UUID)
            for identity in (research_run_id, solve_run_id, parent_node_id)
        ):
            raise InvalidInputError("Research/Solve graph link requires three UUID identities")
        # Replay the *actual source and output bytes*, not just the claimed
        # receipt. Completed research artifacts and receipts are immutable.
        audited = self.audit.audit(context, research_run_id)
        if audited["qualification"] != "verified_immutable_repository_source_only":
            raise StateMismatchError("Research source has not been independently audited")
        request = {
            "research_run_id": str(research_run_id),
            "solve_run_id": str(solve_run_id),
            "parent_node_id": str(parent_node_id),
            "receipt_digest": audited["receipt_digest"],
        }
        from psycopg.types.json import Jsonb

        with self.database.transaction(context) as conn:

            def publish() -> dict[str, Any]:
                solve = _solve_run(conn, context, solve_run_id, lock=True)
                _require_task_current(conn, context, solve)
                if solve["status"] not in {"ready", "running"}:
                    raise StateMismatchError("Solve run no longer accepts source observations")
                research = _research_run(conn, context, research_run_id)
                action, source = _action(conn, context, research["action_id"])
                _current(conn, context, action, source)
                if action.query.category != ResearchCategory.REPOSITORY:
                    raise ScopeDeniedError("Only audited immutable repository sources qualify")
                scope = action.scope
                if (
                    research["state"] != "succeeded"
                    or research["episode_id"] != solve["episode_id"]
                    or research["action_identity"] != action.identity
                    or research["source_revision"] != source["content_digest"]
                    or research["source_artifact_id"] != source["snapshot_artifact_id"]
                    or research["receipt_digest"] != audited["receipt_digest"]
                    or scope.task_id != solve["episode_id"]
                    or scope.contract_revision_id != solve["contract_revision_id"]
                    or scope.repository_state_id != solve["repository_state_id"]
                    or scope.semantic_release_digest != solve["semantic_release_digest"]
                ):
                    raise RevisionConflictError("Audited source is not from the solve baseline")
                existing = conn.execute(
                    "SELECT * FROM research_solve_links WHERE workspace_id=%s "
                    "AND solve_run_id=%s AND research_run_id=%s",
                    (context.workspace_id, solve_run_id, research_run_id),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["parent_node_id"] != parent_node_id
                        or content_hash(existing["content"]) != existing["link_digest"]
                    ):
                        raise RevisionConflictError("Existing source link differs from request")
                    return {
                        "link_id": str(existing["id"]),
                        "node_id": str(existing["node_id"]),
                        "view_id": str(existing["view_id"]),
                        "evidence_event_id": str(existing["evidence_event_id"]),
                        "link_digest": existing["link_digest"],
                        "reused": True,
                        "patch_verified": False,
                        "need_resolved": False,
                    }
                policy = _policy_from_dict(solve["policy"])
                if policy.digest != solve["policy_digest"]:
                    raise StateMismatchError("Solve policy no longer matches its identity")
                parent = conn.execute(
                    "SELECT n.*,v.content AS view_content,v.view_digest "
                    "FROM solve_nodes n JOIN solve_views v "
                    "ON v.workspace_id=n.workspace_id AND v.run_id=n.run_id AND v.id=n.view_id "
                    "WHERE n.workspace_id=%s AND n.run_id=%s AND n.id=%s",
                    (context.workspace_id, solve_run_id, parent_node_id),
                ).fetchone()
                if parent is None:
                    raise ScopeDeniedError("Parent solve graph node is unavailable")
                parent_view = _view_from_dict(parent["view_content"])
                if parent_view.digest != parent["view_digest"]:
                    raise StateMismatchError("Parent knowledge view integrity failed")
                if parent["depth"] + 1 > policy.max_depth:
                    raise StateMismatchError("Observation exceeds the solve depth allowance")
                # The worker's append-only tool event must bind exactly this run,
                # result artifact, action and output receipt, in this workspace.
                events = conn.execute(
                    "SELECT id,episode_id,payload_id,content_digest,details,producer_kind,"
                    "producer_id FROM evidence_events WHERE workspace_id=%s AND episode_id=%s "
                    "AND event_type='repository_research_observed' "
                    "AND details->>'research_run_id'=%s ORDER BY recorded_at,id LIMIT 2",
                    (context.workspace_id, scope.task_id, str(research_run_id)),
                ).fetchall()
                expected_details = {
                    "research_run_id": str(research_run_id),
                    "research_action_id": str(action.action_id),
                    "receipt_digest": audited["receipt_digest"],
                    "source_revision": research["source_revision"],
                }
                if len(events) != 1:
                    raise StateMismatchError("Source evidence event is missing or ambiguous")
                event = events[0]
                if (
                    event["episode_id"] != scope.task_id
                    or event["payload_id"] != research["result_artifact_id"]
                    or event["details"] != expected_details
                    or event["producer_kind"] != "tool"
                    or event["producer_id"] != _PRODUCER
                ):
                    raise StateMismatchError(
                        "Source evidence event does not match audited research"
                    )
                view = observation_view(parent_view, scope, event["content_digest"])
                _evidence_checks(conn, context, policy, view)
                # Reuse a content-equivalent view only when it genuinely has
                # this parent. No silent cross-branch reparenting.
                found_view = conn.execute(
                    "SELECT id,parent_id FROM solve_views WHERE workspace_id=%s "
                    "AND run_id=%s AND view_digest=%s",
                    (context.workspace_id, solve_run_id, view.digest),
                ).fetchone()
                if found_view is not None:
                    if found_view["parent_id"] != parent["view_id"]:
                        raise RevisionConflictError(
                            "Equivalent source view belongs to another branch"
                        )
                    view_id = found_view["id"]
                else:
                    view_id = uuid4()
                    conn.execute(
                        "INSERT INTO solve_views(workspace_id,id,run_id,episode_id,parent_id,"
                        "repository_state_id,content,view_digest) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            context.workspace_id,
                            view_id,
                            solve_run_id,
                            scope.task_id,
                            parent["view_id"],
                            scope.repository_state_id,
                            Jsonb(_view_dict(view)),
                            view.digest,
                        ),
                    )
                node = GraphNode(
                    uuid4(),
                    NodeKind.OBSERVATION,
                    view,
                    _PRODUCER,
                    audited["receipt_digest"],
                    parent["depth"] + 1,
                )
                # The node is an already-completed *observation*, never a
                # candidate queued for automatic model/patch execution.
                current = conn.execute(
                    "SELECT id,parent_id,view_id FROM solve_nodes WHERE workspace_id=%s "
                    "AND run_id=%s AND identity_hash=%s",
                    (context.workspace_id, solve_run_id, node.identity),
                ).fetchone()
                if current is not None:
                    if current["parent_id"] != parent_node_id or current["view_id"] != view_id:
                        raise RevisionConflictError(
                            "Equivalent observation exists on another branch"
                        )
                    node_id = current["id"]
                else:
                    count = conn.execute(
                        "SELECT count(*) AS n FROM solve_nodes WHERE workspace_id=%s AND run_id=%s",
                        (context.workspace_id, solve_run_id),
                    ).fetchone()
                    if count is None or count["n"] >= policy.max_nodes:
                        raise StateMismatchError("Solve node allowance exhausted")
                    node_id = node.id
                    conn.execute(
                        "INSERT INTO solve_nodes(workspace_id,id,run_id,episode_id,view_id,"
                        "parent_id,kind,depth,producer,payload_digest,identity_hash) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            context.workspace_id,
                            node_id,
                            solve_run_id,
                            scope.task_id,
                            view_id,
                            parent_node_id,
                            NodeKind.OBSERVATION.value,
                            node.depth,
                            _PRODUCER,
                            node.payload_digest,
                            node.identity,
                        ),
                    )
                    conn.execute(
                        "INSERT INTO solve_frontier(workspace_id,run_id,node_id,status,"
                        "outcome_digest) VALUES(%s,%s,%s,'finished',%s)",
                        (context.workspace_id, solve_run_id, node_id, audited["receipt_digest"]),
                    )
                    _event(
                        conn,
                        context,
                        solve_run_id,
                        node_id,
                        0,
                        "created",
                        {
                            "kind": NodeKind.OBSERVATION.value,
                            "source": "audited_repository",
                        },
                    )
                    _event(
                        conn,
                        context,
                        solve_run_id,
                        node_id,
                        0,
                        "settled",
                        {
                            "source_receipt": audited["receipt_digest"],
                            "patch_verified": False,
                        },
                    )
                conn.execute(
                    "INSERT INTO solve_edges(workspace_id,run_id,source_id,target_id,kind) "
                    "VALUES(%s,%s,%s,%s,'observes') ON CONFLICT DO NOTHING",
                    (context.workspace_id, solve_run_id, parent_node_id, node_id),
                )
                conn.execute(
                    "INSERT INTO solve_node_contributions(workspace_id,id,run_id,node_id,"
                    "producer,evidence_digest) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (
                        context.workspace_id,
                        uuid4(),
                        solve_run_id,
                        node_id,
                        _PRODUCER,
                        event["content_digest"],
                    ),
                )
                content = {
                    "schema_version": 1,
                    "primary_type_ref": _TYPE_REF,
                    "research_run_id": str(research_run_id),
                    "solve_run_id": str(solve_run_id),
                    "parent_node_id": str(parent_node_id),
                    "observation_node_id": str(node_id),
                    "knowledge_view_id": str(view_id),
                    "knowledge_view_digest": view.digest,
                    "source_event_id": str(event["id"]),
                    "source_event_digest": event["content_digest"],
                    "source_receipt_digest": audited["receipt_digest"],
                    "source_artifact_id": str(research["source_artifact_id"]),
                    "result_artifact_id": str(research["result_artifact_id"]),
                    "audit_report_digest": content_hash(audited),
                    "observation_count": audited["observation_count"],
                    "complete_scan": audited["complete_scan"],
                    "scope": scope.to_dict(),
                    "qualification": "verified_immutable_repository_source_only",
                    "cog_activated": False,
                    "need_resolved": False,
                    "patch_verified": False,
                    "license_assessment": "unknown",
                    "model_dispatched": False,
                }
                identity, digest = uuid4(), content_hash(content)
                conn.execute(
                    "INSERT INTO research_solve_links(workspace_id,id,episode_id,"
                    "research_run_id,solve_run_id,parent_node_id,node_id,view_id,"
                    "evidence_event_id,source_receipt_digest,link_digest,content) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        context.workspace_id,
                        identity,
                        scope.task_id,
                        research_run_id,
                        solve_run_id,
                        parent_node_id,
                        node_id,
                        view_id,
                        event["id"],
                        audited["receipt_digest"],
                        digest,
                        Jsonb(content),
                    ),
                )
                return {
                    "link_id": str(identity),
                    "node_id": str(node_id),
                    "view_id": str(view_id),
                    "evidence_event_id": str(event["id"]),
                    "link_digest": digest,
                    "reused": False,
                    "patch_verified": False,
                    "need_resolved": False,
                }

            return idempotent_command(
                conn, context, "phase7_link_research_solve", key, request, publish
            )

    def inspect(
        self, context: WorkspaceContext, solve_run_id: UUID, research_run_id: UUID
    ) -> dict[str, Any]:
        if not isinstance(solve_run_id, UUID) or not isinstance(research_run_id, UUID):
            raise InvalidInputError("Inspecting research graph linkage requires UUIDs")
        with self.database.transaction(context) as conn:
            row = conn.execute(
                "SELECT * FROM research_solve_links WHERE workspace_id=%s "
                "AND solve_run_id=%s AND research_run_id=%s",
                (context.workspace_id, solve_run_id, research_run_id),
            ).fetchone()
            if row is None:
                raise ScopeDeniedError("Research source graph link is unavailable")
            if content_hash(row["content"]) != row["link_digest"]:
                raise StateMismatchError("Research source graph link was altered")
            return {
                "link_id": str(row["id"]),
                "content": row["content"],
                "link_digest": row["link_digest"],
                "patch_verified": False,
                "need_resolved": False,
            }
