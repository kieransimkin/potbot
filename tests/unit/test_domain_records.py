import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.domain import (
    ContentDigest,
    EnvironmentRef,
    ErrorCode,
    ErrorInfo,
    IndeterminateExecutionError,
    InvalidInputError,
    OperationCancelledError,
    OperationOutcome,
    OperationTimeoutError,
    OutcomeStatus,
    ProducerIdentity,
    ProducerKind,
    RecordId,
    RevisionConflictError,
    ScopeDeniedError,
)


def outcome(status: OutcomeStatus = OutcomeStatus.SUCCEEDED) -> OperationOutcome:
    start = datetime(2026, 10, 21, 10, 12, 13, 456789, timezone(timedelta(hours=1)))
    errors = {
        OutcomeStatus.FAILED: ErrorCode.INVALID_INPUT,
        OutcomeStatus.CANCELLED: ErrorCode.CANCELLED,
        OutcomeStatus.TIMED_OUT: ErrorCode.TIMEOUT,
        OutcomeStatus.INDETERMINATE: ErrorCode.INDETERMINATE,
    }
    return OperationOutcome(
        operation_id=uuid4(),
        status=status,
        producer=ProducerIdentity(uuid4(), ProducerKind.TOOL, "fixture compiler"),
        environment=EnvironmentRef(uuid4(), ContentDigest.from_bytes(b"environment")),
        started_at=start,
        observed_at=start + timedelta(seconds=2),
        finished_at=None if status is OutcomeStatus.INDETERMINATE else start + timedelta(seconds=1),
        result_digest=ContentDigest.from_bytes(b"result")
        if status is OutcomeStatus.SUCCEEDED
        else None,
        error=None
        if status is OutcomeStatus.SUCCEEDED
        else ErrorInfo(errors[status], "fixture outcome"),
    )


@pytest.mark.parametrize("status", list(OutcomeStatus))
def test_outcome_round_trip_preserves_identity_provenance_and_times(status: OutcomeStatus) -> None:
    original = outcome(status)
    restored = OperationOutcome.from_json(original.to_json())
    assert restored == original
    assert restored.started_at.isoformat() == original.started_at.isoformat()
    assert restored.started_at.microsecond == 456789
    assert restored.producer.kind is ProducerKind.TOOL
    assert restored.environment.manifest_digest == ContentDigest.from_bytes(b"environment")


@pytest.mark.parametrize("kind", list(ProducerKind))
def test_producer_distinctions_survive_serialization(kind: ProducerKind) -> None:
    producer = ProducerIdentity(
        uuid4(), kind, "producer", uuid4() if kind is ProducerKind.MODEL else None
    )
    assert ProducerIdentity.from_json(producer.to_json()) == producer


def test_record_id_digest_and_environment_round_trip_are_immutable() -> None:
    identifier = RecordId.new()
    digest = ContentDigest.from_bytes(b"abc")
    assert digest.value == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert RecordId.from_json(identifier.to_json()) == identifier
    assert ContentDigest.from_json(digest.to_json()) == digest
    environment = EnvironmentRef(identifier.value, digest)
    assert EnvironmentRef.from_json(environment.to_json()) == environment
    with pytest.raises(FrozenInstanceError):
        environment.environment_id = uuid4()  # type: ignore[misc]


@pytest.mark.parametrize("value", ["", "ab", "g" * 64, "A" * 64, 123, None])
def test_malformed_digest_is_classified_before_persistence(value: object) -> None:
    with pytest.raises(InvalidInputError) as exc:
        ContentDigest.from_dict({"schema_version": 1, "algorithm": "sha256", "value": value})
    assert exc.value.code is ErrorCode.INVALID_INPUT


@pytest.mark.parametrize("version", [0, 2, "1", True, 1.0, None])
def test_unknown_or_coerced_schema_versions_are_rejected(version: object) -> None:
    wire = outcome().to_dict()
    wire["schema_version"] = version
    with pytest.raises(InvalidInputError):
        OperationOutcome.from_dict(wire)


