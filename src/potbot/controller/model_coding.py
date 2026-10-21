"""Admit recorded model output into the existing immutable coding result store.

Inference acceptance establishes framing only. This controller rechecks the
active handoff, exact packet, retained output, gateway journal and settled budget
before publishing a patch or inability. It never applies or verifies a patch.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from potbot.controller.coding import coding_row
from potbot.controller.execution import scoped_row
from potbot.controller.information_needs import InformationNeedStore
from potbot.controller.tasks import json_record, lock_episode
from potbot.domain.auth import WorkspaceContext
from potbot.domain.coding import CODING_RESPONSE_SCHEMA
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.model_proposals import ModelCodingResponse, admit_coding_response
from potbot.domain.serialization import content_hash
from potbot.models.context import ContextPacketService
from potbot.models.envelopes import parse_model_response
from potbot.persistence.artifacts import ArtifactStore
from potbot.persistence.database import Database, idempotent_command
from potbot.persistence.evidence import append_event


class ModelCodingService:
    def __init__(self, database: Database, artifacts: ArtifactStore) -> None:
        self.database, self.artifacts = database, artifacts
        self.contexts = ContextPacketService(database, artifacts)

    @staticmethod
    def _invocation(conn: Any, context: WorkspaceContext, invocation_id: UUID) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM model_invocations WHERE workspace_id=%s AND id=%s",
            (context.workspace_id, invocation_id),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError("Model invocation is unavailable in this workspace.")
        result = dict(row)
        if content_hash(result["input_manifest"]) != result["input_hash"]:
            raise InvalidInputError("Invocation input provenance failed validation.")
        if (
            result["status"] != "succeeded"
            or result["parse_status"] != "parsed"
            or result["error_code"] is not None
            or result["parsed_result"] is None
        ):
            raise StateMismatchError("Coding requires a successfully parsed model invocation.")
        return result

    @staticmethod
    def _gateway_receipt(
        conn: Any,
        context: WorkspaceContext,
        invocation: dict[str, Any],
        manifest: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        matches = conn.execute(
            "SELECT idempotency_key FROM command_results WHERE workspace_id=%s "
            "AND operation='inference_v1_finished' AND result->>'invocation_id'=%s LIMIT 2",
            (context.workspace_id, str(invocation["id"])),
        ).fetchall()
        if len(matches) != 1:
            raise StateMismatchError("Coding requires one unambiguous gateway completion.")
        records = {
            row["operation"]: dict(row)
            for row in conn.execute(
                "SELECT operation,request_hash,result FROM command_results "
                "WHERE workspace_id=%s AND idempotency_key=%s AND operation IN "
                "('inference_v1_operation','inference_v1_admitted',"
                "'inference_v1_started','inference_v1_finished')",
                (context.workspace_id, matches[0]["idempotency_key"]),
            ).fetchall()
        }
        if len(records) != 4:
            raise StateMismatchError("Coding gateway provenance is incomplete.")
        operation = records["inference_v1_operation"]["result"]
        finished = records["inference_v1_finished"]["result"]
        admitted = records["inference_v1_admitted"]["result"]
        started = records["inference_v1_started"]["result"]
        if (
            content_hash(operation["descriptor"])
            != records["inference_v1_operation"]["request_hash"]
            or any(
                row["request_hash"] != content_hash(operation)
                for key, row in records.items()
                if key != "inference_v1_operation"
            )
            or any(
                item.get("operation_id") != operation["operation_id"]
                for item in (finished, admitted, started)
            )
            or operation["identity"] != manifest["identity"]
            or operation["request"] != invocation["input_manifest"]["request"]
            or operation["input_hash"] != invocation["input_hash"]
            or started.get("dispatch_may_have_started") is not True
        ):
            raise InvalidInputError("Coding gateway journal bindings failed validation.")
        if (
            finished.get("status") != "accepted"
            or finished.get("usable") is not True
            or finished.get("executable") is not False
            or finished.get("raw_output_artifact_id") != str(invocation["raw_output_artifact_id"])
            or finished.get("parsed_result") != invocation["parsed_result"]
            or finished.get("reservation_id") != admitted.get("reservation_id")
            or finished.get("usage") is None
            or finished.get("usage_origin") != invocation["usage_origin"]
        ):
            raise StateMismatchError("Gateway output is not an accepted settled coding response.")
        reservation = scoped_row(
            conn, context, "budget_reservations", UUID(admitted["reservation_id"])
        )
        if (
            reservation["status"] != "settled"
            or str(reservation["episode_id"]) != manifest["baseline"]["episode_id"]
            or reservation["amounts"] != operation["amounts"]
            or reservation["actual"] != finished["usage"]
            or reservation["origin"] != finished["usage_origin"]
        ):
            raise StateMismatchError(
                "Coding usage is not settled against its own task reservation."
            )
        return dict(finished), reservation

    def record(
        self,
        context: WorkspaceContext,
        implementation_id: UUID,
        invocation_id: UUID,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not isinstance(implementation_id, UUID) or not isinstance(invocation_id, UUID):
            raise InvalidInputError("Coding admission requires typed handoff and invocation IDs.")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise InvalidInputError("Coding admission requires a bounded idempotency key.")
        with self.database.transaction(context) as conn:
            invocation = self._invocation(conn, context, invocation_id)
        # This checks every source's actual bytes, the rendering and exact current
        # baseline. The following transaction locks the episode and fences a
        # contract change between this read and publication, including on replay.
        manifest = self.contexts.read(context, invocation["context_packet_id"])
        with self.database.transaction(context) as conn:
            handoff = coding_row(conn, context, "implementation_contracts", implementation_id)
            episode = lock_episode(conn, context, handoff["episode_id"])
            invocation = self._invocation(conn, context, invocation_id)
            baseline = manifest["baseline"]
            request = invocation["input_manifest"]["request"]
            if (
                episode["status"] != "running"
                or episode["active_contract_id"] != handoff["contract_revision_id"]
                or baseline["implementation_id"] != str(implementation_id)
                or baseline["episode_id"] != str(handoff["episode_id"])
                or baseline["contract_revision_id"] != str(handoff["contract_revision_id"])
                or baseline["repository_state_id"] != str(handoff["base_state_id"])
                or manifest["role"] != "coding"
                or manifest["response_schema"] != CODING_RESPONSE_SCHEMA
                or manifest["deployment_id"] != str(invocation["deployment_id"])
                or request["context_packet_id"] != str(invocation["context_packet_id"])
                or request["deployment_id"] != str(invocation["deployment_id"])
                or request["role"] != "coding"
                or request["response_schema"] != CODING_RESPONSE_SCHEMA
            ):
                raise StateMismatchError("Model coding output does not match the current handoff.")
            finished, reservation = self._gateway_receipt(conn, context, invocation, manifest)
            raw = self.artifacts.read_in_transaction(
                conn, context, invocation["raw_output_artifact_id"]
            )
            response = parse_model_response(raw, response_schema=CODING_RESPONSE_SCHEMA)
            if not isinstance(response, ModelCodingResponse):
                raise InvalidInputError("Coding invocation contains another response type.")
            if response.to_dict() != invocation["parsed_result"]:
                raise InvalidInputError(
                    "Retained raw output differs from the recorded parsed result."
                )
            view = scoped_row(conn, context, "knowledge_views", UUID(baseline["knowledge_view_id"]))
            known_events = tuple(UUID(value) for value in view["content"]["evidence_ids"])
            supplied_evidence = tuple(
                dict.fromkeys(
                    (
                        *(UUID(source["id"]) for source in manifest["sources"]),
                        *known_events,
                    )
                )
            )
            query_profiles: tuple[str, ...] = ()
            if any(need.query_profiles for need in response.information_needs):
                from potbot.semantics.releases import catalog_inventory, read_task_pin

                pin = read_task_pin(conn, context, handoff["episode_id"], required=True)
                assert pin is not None
                query_profiles = tuple(
                    row["name"]
                    for row in catalog_inventory(
                        conn, context, pin["release_id"], "retrieval_profile"
                    )
                )
            spec = handoff["content"]["spec"]
            admit_coding_response(
                response,
                implementation_id=implementation_id,
                base_state_id=handoff["base_state_id"],
                contract_revision_id=handoff["contract_revision_id"],
                permitted_paths=tuple(spec["permitted_paths"]),
                known_checks=tuple(check["check_id"] for check in spec["required_checks"]),
                evidence_ids=supplied_evidence,
                query_profiles=query_profiles,
            )
            if any(
                not set(need.known_evidence).issubset(known_events)
                for need in response.information_needs
            ):
                raise ScopeDeniedError("Known question evidence must be a supplied evidence event.")
            command = {
                "implementation_id": str(implementation_id),
                "invocation_id": str(invocation_id),
                "response_digest": content_hash(response.to_dict()),
            }

            def publish() -> dict[str, Any]:
                from psycopg.types.json import Jsonb

                prior = conn.execute(
                    "SELECT id FROM coding_results WHERE workspace_id=%s AND invocation_id=%s",
                    (context.workspace_id, invocation_id),
                ).fetchone()
                if prior is not None:
                    return dict(
                        json_record(coding_row(conn, context, "coding_results", prior["id"]))
                    )
                identity = uuid4()
                patch = (
                    None
                    if response.patch is None
                    else self.artifacts.put_in_transaction(
                        conn, context, response.patch, "text/x-diff"
                    )
                )
                questions = []
                for need in response.information_needs:
                    need_id = uuid4()
                    conn.execute(
                        "INSERT INTO information_needs(workspace_id,id,episode_id,node_id) VALUES(%s,%s,%s,%s)",
                        (
                            context.workspace_id,
                            need_id,
                            handoff["episode_id"],
                            UUID(baseline["node_id"]),
                        ),
                    )
                    revision_id = InformationNeedStore._revision(conn, context, need_id, need, None)
                    conn.execute(
                        "INSERT INTO need_status(workspace_id,need_id,current_revision_id,status) VALUES(%s,%s,%s,'open')",
                        (context.workspace_id, need_id, revision_id),
                    )
                    append_event(
                        conn,
                        context,
                        "information_need_created",
                        episode_id=handoff["episode_id"],
                        node_id=UUID(baseline["node_id"]),
                        producer_kind="model",
                        producer_id=str(invocation["deployment_id"]),
                        deployment_id=invocation["deployment_id"],
                        details={
                            "need_id": str(need_id),
                            "revision_id": str(revision_id),
                            "coding_result_id": str(identity),
                            "invocation_id": str(invocation_id),
                        },
                    )
                    questions.append({"need_id": str(need_id), "revision_id": str(revision_id)})
                content = {
                    **response.to_result_dict(patch["id"] if patch else None),
                    "deployment_id": str(invocation["deployment_id"]),
                    "packet_id": str(invocation["context_packet_id"]),
                    "invocation_id": str(invocation_id),
                    "reservation_id": str(reservation["id"]),
                    "operation_id": finished["operation_id"],
                    "recorded_information_needs": questions,
                    "usage": finished["usage"],
                    "usage_origin": finished["usage_origin"],
                    "verification_status": "unverified",
                    "fixture_only": finished.get("fixture_only") is True,
                }
                conn.execute(
                    "INSERT INTO coding_results(workspace_id,id,episode_id,implementation_id,base_state_id,"
                    "deployment_id,packet_id,invocation_id,reservation_id,patch_artifact_id,inability_code,content,content_digest) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        context.workspace_id,
                        identity,
                        handoff["episode_id"],
                        implementation_id,
                        handoff["base_state_id"],
                        invocation["deployment_id"],
                        invocation["context_packet_id"],
                        invocation_id,
                        reservation["id"],
                        patch["id"] if patch else None,
                        response.inability_code,
                        Jsonb(content),
                        content_hash(content),
                    ),
                )
                append_event(
                    conn,
                    context,
                    "coding_result_recorded",
                    episode_id=handoff["episode_id"],
                    node_id=UUID(baseline["node_id"]),
                    producer_kind="model",
                    producer_id=str(invocation["deployment_id"]),
                    deployment_id=invocation["deployment_id"],
                    payload_id=invocation["raw_output_artifact_id"],
                    details={
                        "coding_result_id": str(identity),
                        "implementation_id": str(implementation_id),
                        "invocation_id": str(invocation_id),
                        "producer_kind": "model",
                        "usage_origin": invocation["usage_origin"],
                    },
                )
                return dict(json_record(coding_row(conn, context, "coding_results", identity)))

            return idempotent_command(
                conn, context, "record_model_coding_v1", idempotency_key, command, publish
            )
