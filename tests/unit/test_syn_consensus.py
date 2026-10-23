"""Actual shared-client consumer with frozen native public membership evidence."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from syn_client.consensus import (
    CanonicalTypeDefinition,
    CanonicalTypeRelease,
    ConsensusPolicy,
    PolicyBinding,
    TypePolicyPin,
)

from potbot.domain.errors import InvalidInputError
from potbot.semantics.consensus import (
    adopted_policy_resolver,
    node_identity_from_membership,
    pinned_policy_resolver,
    resolve_policy_write_set,
)

CATALOG = "11111111-1111-4111-8111-111111111111"
DOMAIN = "22222222-2222-4222-8222-222222222222"
STREAM = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def selected():
    policy = ConsensusPolicy.from_dict(
        {
            "profile": "syn.consensus-policy/1",
            "policy_revision": "1",
            "protocol": "syn.operator-log/1",
            "custody_constraint": "same-verified-issuer",
            "membership_anchor_digest": "a" * 64,
            "participant_reference": "node-public-key-sha256",
            "quorum_rule": "strict-majority",
            "fault_model": "crash-and-partition",
            "proof_profile": "syn.operator-commit/1",
            "conflict_namespace": "tasks",
            "change_rule": "current-policy-and-type-governance",
        }
    )
    definition = CanonicalTypeDefinition("syn://type/task", "1", '{"name":"Task"}', policy)
    release = CanonicalTypeRelease(CATALOG, "1", (definition,))
    binding = PolicyBinding(policy.digest, DOMAIN, "b" * 64, STREAM, "1", "a" * 64)
    return release, binding, TypePolicyPin(definition.type_ref, definition.digest, policy.digest)


def test_potbot_resolves_exact_shared_contract_without_enabling_a_backend(selected):
    release, binding, pin = selected
    resolver = pinned_policy_resolver(
        release.to_json(),
        expected_catalog_id=CATALOG,
        expected_release_digest=release.digest,
        bindings=(binding,),
    )
    (context,) = resolve_policy_write_set(resolver, (pin,))
    assert context.binding.stream_id == STREAM
    assert context.proof_profile == "syn.operator-commit/1"
    with pytest.raises(InvalidInputError, match="pin mismatch"):
        resolve_policy_write_set(resolver, (replace(pin, consensus_policy_digest="f" * 64),))


def test_remote_document_cannot_choose_its_own_trust_pin(selected):
    release, binding, _ = selected
    for changes in ({"expected_catalog_id": DOMAIN}, {"expected_release_digest": "f" * 64}):
        options = dict(
            expected_catalog_id=CATALOG, expected_release_digest=release.digest, bindings=(binding,)
        )
        options.update(changes)
        with pytest.raises(InvalidInputError):
            pinned_policy_resolver(release.to_json(), **options)


def test_durable_syn_adoption_uses_independent_workspace_and_adoption_pin(selected):
    from unittest.mock import Mock

    from syn_client import SynClient
    from syn_client.policy_adoption import GenesisPolicyAdoption

    release, binding, pin = selected
    document = GenesisPolicyAdoption(STREAM, release, (binding,))
    client = Mock(spec=SynClient)
    client.policy_adoption.return_value = document
    resolver = adopted_policy_resolver(
        client, workspace_id=STREAM, catalog_id=CATALOG, expected_adoption_digest=document.digest
    )
    assert resolver.resolve(pin).binding == binding
    client.policy_adoption.assert_called_once_with(
        STREAM, catalog_id=CATALOG, expected_adoption_digest=document.digest
    )
    for options in ({"workspace_id": DOMAIN}, {"expected_adoption_digest": "f" * 64}):
        with pytest.raises(InvalidInputError):
            adopted_policy_resolver(
                client,
                **dict(
                    workspace_id=STREAM,
                    catalog_id=CATALOG,
                    expected_adoption_digest=document.digest,
                )
                | options,
            )
    from syn_client import SynError

    client.policy_adoption.side_effect = SynError("unavailable")
    with pytest.raises(InvalidInputError):
        adopted_policy_resolver(
            client,
            workspace_id=STREAM,
            catalog_id=CATALOG,
            expected_adoption_digest=document.digest,
        )


def test_node_reference_matches_existing_native_account_fingerprint():
    certificate = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/security/native-agent-public-proof.json"
        ).read_bytes()
    )["membership"]
    issuer = certificate["operator"]["signer_hash"]
    node = certificate["agent"]["signer_hash"]
    identity = node_identity_from_membership(
        certificate, expected_issuer_hash=issuer, expected_node_hash=node
    )
    assert identity.node_key_hash == node
    assert identity.node_key_hash != issuer
    from potbot.security.public import PublicVerificationError

    with pytest.raises(PublicVerificationError):
        node_identity_from_membership(
            certificate, expected_issuer_hash=issuer, expected_node_hash=issuer
        )
    certificate["account_number"] += 1
    with pytest.raises(PublicVerificationError):
        node_identity_from_membership(
            certificate, expected_issuer_hash=issuer, expected_node_hash=node
        )
