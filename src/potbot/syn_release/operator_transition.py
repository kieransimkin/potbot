"""Select a reviewed operator package while retaining Syn's anchored live state."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.syn_release.contracts import Policy, canonical, digest, fields, require_retained_policy
from potbot.syn_release.operator import SynOperatorDeployment
from potbot.syn_release.store import _sync_directory, _write_new

# Exact signed manifests. 0.2.1 cannot validate the adoption journal introduced
# in 0.3.0. 0.3.1 and 0.4.0 use the same operator allocation and adoption
# formats. This allowlist applies only to operator mode.
OPERATOR_021 = "8455cc21a8fc320d5522f88a873e9b5575aa4379d9c2e65c60cf21f9f4d28d46"
OPERATOR_031 = "10da08e7b0af7d32c48a3087e873c2e448e846e2e3aac6ad56df81330267b4f1"
OPERATOR_040 = "a406356fba844a28c04870e45299ccd83e406b5dbe8b7a55741db6d5a6cd1088"
SUPPORTED = {
    (OPERATOR_021, OPERATOR_031),
    (OPERATOR_031, OPERATOR_040),
    (OPERATOR_040, OPERATOR_031),
}


def _preflight(deployment: SynOperatorDeployment, python: Path, record: dict[str, Any]) -> str:
    value = fields(
        deployment._public_command(
            python,
            [
                "-m",
                "syn.operator",
                "preflight",
                "--config",
                str(deployment.state_dir / "operator.json"),
            ],
        ),
        {
            "profile",
            "base_public_key_hash",
            "configuration_sha256",
            "state_sha256",
            "agent_activation_authorized",
        },
    )
    if (
        value["profile"] != "syn.operator-preflight/1"
        or value["base_public_key_hash"] != record["base_public_key_hash"]
        or value["configuration_sha256"] != record["configuration_sha256"]
        or value["agent_activation_authorized"] is not False
    ):
        raise StateMismatchError("Syn preflight differs from the retained operator registration")
    return digest(value["state_sha256"])


def switch_release(deployment: SynOperatorDeployment, target: Policy) -> dict[str, Any]:
    """Atomically select already staged code after independently verifying both packages."""
    target = Policy.parse(canonical(target.value))
    previous = deployment.policy
    require_retained_policy(previous, target)
    pair = previous.manifest_sha256, target.manifest_sha256
    if previous.value["mode"] != "operator" or pair not in SUPPORTED:
        raise ScopeDeniedError("Syn operator release pair requires separate state qualification")
    if previous.manifest_sha256 in target.value["revoked_manifests"]:
        raise ScopeDeniedError("A revoked Syn baseline requires separate recovery")
    selected = SynOperatorDeployment(deployment.store, target, deployment.root)
    endpoint = deployment.state_dir / "operator.sock"
    with deployment._supervisor_lock():
        if endpoint.exists() or endpoint.is_symlink():
            raise StateMismatchError("Stop the operator and reconcile its socket before switching")
        record = deployment._registered()
        current, current_python = deployment._installation()
        candidate, candidate_python = selected._installation()
        if (
            record["installation"] != current["directory"]
            or current["mode"] != "operator"
            or candidate["mode"] != "operator"
        ):
            raise StateMismatchError(
                "Both Syn installations must match the registered operator mode"
            )
        runtime = current["observed"]["runtime"]
        if runtime["database_revision"] != candidate["observed"]["runtime"]["database_revision"]:
            raise ScopeDeniedError("Syn operator switch requires independent database migration")
        before = _preflight(deployment, current_python, record)
        if _preflight(deployment, candidate_python, record) != before:
            raise StateMismatchError("Syn packages disagree on the anchored operator state")
        if endpoint.exists() or endpoint.is_symlink() or deployment._registered() != record:
            raise StateMismatchError("Operator state changed during release preflight")
        replacement = {
            **record,
            "installation": candidate["directory"],
            "manifest_sha256": target.manifest_sha256,
        }
        pending = deployment.root / (uuid4().hex + ".registration")
        try:
            _write_new(pending, canonical(replacement))
            os.replace(pending, deployment.registration)
            _sync_directory(deployment.root)
        finally:
            pending.unlink(missing_ok=True)
    return {
        "profile": "potbot.syn-operator-release-switch/1",
        "from_manifest_sha256": previous.manifest_sha256,
        "target_manifest_sha256": target.manifest_sha256,
        "base_public_key_hash": record["base_public_key_hash"],
        "state_sha256": before,
        "restart_required": True,
        "agent_activation_authorized": False,
    }
