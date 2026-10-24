"""Signed Syn history and the public-only native approval adapter."""

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from syn_client import SynClient
from syn_client.consensus import canonical_bytes, load_document
from syn_client.governance import GovernedPolicyRecord

from potbot.domain.errors import InvalidInputError
from potbot.security.broker import NativeIdentityBroker
from potbot.security.governance import sign_governance_approval
from potbot.semantics.consensus import governed_policy_resolver

FIXTURE = (
    Path(__file__).parents[2] / "vendor/syn/packages/syn-client/tests/fixtures/governed-chain.json"
)


@pytest.fixture
def chain():
    return tuple(
        GovernedPolicyRecord.from_dict(r) for r in json.loads(FIXTURE.read_bytes())["records"]
    )


def test_consumer_checks_genesis_head_and_each_old_authority(chain):
    root, head = chain
    client = Mock(spec=SynClient)
    client.governed_policy_chain.return_value = chain
    options = dict(
        workspace_id=root.state.workspace_id,
        catalog_id=root.state.release.catalog_id,
        expected_genesis_digest=root.digest,
        expected_head_digest=head.digest,
    )
    assert governed_policy_resolver(client, **options).release == head.state.release
    client.governed_policy_chain.return_value = (root,)
    with pytest.raises(InvalidInputError):
        governed_policy_resolver(client, **options)
    client.governed_policy_chain.return_value = chain
    for key in ("expected_genesis_digest", "expected_head_digest"):
        with pytest.raises(InvalidInputError):
            governed_policy_resolver(client, **(options | {key: "f" * 64}))


@pytest.mark.parametrize("role", ["type-governance", "existing-policy"])
def test_signer_selects_old_authority_and_returns_only_native_public_proof(chain, tmp_path, role):
    root, proposal = chain
    proofs = load_document(proposal.approvals_json)
    policy = (
        root.state.release.types[0].consensus_policy.digest if role == "existing-policy" else None
    )
    proof = proofs["existing_policies"][policy][0] if policy else proofs["type_governance"][0]
    node = proof["membership"]["agent"]["signer_hash"]
    broker = Mock(spec=NativeIdentityBroker)
    broker.sign_agent.return_value = proof
    with patch("potbot.security.governance.time.time", return_value=proof["issued_at"]):
        actual = sign_governance_approval(
            broker,
            tmp_path / "assigned.vault",
            proposal,
            expected_proposal_digest=proposal.proposal_digest,
            expected_node_hash=node,
            request_id=proof["request_id"],
            trusted_history=(root,),
            expected_genesis_digest=root.digest,
            expected_predecessor_digest=root.digest,
            existing_policy_digest=policy,
        )
    assert actual == proof
    args, kwargs = broker.sign_agent.call_args
    assert args[1]["payload_sha256"] == proof["payload_sha256"]
    assert kwargs["expected_agent_hash"] == node
    assert kwargs["expected_operator_hash"] == proof["membership"]["operator"]["signer_hash"]
    assert "private" not in canonical_bytes(actual).decode()


@pytest.mark.parametrize(
    "attack", ["changed-proposal", "new-governor", "missing-history", "bad-root", "bad-head"]
)
def test_invalid_approval_request_never_invokes_native_signer(chain, tmp_path, attack):
    root, proposal = chain
    proof = load_document(proposal.approvals_json)["type_governance"][0]
    broker = Mock(spec=NativeIdentityBroker)
    options = dict(
        expected_proposal_digest=proposal.proposal_digest,
        expected_node_hash=proof["membership"]["agent"]["signer_hash"],
        request_id=proof["request_id"],
        trusted_history=(root,),
        expected_genesis_digest=root.digest,
        expected_predecessor_digest=root.digest,
    )
    if attack == "changed-proposal":
        options["expected_proposal_digest"] = "f" * 64
    elif attack == "new-governor":
        options["expected_node_hash"] = proposal.state.type_governance.memberships()[0]["agent"][
            "signer_hash"
        ]
    elif attack == "missing-history":
        options.update(
            trusted_history=(), expected_genesis_digest=None, expected_predecessor_digest=None
        )
    else:
        options[
            "expected_genesis_digest" if attack == "bad-root" else "expected_predecessor_digest"
        ] = "f" * 64
    with pytest.raises(InvalidInputError):
        sign_governance_approval(broker, tmp_path / "assigned.vault", proposal, **options)
    broker.sign_agent.assert_not_called()


def test_explicitly_pinned_genesis_proposal_can_use_native_account_signer(chain, tmp_path):
    root = chain[0]
    proof = load_document(root.approvals_json)["type_governance"][0]
    broker = Mock(spec=NativeIdentityBroker)
    broker.sign_agent.return_value = proof
    with patch("potbot.security.governance.time.time", return_value=proof["issued_at"]):
        assert (
            sign_governance_approval(
                broker,
                tmp_path / "assigned.vault",
                root,
                expected_proposal_digest=root.proposal_digest,
                expected_node_hash=proof["membership"]["agent"]["signer_hash"],
                request_id=proof["request_id"],
            )
            == proof
        )
