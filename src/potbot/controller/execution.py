"""Durable, scoped execution envelopes independent of model search policy."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from potbot.controller.tasks import json_record, lock_episode
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.execution import ActionProposal, ToolRegistry
from potbot.domain.serialization import content_hash
from potbot.persistence.database import Database, idempotent_command
from potbot.persistence.evidence import append_event


def scoped_row(conn: Any, context: WorkspaceContext, table: str, identity: UUID) -> dict[str, Any]:
    if table not in {
        "knowledge_views",
        "search_nodes",
        "actions",
        "action_runs",
        "repository_states",
        "candidate_workspaces",
        "budget_reservations",
        "contract_revisions",
        "need_revisions",
        "evidence_events",
        "model_deployments",
        "artifacts",
    }:
        raise InvalidInputError("Unregistered execution reference.")
    row = conn.execute(
        f"SELECT * FROM {table} WHERE workspace_id=%s AND id=%s", (context.workspace_id, identity)
    ).fetchone()
    if row is None:
        raise ScopeDeniedError("Execution resource is unavailable in this workspace.")
    return dict(row)


def execution_inputs(
    conn: Any, context: WorkspaceContext, action_id: UUID
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    action = scoped_row(conn, context, "actions", action_id)
    node = scoped_row(conn, context, "search_nodes", action["node_id"])
    view = scoped_row(conn, context, "knowledge_views", node["knowledge_view_id"])
    if (
        content_hash(action["proposal"]) != action["proposal_digest"]
        or content_hash(view["content"]) != view["content_digest"]
    ):
        raise InvalidInputError("Execution input integrity check failed.")
    return action, node, view


class ExecutionStore:
    def __init__(self, database: Database, registry: ToolRegistry) -> None:
        self.database = database
        self.registry = registry

    def create_inputs(
        self,
        context: WorkspaceContext,
        episode_id: UUID,
        contract_revision_id: UUID,
        repository_state_id: UUID,
        idempotency_key: str,
        *,
        evidence_ids: tuple[UUID, ...] = (),
        need_revision_ids: tuple[UUID, ...] = (),
        parent_node_id: UUID | None = None,
    ) -> dict[str, Any]:
        for values in (evidence_ids, need_revision_ids):
            if (
                not isinstance(values, tuple)
                or len(values) > 256
                or any(not isinstance(v, UUID) for v in values)
                or len(set(values)) != len(values)
            ):
                raise InvalidInputError("Input references must be bounded distinct UUID sequences.")
        request = {
            "episode_id": str(episode_id),
            "contract_revision_id": str(contract_revision_id),
            "repository_state_id": str(repository_state_id),
            "evidence_ids": list(map(str, evidence_ids)),
            "need_revision_ids": list(map(str, need_revision_ids)),
            "parent_node_id": str(parent_node_id) if parent_node_id else None,
        }
        with self.database.transaction(context) as conn:

            def create() -> dict[str, Any]:
                from psycopg.types.json import Jsonb

                from potbot.semantics.releases import read_task_pin

                episode = lock_episode(conn, context, episode_id)
                if episode["active_contract_id"] != contract_revision_id:
                    raise StateMismatchError(
                        "Execution inputs require the active contract revision."
                    )
                state = scoped_row(conn, context, "repository_states", repository_state_id)
                if state["repository_id"] != episode["repository_id"]:
                    raise ScopeDeniedError("Repository state belongs to another repository.")
                pin = read_task_pin(conn, context, episode_id, required=True)
                assert pin is not None
                for identity in evidence_ids:
                    scoped_row(conn, context, "evidence_events", identity)
                for identity in need_revision_ids:
                    revision = scoped_row(conn, context, "need_revisions", identity)
                    need = conn.execute(
                        "SELECT episode_id FROM information_needs WHERE workspace_id=%s AND id=%s",
                        (context.workspace_id, revision["need_id"]),
                    ).fetchone()
                    if need["episode_id"] != episode_id:
                        raise ScopeDeniedError("Information need belongs to another task.")
                if parent_node_id is not None:
                    parent = scoped_row(conn, context, "search_nodes", parent_node_id)
                    if parent["episode_id"] != episode_id:
                        raise ScopeDeniedError("Parent node belongs to another task.")
                view_id, node_id = uuid4(), uuid4()
                content = {
                    "schema_version": 1,
                    **request,
                    "semantic_release_id": str(pin["release_id"]),
                }
                conn.execute(
                    "INSERT INTO knowledge_views(workspace_id,id,episode_id,contract_revision_id,repository_state_id,semantic_release_id,content,content_digest) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        context.workspace_id,
                        view_id,
                        episode_id,
                        contract_revision_id,
                        repository_state_id,
                        pin["release_id"],
                        Jsonb(content),
                        content_hash(content),
                    ),
                )
                for identity in evidence_ids:
                    conn.execute(
                        "INSERT INTO knowledge_view_evidence(workspace_id,knowledge_view_id,evidence_id) VALUES(%s,%s,%s)",
                        (context.workspace_id, view_id, identity),
                    )
                conn.execute(
                    "INSERT INTO search_nodes(workspace_id,id,episode_id,knowledge_view_id,parent_id) VALUES(%s,%s,%s,%s,%s)",
                    (context.workspace_id, node_id, episode_id, view_id, parent_node_id),
                )
                append_event(
                    conn,
                    context,
                    "execution_inputs_frozen",
                    episode_id=episode_id,
                    node_id=node_id,
                    details={
                        "knowledge_view_id": str(view_id),
                        "repository_state_id": str(repository_state_id),
                    },
                )
                return {
                    "node_id": str(node_id),
                    "knowledge_view_id": str(view_id),
                    "content": content,
                }

            return idempotent_command(
                conn, context, "create_execution_inputs", idempotency_key, request, create
            )

    def propose(
        self,
        context: WorkspaceContext,
        node_id: UUID,
        proposal: ActionProposal,
        idempotency_key: str,
        *,
        dependencies: tuple[UUID, ...] = (),
        deployment_id: UUID | None = None,
        stdin_artifact_id: UUID | None = None,
    ) -> dict[str, Any]:
        definition = self.registry.get(proposal.kind)
        definition.admit(proposal)
        if stdin_artifact_id is not None and (
            not isinstance(stdin_artifact_id, UUID) or not definition.accepts_stdin
        ):
            raise ScopeDeniedError("This tool does not accept an artifact as standard input.")
        if (
            not isinstance(dependencies, tuple)
            or len(dependencies) > 128
            or any(not isinstance(v, UUID) for v in dependencies)
            or len(set(dependencies)) != len(dependencies)
        ):
            raise InvalidInputError("Action dependencies must be distinct scoped UUID references.")
        request = {
            "node_id": str(node_id),
            "proposal": proposal.to_dict(),
            "dependencies": list(map(str, dependencies)),
            "tool_definition_digest": definition.fingerprint,
            "deployment_id": str(deployment_id) if deployment_id else None,
        }
        if stdin_artifact_id is not None:
            request["stdin_artifact_id"] = str(stdin_artifact_id)
        with self.database.transaction(context) as conn:

            def create() -> dict[str, Any]:
                from psycopg.types.json import Jsonb

                node = scoped_row(conn, context, "search_nodes", node_id)
                view = scoped_row(conn, context, "knowledge_views", node["knowledge_view_id"])
                episode = lock_episode(conn, context, node["episode_id"])
                if view["contract_revision_id"] != episode["active_contract_id"]:
                    raise StateMismatchError("Proposal belongs to a superseded contract.")
                for identity in dependencies:
                    dependency = scoped_row(conn, context, "actions", identity)
                    if dependency["episode_id"] != node["episode_id"]:
                        raise ScopeDeniedError("Dependency belongs to another task.")
                if deployment_id is not None:
                    scoped_row(conn, context, "model_deployments", deployment_id)
                if stdin_artifact_id is not None:
                    artifact = scoped_row(conn, context, "artifacts", stdin_artifact_id)
                    if artifact["byte_length"] > 16_777_216:
                        raise InvalidInputError(
                            "Tool input artifact exceeds its bounded input limit."
                        )
                action_id = uuid4()
                kind = "model" if deployment_id else "controller"
                producer_id = str(deployment_id) if deployment_id else "potbot.execution-controller"
                conn.execute(
                    "INSERT INTO actions(workspace_id,id,episode_id,node_id,kind,tool_definition_digest,proposal,proposal_digest,producer_kind,producer_id,deployment_id,stdin_artifact_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        context.workspace_id,
                        action_id,
                        node["episode_id"],
                        node_id,
                        proposal.kind,
                        definition.fingerprint,
                        Jsonb(proposal.to_dict()),
                        content_hash(proposal.to_dict()),
                        kind,
                        producer_id,
                        deployment_id,
                        stdin_artifact_id,
                    ),
                )
                for identity in dependencies:
                    conn.execute(
                        "INSERT INTO action_dependencies(workspace_id,episode_id,action_id,dependency_id) VALUES(%s,%s,%s,%s)",
                        (context.workspace_id, node["episode_id"], action_id, identity),
                    )
                append_event(
                    conn,
                    context,
                    "action_proposed",
                    episode_id=node["episode_id"],
                    node_id=node_id,
                    details={"action_id": str(action_id), "kind": proposal.kind},
                )
                return dict(json_record(scoped_row(conn, context, "actions", action_id)))

            return idempotent_command(
                conn, context, "propose_action", idempotency_key, request, create
            )

    def inspect(self, context: WorkspaceContext, action_id: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as conn:
            action, node, view = execution_inputs(conn, context, action_id)
            run = conn.execute(
                "SELECT * FROM action_runs WHERE workspace_id=%s AND action_id=%s",
                (context.workspace_id, action_id),
            ).fetchone()
            completion = (
                None
                if run is None
                else conn.execute(
                    "SELECT * FROM action_completions WHERE workspace_id=%s AND run_id=%s",
                    (context.workspace_id, run["id"]),
                ).fetchone()
            )
            return dict(
                json_record(
                    {
                        "schema_version": 1,
                        "execution_status": run["status"] if run else "proposed",
                        "action": action,
                        "node": node,
                        "knowledge_view": view,
                        "run": dict(run) if run else None,
                        "completion": dict(completion) if completion else None,
                    }
                )
            )
