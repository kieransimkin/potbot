"""Optional Syn policy consumer; resolution never activates a consensus engine.

Legacy SynSnapshot v1 and its fingerprints remain unchanged. Shared processing
requires the separate canonical Type release and independently selected pins.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from potbot.domain.errors import InvalidInputError

if TYPE_CHECKING:
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

    First-release trust selection is explicit. Governance-approved transitions,
    sealed database persistence and consensus execution remain later work.
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


def resolve_policy_write_set(
    resolver: CanonicalTypePolicyResolver, pins: tuple[TypePolicyPin, ...]
) -> tuple[PolicyContext, ...]:
    """Check all operation/subject Type pins from the admitted write contract."""
    from syn_client.consensus import PolicyError

    try:
        return resolver.resolve_write_set(pins)
    except PolicyError as error:
        raise InvalidInputError(str(error)) from error


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
