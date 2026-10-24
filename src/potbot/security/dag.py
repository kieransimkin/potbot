"""Reviewed policy context for protected native DAG intent preparation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from potbot.domain.errors import InvalidInputError

if TYPE_CHECKING:
    from syn_client.governance import GovernedPolicyRecord

    from potbot.security.broker import NativeIdentityBroker


def prepare_governance_dag_event(
    broker: NativeIdentityBroker,
    vault: Path,
    body: dict[str, Any],
    *,
    trusted_history: tuple[GovernedPolicyRecord, ...],
    expected_genesis_digest: str,
    expected_head_digest: str,
    existing_policy_digest: str,
    expected_node_hash: str,
    other_parent_event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate retained policy/roster pins before requesting a durable native intent."""
    from syn_client.consensus import NodeKeyHash, PolicyError, _fields, digest_text
    from syn_client.dag_finality import event_digest, governance_finality_context
    from syn_client.dag_signing import (
        validate_dag_body,
        verify_governance_candidate,
        verify_protected_event,
    )
    from syn_client.governance import verified_chain

    try:
        if not trusted_history:
            raise PolicyError("Protected DAG preparation requires retained signed history")
        previous = trusted_history[-1]
        verified_chain(
            trusted_history,
            expected_workspace=previous.state.workspace_id,
            expected_catalog=previous.state.release.catalog_id,
            expected_genesis_digest=expected_genesis_digest,
            expected_head_digest=expected_head_digest,
        )
        digest_text(existing_policy_digest)
        NodeKeyHash(expected_node_hash)
        policies = {
            t.consensus_policy.digest: t.consensus_policy
            for t in previous.state.release.types
            if t.consensus_policy
        }
        p = policies.get(existing_policy_digest)
        if p is None or p.protocol != "syn.bft-dag-hashgraph/2":
            raise PolicyError("This policy has not adopted protected DAG signing")
        context = governance_finality_context(previous, existing_policy_digest)
        roster = next(
            a for a in previous.state.policy_authorities if a.digest == p.membership_anchor_digest
        )
        members = {m["agent"]["signer_hash"]: m for m in roster.memberships()}
        b = validate_dag_body(body)
        if (
            b["context_digest"] != context
            or b["creator"] != expected_node_hash
            or expected_node_hash not in members
        ):
            raise PolicyError("Protected DAG body differs from the old policy or assigned node")
        if b["proposal"] is not None:
            verify_governance_candidate(
                b["proposal"],
                governance=previous.state.type_governance,
                previous_record_digest=previous.digest,
                successor_sequence=str(int(previous.sequence) + 1),
            )
        if b["other_parent"] is None:
            if other_parent_event is not None:
                raise PolicyError("Unexpected other-parent evidence")
        else:
            if other_parent_event is None:
                raise PolicyError("Protected DAG requires authenticated other-parent evidence")
            e = _fields(other_parent_event, {"body", "proof", "reservation"})
            other = e["body"].get("creator") if isinstance(e["body"], dict) else None
            if not isinstance(other, str) or other not in members or other == expected_node_hash:
                raise PolicyError("Other parent is outside the old roster")
            verified = verify_protected_event(e, membership=members[other])
            if (
                verified["body"]["context_digest"] != context
                or event_digest(verified) != b["other_parent"]
            ):
                raise PolicyError("Other parent differs from the policy context or content hash")
        return broker.prepare_dag_event(vault, b, membership=members[expected_node_hash])
    except (PolicyError, ValueError) as error:
        raise InvalidInputError(
            "Protected DAG preparation held: verify history, policy and parents"
        ) from error
