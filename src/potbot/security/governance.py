"""Public governance approval adapter; all signing remains in native custody."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from potbot.domain.errors import InvalidInputError

if TYPE_CHECKING:
    from syn_client.governance import GovernedPolicyRecord

    from potbot.security.broker import NativeIdentityBroker


def sign_governance_approval(
    broker: NativeIdentityBroker,
    vault: Path,
    proposal: GovernedPolicyRecord,
    *,
    expected_proposal_digest: str,
    expected_node_hash: str,
    request_id: str,
    trusted_history: tuple[GovernedPolicyRecord, ...] = (),
    expected_genesis_digest: str | None = None,
    expected_predecessor_digest: str | None = None,
    existing_policy_digest: str | None = None,
) -> dict[str, Any]:
    """Review/pin the proposal independently before asking the assigned native signer.

    A transition verifies the caller's retained history/genesis/head first. A
    genesis uses an explicitly reviewed proposal digest, before proofs exist.
    This only produces consent; admission and all other approvals remain required.
    """
    from syn_client.consensus import NodeKeyHash, PolicyError, digest_text
    from syn_client.custody import verify_agent_approval
    from syn_client.governance import required_approval_authority, verified_chain
    from syn_client.protocol import uuid_text

    try:
        digest_text(expected_proposal_digest)
        NodeKeyHash(expected_node_hash)
        uuid_text(request_id)
        if proposal.proposal_digest != expected_proposal_digest:
            raise PolicyError("Governance proposal differs from independently reviewed bytes")
        if trusted_history:
            if expected_genesis_digest is None or expected_predecessor_digest is None:
                raise PolicyError(
                    "Transition signing requires independent genesis and predecessor pins"
                )
            verified_chain(
                trusted_history,
                expected_workspace=proposal.state.workspace_id,
                expected_catalog=proposal.state.release.catalog_id,
                expected_genesis_digest=expected_genesis_digest,
                expected_head_digest=expected_predecessor_digest,
            )
            previous = trusted_history[-1]
        else:
            previous = None
            if expected_genesis_digest is not None or expected_predecessor_digest is not None:
                raise PolicyError(
                    "Genesis signing uses a reviewed proposal, not a fabricated history"
                )
        authority = required_approval_authority(proposal, previous, existing_policy_digest)
        membership = next(
            (m for m in authority.memberships() if m["agent"]["signer_hash"] == expected_node_hash),
            None,
        )
        if membership is None:
            raise PolicyError("Assigned node is outside the existing approval authority")
        payload = proposal.approval_payload(authority, existing_policy_digest)
        request = dict(
            group_id=membership["group_id"],
            group_epoch=membership["group_epoch"],
            request_id=request_id,
            payload_sha256=payload,
            validity_seconds=300,
        )
        proof = broker.sign_agent(
            vault,
            request,
            expected_operator_hash=membership["operator"]["signer_hash"],
            expected_agent_hash=expected_node_hash,
        )
        verify_agent_approval(
            proof, membership=membership, payload_digest=payload, now=int(time.time())
        )
        return proof
    except (PolicyError, ValueError) as error:
        raise InvalidInputError(
            "Governance approval held: check reviewed proposal and existing authority"
        ) from error
