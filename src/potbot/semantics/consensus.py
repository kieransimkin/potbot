"""Optional Syn policy consumer; resolution never activates a consensus engine.

Legacy SynSnapshot v1 and its fingerprints remain unchanged. Shared processing
requires the separate canonical Type release and independently selected pins.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from potbot.domain.errors import InvalidInputError

if TYPE_CHECKING:
    from syn_client import SynClient
    from syn_client.consensus import (
        CanonicalTypePolicyResolver,
        NodeIdentity,
        PolicyBinding,
        PolicyContext,
        TypePolicyPin,
    )


def pinned_policy_resolver(
    release_json: str,
    *,
    expected_catalog_id: str,
    expected_release_digest: str,
    bindings: tuple[PolicyBinding, ...],
) -> CanonicalTypePolicyResolver:
    """Use caller-trusted pins, never hashes taken from the received document.

    First-release trust selection is explicit. Governance-approved transitions
    and consensus execution remain later work.
    """
    from syn_client.consensus import (
        CanonicalTypePolicyResolver,
        CanonicalTypeRelease,
        PolicyError,
    )

    try:
        return CanonicalTypePolicyResolver(
            CanonicalTypeRelease.from_json(release_json),
            expected_catalog_id,
            expected_release_digest,
            bindings,
        )
    except PolicyError as error:
        raise InvalidInputError(str(error)) from error


def adopted_policy_resolver(
    client: SynClient, *, workspace_id: str, catalog_id: str, expected_adoption_digest: str
) -> CanonicalTypePolicyResolver:
    """Resolve Syn's durable genesis using independently provisioned trust pins."""
    from syn_client import SynError
    from syn_client.consensus import PolicyError

    try:
        document = client.policy_adoption(
            workspace_id, catalog_id=catalog_id, expected_adoption_digest=expected_adoption_digest
        )
        return document.resolver(
            expected_workspace_id=workspace_id,
            expected_catalog_id=catalog_id,
            expected_adoption_digest=expected_adoption_digest,
        )
    except (PolicyError, SynError, ValueError) as error:
        raise InvalidInputError("Pinned Syn policy adoption is unavailable or invalid") from error


def resolve_policy_write_set(
    resolver: CanonicalTypePolicyResolver, pins: tuple[TypePolicyPin, ...]
) -> tuple[PolicyContext, ...]:
    """Check all operation/subject Type pins from the admitted write contract."""
    from syn_client.consensus import PolicyError

    try:
        return resolver.resolve_write_set(pins)
    except PolicyError as error:
        raise InvalidInputError(str(error)) from error


def governed_policy_resolver(
    client: SynClient,
    *,
    workspace_id: str,
    catalog_id: str,
    expected_genesis_digest: str,
    expected_head_digest: str,
) -> CanonicalTypePolicyResolver:
    """Verify the signed history without adopting a server-selected latest head."""
    from syn_client import SynError
    from syn_client.consensus import PolicyError
    from syn_client.governance import verified_chain

    try:
        chain = client.governed_policy_chain(
            workspace_id,
            catalog_id=catalog_id,
            expected_genesis_digest=expected_genesis_digest,
            expected_head_digest=expected_head_digest,
        )
        return verified_chain(
            chain,
            expected_workspace=workspace_id,
            expected_catalog=catalog_id,
            expected_genesis_digest=expected_genesis_digest,
            expected_head_digest=expected_head_digest,
        )
    except (PolicyError, SynError, ValueError) as error:
        raise InvalidInputError("Pinned signed Syn governance is unavailable or invalid") from error


def node_identity_from_membership(
    certificate: dict[str, Any], *, expected_issuer_hash: str, expected_node_hash: str
) -> NodeIdentity:
    """Verify an assignment certificate; fresh live possession is a later gate."""
    from syn_client.consensus import NodeIdentity, NodeKeyHash

    from potbot.security.provisioning import verify_membership
    from potbot.security.public import verify_public_identity

    checked = verify_membership(
        certificate,
        expected_operator_hash=expected_issuer_hash,
        expected_agent_hash=expected_node_hash,
    )
    identity = verify_public_identity(checked["agent"], expected_signer_hash=expected_node_hash)
    return NodeIdentity(NodeKeyHash(identity.signer_hash), identity.public_key)
