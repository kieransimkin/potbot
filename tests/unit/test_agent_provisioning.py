"""Independent public verifier and secret-free native provisioning command boundary."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from potbot.security.broker import NativeBrokerError, NativeIdentityBroker
from potbot.security.cli import main
from potbot.security.provisioning import validate_assignment, verify_agent_proof, verify_membership
from potbot.security.public import (
    PublicVerificationError,
    canonical_json_bytes,
    public_identity_descriptor,
)

GROUP = "11111111-1111-4111-8111-111111111111"
INSTANCE = "22222222-2222-4222-8222-222222222222"
NOW = 1792584000


def test_native_public_proof_fixture_with_independent_python_verifier():
    import hashlib
    import json

    path = Path(__file__).parents[1] / "fixtures/security/native-agent-public-proof.json"
    proof = json.loads(path.read_bytes())
    operator = "28996c77aba50985b85b6c81f0e60d5b405b81a45efdafb3fda5d9f8581720e8"
    agent = "99a54f2ee8f56ca9ad1a2d3c583bb9f55e07592daeef9b1be8a7fa517d0048d1"
    assert (
        verify_agent_proof(
            proof,
            expected_operator_hash=operator,
            expected_agent_hash=agent,
            group_id=GROUP,
            group_epoch=0,
            payload_sha256=hashlib.sha256(b"synthetic fixture").hexdigest(),
            now=NOW,
        )
        == proof
    )
    assert set(proof["membership"]["agent"]) == set(proof["membership"]["operator"])


def signed(value, key, prefix):
    import base64

    unsigned = {name: item for name, item in value.items() if name != "signature"}
    value["signature"] = base64.b64encode(
        key.sign(prefix + b"\0" + canonical_json_bytes(unsigned))
    ).decode()
    return value


@pytest.fixture
def evidence():
    # Fixed public test keys; this fixture tests certificate verification, not HD derivation.
    owner = Ed25519PrivateKey.from_private_bytes(b"O" * 32)
    agent = Ed25519PrivateKey.from_private_bytes(b"A" * 32)
    descriptors = [
        public_identity_descriptor(
            key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        )
        for key in (owner, agent)
    ]
    member = signed(
        {
            "schema_version": 1,
            "profile": "syn-agent-account-v1",
            "operator": descriptors[0],
            "agent": descriptors[1],
            "account_number": 7,
            "instance_id": INSTANCE,
            "group_id": GROUP,
            "group_epoch": 0,
            "signature": "",
        },
        owner,
        b"syn-agent-membership-v1",
    )
    request = {
        "group_id": GROUP,
        "group_epoch": 0,
        "request_id": "33333333-3333-4333-8333-333333333333",
        "payload_sha256": "a" * 64,
        "validity_seconds": 120,
    }
    proof = signed(
        {
            "schema_version": 1,
            "profile": "syn-agent-proof-v1",
            "membership": member,
            "request_id": request["request_id"],
            "payload_sha256": request["payload_sha256"],
            "issued_at": NOW,
            "expires_at": NOW + 120,
            "signature": "",
        },
        agent,
        b"syn-agent-proof-v1",
    )
    return member, proof, request


def test_independent_pinned_membership_and_account_proof(evidence):
    member, proof, request = evidence
    operator = member["operator"]["signer_hash"]
    agent = member["agent"]["signer_hash"]
    assert (
        verify_membership(member, expected_operator_hash=operator, expected_agent_hash=agent)
        == member
    )
    assert (
        verify_agent_proof(
            proof,
            expected_operator_hash=operator,
            expected_agent_hash=agent,
            group_id=GROUP,
            group_epoch=0,
            payload_sha256=request["payload_sha256"],
            now=NOW,
        )
        == proof
    )
    for change in ({"expected_operator_hash": "0" * 64}, {"expected_agent_hash": "0" * 64}):
        with pytest.raises(PublicVerificationError):
            verify_membership(
                member,
                **{"expected_operator_hash": operator, "expected_agent_hash": agent, **change},
            )


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("profile", "future"),
        ("account_number", True),
        ("account_number", -1),
        ("account_number", 2**31),
        ("group_epoch", True),
        ("group_epoch", 2**31),
        ("instance_id", "not-uuid"),
        ("group_id", "not-uuid"),
        ("private_key", "must not cross"),
        ("signature", "invalid"),
    ],
)
def test_membership_rejects_scope_forgery_extensions_and_invalid_numbers(evidence, field, value):
    member = deepcopy(evidence[0])
    member[field] = value
    with pytest.raises(PublicVerificationError):
        verify_membership(member, expected_operator_hash=member["operator"]["signer_hash"])


@pytest.mark.parametrize(
    "change",
    [
        {"group_epoch": 1},
        {"group_id": INSTANCE},
        {"payload_sha256": "b" * 64},
        {"now": NOW - 1},
        {"now": NOW + 120},
    ],
)
def test_proof_binds_group_current_epoch_payload_and_time(evidence, change):
    member, proof, request = evidence
    arguments = {
        "expected_operator_hash": member["operator"]["signer_hash"],
        "expected_agent_hash": member["agent"]["signer_hash"],
        "group_id": GROUP,
        "group_epoch": 0,
        "payload_sha256": request["payload_sha256"],
        "now": NOW,
    }
    with pytest.raises(PublicVerificationError):
        verify_agent_proof(proof, **{**arguments, **change})


def test_native_adapter_passes_public_assignment_only_and_validates_response(evidence, tmp_path):
    member, _proof, _request = evidence
    broker = NativeIdentityBroker()
    broker._require_ready = Mock()
    broker._run = Mock(return_value=(0, member))
    root, agent = tmp_path / "operator.json", tmp_path / "agent.json"
    result = broker.provision_agent(
        root,
        agent,
        expected_operator_hash=member["operator"]["signer_hash"],
        account_number=7,
        instance_id=INSTANCE,
        group_id=GROUP,
        group_epoch=0,
    )
    assert result == member
    arguments = broker._run.call_args.args[0]
    assert arguments[:5] == ["agent-provision", "--vault", str(root), "--agent-vault", str(agent)]
    assert broker._run.call_args.kwargs == {}
    assert "--password-fd" not in arguments
    assert (
        not root.exists() and not agent.exists()
    )  # Python never opens or creates a private vault.
    forged = {**member, "privateData": "private-output"}
    broker._run.return_value = (0, forged)
    with pytest.raises(PublicVerificationError):
        broker.provision_agent(
            root,
            agent,
            expected_operator_hash=member["operator"]["signer_hash"],
            account_number=7,
            instance_id=INSTANCE,
            group_id=GROUP,
            group_epoch=0,
        )


def test_cli_requires_explicit_account_and_protects_both_vault_paths(
    evidence, tmp_path, monkeypatch, capsys
):
    member = evidence[0]
    instance = Mock(spec=NativeIdentityBroker)
    instance.provision_agent.return_value = member
    monkeypatch.setattr("potbot.security.cli.NativeIdentityBroker", lambda _: instance)
    operator, agent = tmp_path / "operator.json", tmp_path / "agent.json"
    args = [
        "agent-provision",
        "--vault",
        str(operator),
        "--agent-vault",
        str(agent),
        "--expected-operator-hash",
        member["operator"]["signer_hash"],
        "--account-number",
        "7",
        "--instance-id",
        INSTANCE,
        "--group-id",
        GROUP,
        "--group-epoch",
        "0",
    ]
    assert main(args) == 0
    instance.provision_agent.assert_called_once_with(
        operator,
        agent,
        expected_operator_hash=member["operator"]["signer_hash"],
        account_number=7,
        instance_id=INSTANCE,
        group_id=GROUP,
        group_epoch=0,
    )
    capsys.readouterr()
    instance.reset_mock()
    assert main([*args, "--output", str(agent)]) == 2
    instance.provision_agent.assert_not_called()
    assert "private-output" not in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(args[: args.index("--account-number")] + args[args.index("--instance-id") :])


def test_restricted_account_scope_rejects_unknown_private_fields():
    with pytest.raises(PublicVerificationError):
        validate_assignment(
            {
                "account_number": 7,
                "instance_id": INSTANCE,
                "group_id": GROUP,
                "group_epoch": 0,
                "base_private_key": "never allowed",
            }
        )


def test_broker_refuses_a_valid_proof_for_a_different_request(evidence, tmp_path, monkeypatch):
    member, proof, request = evidence
    monkeypatch.setattr("potbot.security.provisioning.time.time", lambda: NOW)
    broker = NativeIdentityBroker()
    broker._require_ready = Mock()
    broker._run = Mock(return_value=(0, proof))
    pins = {
        "expected_operator_hash": member["operator"]["signer_hash"],
        "expected_agent_hash": member["agent"]["signer_hash"],
    }
    assert broker.sign_agent(tmp_path / "agent.json", request, **pins) == proof
    with pytest.raises(NativeBrokerError):
        broker.sign_agent(
            tmp_path / "agent.json",
            {**request, "request_id": "44444444-4444-4444-8444-444444444444"},
            **pins,
        )
    broker._run.return_value = (0, member)
    with pytest.raises(NativeBrokerError):
        broker.rotate_agent_group(
            tmp_path / "operator.json", tmp_path / "agent.json", group_epoch=1, **pins
        )  # A valid certificate for epoch 0 cannot acknowledge epoch 1.
    broker._run.reset_mock()
    with pytest.raises(NativeBrokerError):
        broker.agent_public(
            tmp_path / "agent.json",
            expected_operator_hash=pins["expected_operator_hash"],
            unlock=True,
        )
    broker._run.assert_not_called()


def test_allocation_inventory_rejects_hidden_records_duplicates_and_bad_pagination(
    evidence, tmp_path
):
    member = evidence[0]
    pin = member["operator"]["signer_hash"]
    item = {
        "account_number": 7,
        "instance_id": INSTANCE,
        "group_id": GROUP,
        "group_epoch": 0,
        "agent_fingerprint": member["agent"]["signer_hash"],
    }
    page = {
        "schema_version": 1,
        "operator_fingerprint": pin,
        "generation": 2,
        "total": 1,
        "offset": 0,
        "next_offset": 1,
        "has_more": False,
        "allocations": [item],
    }
    broker = NativeIdentityBroker()
    broker._require_ready = Mock()
    broker._run = Mock(return_value=(0, page))
    assert broker.agent_allocations(tmp_path / "operator.json", expected_operator_hash=pin) == page
    for changed in (
        {"next_offset": 0},
        {"has_more": True},
        {"operator_fingerprint": "0" * 64},
        {"allocations": [{**item, "base_private_key": "untrusted extra field"}]},
        {"allocations": [item, item], "total": 2, "next_offset": 2},
    ):
        broker._run.return_value = (0, {**page, **changed})
        with pytest.raises(NativeBrokerError):
            broker.agent_allocations(tmp_path / "operator.json", expected_operator_hash=pin)
