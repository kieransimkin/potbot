"""Public-only broker frames and retained-policy checks for protected DAG signing."""

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
from syn_client.consensus import PolicyError
from syn_client.governance import GovernedPolicyRecord

from potbot.domain.errors import InvalidInputError
from potbot.security.broker import NativeBrokerError, NativeIdentityBroker
from potbot.security.dag import prepare_governance_dag_event

FIXTURES = Path(__file__).parents[2] / "vendor/syn/packages/syn-client/tests/fixtures"


@pytest.fixture
def broker(monkeypatch):
    instance = NativeIdentityBroker()
    monkeypatch.setattr(instance, "_require_ready", Mock())
    monkeypatch.setattr(instance, "_run", Mock())
    return instance


@pytest.fixture
def event():
    return json.loads((FIXTURES / "protected-native-dag-event.json").read_bytes())


def test_broker_prepares_reserves_commits_and_exports_only_public_frames(broker, event, tmp_path):
    receipt = event["reservation"]
    intent = receipt["intent"]
    member = event["proof"]["membership"]
    agent, operator, anchor = (
        tmp_path / name for name in ("agent.vault", "operator.vault", "anchor.journal")
    )
    broker._run.return_value = (0, intent)
    assert broker.prepare_dag_event(agent, event["body"], membership=member) == intent
    assert broker._run.call_args.args[0][0] == "agent-dag-prepare"
    assert json.loads(broker._run.call_args.kwargs["public_input"]) == event["body"]
    broker._run.return_value = (0, receipt)
    assert broker.reserve_dag_event(operator, anchor, intent, membership=member) == receipt
    assert broker._run.call_args.args[0][0] == "dag-anchor-reserve"
    broker._run.return_value = (0, event)
    assert broker.commit_dag_event(agent, receipt, membership=member) == event
    assert broker._run.call_args.args[0][0] == "agent-dag-commit"
    broker._run.return_value = (0, receipt)
    assert (
        broker.export_dag_reservation(
            operator,
            anchor,
            membership=member,
            context_digest=intent["context_digest"],
            sequence=intent["sequence"],
        )
        == receipt
    )
    broker._run.return_value = (
        0,
        dict(
            schema_version=1,
            profile="syn-dag-anchor-ready-v1",
            issuer_hash=member["operator"]["signer_hash"],
            ledger_digest="a" * 64,
        ),
    )
    broker.initialize_dag_anchor(
        operator, anchor, expected_operator_hash=member["operator"]["signer_hash"]
    )
    # Python never creates, opens or receives the private vault/journal files.
    assert not any(p.exists() for p in (agent, operator, anchor))


@pytest.mark.parametrize(
    "attack", ["intent-header", "receipt-signature", "event-body", "export-slot"]
)
def test_broker_rejects_tampered_or_unrelated_public_responses(broker, event, tmp_path, attack):
    member = event["proof"]["membership"]
    receipt = event["reservation"]
    if attack == "intent-header":
        wrong = deepcopy(event["body"])
        wrong["timestamp_ms"] += 1
        broker._run.return_value = (0, receipt["intent"])
        with pytest.raises(NativeBrokerError):
            broker.prepare_dag_event(tmp_path / "agent", wrong, membership=member)
    elif attack == "receipt-signature":
        wrong = deepcopy(receipt)
        wrong["signature"] = "A" * 86 + "=="
        with pytest.raises(PolicyError):
            broker.commit_dag_event(tmp_path / "agent", wrong, membership=member)
        broker._run.assert_not_called()
    elif attack == "event-body":
        wrong = deepcopy(event)
        wrong["body"]["timestamp_ms"] += 1
        broker._run.return_value = (0, wrong)
        with pytest.raises(PolicyError):
            broker.commit_dag_event(tmp_path / "agent", receipt, membership=member)
    else:
        broker._run.return_value = (0, receipt)
        with pytest.raises(NativeBrokerError):
            broker.export_dag_reservation(
                tmp_path / "operator",
                tmp_path / "anchor",
                membership=member,
                context_digest=receipt["intent"]["context_digest"],
                sequence="2",
            )


@pytest.mark.parametrize(
    "attack", [None, "head", "genesis", "policy", "node", "candidate", "other-parent"]
)
def test_policy_bound_preparation_holds_before_signing_on_bad_history_or_scope(tmp_path, attack):
    data = json.loads((FIXTURES / "protected-governed-chain.json").read_bytes())
    root, proposal = (GovernedPolicyRecord.from_dict(r) for r in data["records"])
    evidence = next(iter(json.loads(proposal.approvals_json)["existing_policies"].values()))
    event = evidence["events"][0]
    body = deepcopy(event["body"])
    broker = Mock(spec=NativeIdentityBroker)
    broker.prepare_dag_event.return_value = event["reservation"]["intent"]
    options = dict(
        trusted_history=(root,),
        expected_genesis_digest=root.digest,
        expected_head_digest=root.digest,
        existing_policy_digest=root.state.bindings[0].policy_digest,
        expected_node_hash=body["creator"],
    )
    if attack == "head":
        options["expected_head_digest"] = "f" * 64
    elif attack == "genesis":
        options["expected_genesis_digest"] = "f" * 64
    elif attack == "policy":
        options["existing_policy_digest"] = "f" * 64
    elif attack == "node":
        options["expected_node_hash"] = "f" * 64
    elif attack == "candidate":
        body["proposal"]["previous_record_digest"] = "f" * 64
    elif attack == "other-parent":
        body["other_parent"] = "f" * 64
    if attack is None:
        assert (
            prepare_governance_dag_event(broker, tmp_path / "agent", body, **options)
            == event["reservation"]["intent"]
        )
        assert (
            broker.prepare_dag_event.call_args.kwargs["membership"] == event["proof"]["membership"]
        )
    else:
        with pytest.raises(InvalidInputError):
            prepare_governance_dag_event(broker, tmp_path / "agent", body, **options)
        broker.prepare_dag_event.assert_not_called()


@pytest.mark.parametrize("wrong_hash", [False, True])
def test_policy_preparation_authenticates_the_other_parent(tmp_path, wrong_hash):
    from syn_client.dag_finality import event_digest

    data = json.loads((FIXTURES / "protected-governed-chain.json").read_bytes())
    root, proposal = (GovernedPolicyRecord.from_dict(r) for r in data["records"])
    events = next(iter(json.loads(proposal.approvals_json)["existing_policies"].values()))["events"]
    event = next(e for e in events if e["body"]["other_parent"] is not None)
    parent = next(p for p in events if event_digest(p) == event["body"]["other_parent"])
    body = deepcopy(event["body"])
    if wrong_hash:
        body["other_parent"] = "f" * 64
    broker = Mock(spec=NativeIdentityBroker)
    options = dict(
        trusted_history=(root,),
        expected_genesis_digest=root.digest,
        expected_head_digest=root.digest,
        existing_policy_digest=root.state.bindings[0].policy_digest,
        expected_node_hash=body["creator"],
        other_parent_event=parent,
    )
    if wrong_hash:
        with pytest.raises(InvalidInputError):
            prepare_governance_dag_event(broker, tmp_path / "agent", body, **options)
        broker.prepare_dag_event.assert_not_called()
    else:
        prepare_governance_dag_event(broker, tmp_path / "agent", body, **options)
        broker.prepare_dag_event.assert_called_once()