def test_nested_unknown_version_extra_fields_and_invalid_ids_are_rejected() -> None:
    wire = outcome().to_dict()
    wire["environment"]["schema_version"] = 99
    with pytest.raises(InvalidInputError):
        OperationOutcome.from_dict(wire)
    wire = outcome().to_dict()
    wire["verified"] = True
    with pytest.raises(InvalidInputError):
        OperationOutcome.from_dict(wire)
    wire = outcome().to_dict()
    wire["operation_id"] = "not-a-uuid"
    with pytest.raises(InvalidInputError):
        OperationOutcome.from_dict(wire)


def test_model_requires_deployment_and_other_producers_cannot_claim_one() -> None:
    with pytest.raises(InvalidInputError):
        ProducerIdentity(uuid4(), ProducerKind.MODEL, "model")
    with pytest.raises(InvalidInputError):
        ProducerIdentity(uuid4(), ProducerKind.HUMAN, "human", uuid4())


def test_success_and_error_cannot_coexist() -> None:
    with pytest.raises(InvalidInputError):
        replace(outcome(), error=ErrorInfo(ErrorCode.INVALID_INPUT, "failure"))
    with pytest.raises(InvalidInputError):
        replace(outcome(OutcomeStatus.FAILED), result_digest=ContentDigest.from_bytes(b"partial"))


def test_uncertainty_and_control_outcomes_are_not_mislabeled_as_failure() -> None:
    with pytest.raises(InvalidInputError):
        replace(outcome(OutcomeStatus.INDETERMINATE), finished_at=outcome().finished_at)
    with pytest.raises(InvalidInputError):
        replace(outcome(OutcomeStatus.FAILED), error=ErrorInfo(ErrorCode.TIMEOUT, "timeout"))
    with pytest.raises(InvalidInputError):
        replace(
            outcome(OutcomeStatus.CANCELLED), error=ErrorInfo(ErrorCode.INDETERMINATE, "uncertain")
        )


def test_naive_or_inconsistent_timestamps_fail() -> None:
    original = outcome()
    with pytest.raises(InvalidInputError):
        replace(original, started_at=original.started_at.replace(tzinfo=None))
    with pytest.raises(InvalidInputError):
        replace(original, observed_at=original.started_at - timedelta(seconds=1))
    with pytest.raises(InvalidInputError):
        replace(original, finished_at=original.observed_at + timedelta(seconds=1))


@pytest.mark.parametrize("payload", ["not json", "[]", "null", '{"schema_version":1}'])
def test_invalid_wire_payload_has_stable_error(payload: str) -> None:
    with pytest.raises(InvalidInputError) as exc:
        OperationOutcome.from_json(payload)
    assert exc.value.code is ErrorCode.INVALID_INPUT


@pytest.mark.parametrize(
    "error,code",
    [
        (InvalidInputError, ErrorCode.INVALID_INPUT),
        (ScopeDeniedError, ErrorCode.SCOPE_DENIED),
        (RevisionConflictError, ErrorCode.REVISION_CONFLICT),
        (OperationTimeoutError, ErrorCode.TIMEOUT),
        (OperationCancelledError, ErrorCode.CANCELLED),
        (IndeterminateExecutionError, ErrorCode.INDETERMINATE),
    ],
)
def test_error_taxonomy_has_stable_serializable_codes(error: type, code: ErrorCode) -> None:
    instance = error("observable failure")
    assert instance.to_dict() == {"code": code.value, "message": "observable failure"}


def test_domain_import_performs_no_network_process_or_provider_load() -> None:
    # A fresh interpreter prevents already-imported modules hiding side effects.
    code = r"""
import importlib.abc, json, socket, subprocess, sys
blocked = ("psycopg", "openai", "torch", "transformers", "requests", "httpx")
class NoProviders(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in blocked:
            raise AssertionError("provider import: " + fullname)
sys.meta_path.insert(0, NoProviders())
def deny(*args, **kwargs):
    raise AssertionError("I/O during domain import")
socket.socket = deny
socket.create_connection = deny
subprocess.Popen = deny
import potbot.domain
print(json.dumps({"loaded": True}))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout) == {"loaded": True}
