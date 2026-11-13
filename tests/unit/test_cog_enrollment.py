from copy import deepcopy

import pytest

from potbot.syn_release.cog_enrollment import cog_enrollment_sha256


@pytest.fixture
def attachment():
    return {
        "status": {
            "boot_id": "first",
            "assignment_sha256": "a" * 64,
            "group_certificate": {
                "body": {
                    "operator_public_key_hash": "b" * 64,
                    "network_id": "fixture",
                    "group_id": "d84f968d-2717-4412-bbe5-90af83572b62",
                    "group_epoch": 1,
                    "group_key_commitment": "c" * 64,
                    "node_public_key_hash": "d" * 64,
                },
            },
        },
    }


def test_grant_pin_survives_boot_but_changes_on_enrollment_rollover(attachment):
    original = cog_enrollment_sha256(attachment)
    restarted = deepcopy(attachment)
    restarted["status"]["boot_id"] = "second"
    assert cog_enrollment_sha256(restarted) == original
    restarted["status"]["group_certificate"]["body"]["group_epoch"] += 1
    assert cog_enrollment_sha256(restarted) != original
    reassigned = deepcopy(attachment)
    reassigned["status"]["assignment_sha256"] = "e" * 64
    assert cog_enrollment_sha256(reassigned) != original
