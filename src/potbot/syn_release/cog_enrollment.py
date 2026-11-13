"""Public Cog grant binding from a release-verified Syn Agent attachment."""

from typing import Any

from potbot.domain.serialization import content_hash


def cog_enrollment_sha256(proof: dict[str, Any]) -> str:
    """Bind stable enrollment fields and the current signed group epoch."""
    status = proof["status"]
    membership = status["group_certificate"]["body"]
    return content_hash(
        {
            "profile": "potbot.syn-cog-enrollment/1",
            "assignment_sha256": status["assignment_sha256"],
            "operator": membership["operator_public_key_hash"],
            "network_id": membership["network_id"],
            "group_id": membership["group_id"],
            "group_epoch": membership["group_epoch"],
            "group_key_commitment": membership["group_key_commitment"],
            "node": membership["node_public_key_hash"],
        }
    )
