"""Consume actual public DAG proofs and hold native consent in the BFT role."""

import json
from io import BytesIO
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
    Path(__file__).parents[2]
    / "vendor/syn/packages/syn-client/tests/fixtures/federated-governed-chain.json"
)


def records():
    data = json.loads(FIXTURE.read_bytes())
    return data, tuple(GovernedPolicyRecord.from_dict(r) for r in data["records"])


@pytest.mark.parametrize("attack", [None, "incomplete", "signature-count", "governors", "profile"])
def test_http_consumer_replays_old_epoch_finality_before_resolving_types(attack):
    data, _ = records()
    successor = data["records"][1]
    old_policy = data["records"][0]["state"]["bindings"][0]["policy_digest"]
    proof = successor["approvals"]["existing_policies"][old_policy]
    if attack == "incomplete":
        proof["events"] = proof["events"][:4]
    elif attack == "signature-count":
        successor["approvals"]["existing_policies"][old_policy] = successor["approvals"][
            "type_governance"
        ]
    elif attack == "governors":
        successor["approvals"]["type_governance"] = []
    elif attack == "profile":
        proof["algorithm"] = "quorum-signatures-are-not-hashgraph"
    root, head = (GovernedPolicyRecord.from_dict(r) for r in data["records"])
    client = SynClient("http://127.0.0.1", "synthetic-" + "x" * 40)
    body = canonical_bytes({"profile": "syn.governance-chain/1", "records": data["records"]})
    options = dict(
        workspace_id=root.state.workspace_id,
        catalog_id=root.state.release.catalog_id,
        expected_genesis_digest=root.digest,
        # Even a caller who pins an invalid final document cannot skip replay.
        expected_head_digest=head.digest,
    )
    with patch.object(client._opener, "open", return_value=BytesIO(body)):
        if attack is None:
            assert governed_policy_resolver(client, **options).release == head.state.release
        else:
            with pytest.raises(InvalidInputError):
                governed_policy_resolver(client, **options)


@pytest.mark.parametrize("policy_consent", [False, True])
def test_protected_signer_separates_type_governance_from_federated_finality(
    tmp_path, policy_consent
):
    _, (root, proposal) = records()
    proof = load_document(proposal.approvals_json)["type_governance"][0]
    broker = Mock(spec=NativeIdentityBroker)
    broker.sign_agent.return_value = proof
    node = proof["membership"]["agent"]["signer_hash"]
    policy = None
    if policy_consent:
        policy = root.state.bindings[0].policy_digest
        node = root.state.policy_authorities[0].memberships()[0]["agent"]["signer_hash"]
    options = dict(
        expected_proposal_digest=proposal.proposal_digest,
        expected_node_hash=node,
        request_id=proof["request_id"],
        trusted_history=(root,),
        expected_genesis_digest=root.digest,
        expected_predecessor_digest=root.digest,
        existing_policy_digest=policy,
    )
    with patch("potbot.security.governance.time.time", return_value=proof["issued_at"]):
        if policy_consent:
            with pytest.raises(InvalidInputError):
                sign_governance_approval(broker, tmp_path / "assigned.vault", proposal, **options)
            broker.sign_agent.assert_not_called()
        else:
            assert (
                sign_governance_approval(broker, tmp_path / "assigned.vault", proposal, **options)
                == proof
            )
            assert broker.sign_agent.call_args.kwargs["expected_agent_hash"] == node
