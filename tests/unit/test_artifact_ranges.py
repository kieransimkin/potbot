"""Compiled Cog contracts and actual bounded, verified filesystem range reads."""

import base64
import hashlib
import os
from uuid import uuid4

import pytest

from potbot.cogs.artifact_ranges import CLOSE, OPEN, RANGE_COGS, READ
from potbot.cogs.contracts import TypedCogValue
from potbot.cogs.registry import BoundCogRegistry
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError
from potbot.persistence.artifacts import (
    ArtifactIntegrityError,
    LocalArtifactBackend,
    PublishedArtifact,
)
from potbot.tools.artifact_ranges import MAX_RANGE_BYTES, READ_BLOCK_BYTES, schemas
from potbot.tools.execution_contracts import MAX_INLINE_BYTES
from potbot.tools.semantic_contracts import ToolSemanticCatalog


def test_unsupported_range_profile_is_an_explicit_hold_before_filesystem_access(monkeypatch):
    backend = object.__new__(LocalArtifactBackend)
    monkeypatch.setattr(LocalArtifactBackend, "supports_range_reads", False)
    with pytest.raises(CapabilityUnavailableError, match="POSIX"):
        backend.read_range(uuid4(), PublishedArtifact("a" * 64, 1, "unused"), 0, 1)


def test_largest_encoded_range_fits_the_independent_work_output_cap():
    from potbot.tools.artifact_ranges import MAX_ARTIFACT_BYTES

    raw = b"\xff" * MAX_RANGE_BYTES
    source = TypedCogValue(
        READ.output_type_ref,
        {
            "schema_version": 1,
            "lease_id": str(uuid4()),
            "artifact_id": str(uuid4()),
            "artifact_sha256": "a" * 64,
            "byte_length": MAX_ARTIFACT_BYTES,
            "offset": 0,
            "requested_length": MAX_RANGE_BYTES,
            "returned_length": MAX_RANGE_BYTES,
            "data_base64": base64.b64encode(raw).decode("ascii"),
            "range_sha256": hashlib.sha256(raw).hexdigest(),
            "verified_byte_length": MAX_ARTIFACT_BYTES,
            "maximum_read_request_bytes": READ_BLOCK_BYTES,
            "read_calls": MAX_ARTIFACT_BYTES // READ_BLOCK_BYTES + 1,
            "range_ends_at_eof": False,
            "integrity": "full_sha256_verified",
        },
    )
    catalog = ToolSemanticCatalog(BoundCogRegistry.artifact_ranges().snapshot)
    assert len(catalog.encode(source)) <= MAX_INLINE_BYTES


def test_range_profile_is_installed_without_changing_the_prior_provenance_catalog():
    registry = BoundCogRegistry.artifact_ranges()
    assert len(registry.installed()) == 26
    assert all(s in registry.installed() for s in RANGE_COGS)
    assert (
        BoundCogRegistry.provenance().snapshot.fingerprint
        == "2414f1d94058e1518c01a9f35189d7d4e8d03b53174e2fc6dbb6c05f265f21ff"
    )
    assert all(s.requires_controller_scope for s in RANGE_COGS)
    catalog = ToolSemanticCatalog(registry.snapshot)
    for ref in schemas():
        assert len(catalog.schema_hash(ref)) == 64
    changed = schemas()
    changed[READ.input_type_ref]["properties"]["length"]["maximum"] = 999999
    assert schemas()[READ.input_type_ref]["properties"]["length"]["maximum"] == MAX_RANGE_BYTES


@pytest.mark.parametrize(
    "offset,length", [(-1, 1), (False, 1), (0, 0), (0, True), (0, MAX_RANGE_BYTES + 1)]
)
def test_invalid_offsets_and_caps_fail_at_the_registered_semantic_port(offset, length):
    catalog = ToolSemanticCatalog(BoundCogRegistry.artifact_ranges().snapshot)
    source = TypedCogValue(
        READ.input_type_ref,
        {
            "schema_version": 1,
            "lease_id": str(uuid4()),
            "artifact_sha256": "a" * 64,
            "offset": offset,
            "length": length,
        },
    )
    with pytest.raises(InvalidInputError):
        catalog.validate(source)


@pytest.mark.parametrize("signature", [OPEN, READ, CLOSE])
def test_uninstalled_metadata_cannot_change_the_range_handler(signature):
    from dataclasses import replace

    from potbot.domain.errors import ScopeDeniedError

    registry = BoundCogRegistry.artifact_ranges()
    objects = tuple(
        replace(
            o,
            payload_json=o.payload_json.replace(signature.handler_ref, "untrusted.module.execute"),
        )
        if o.ref == signature.cog_ref
        else o
        for o in registry.snapshot.objects
    )
    with pytest.raises(ScopeDeniedError):
        BoundCogRegistry(replace(registry.snapshot, objects=objects), artifact_range_profile=True)


@pytest.mark.skipif(
    os.name != "posix", reason="Existing local artifact backend requires POSIX dir_fd/O_NOFOLLOW"
)
def test_range_reader_hashes_the_whole_file_in_bounded_blocks_and_handles_binary_eof(
    tmp_path, monkeypatch
):
    backend = LocalArtifactBackend(tmp_path / "objects")
    actor = uuid4()
    raw = bytes(range(256)) * 1024 + b"tail\xff"
    artifact = backend.publish(actor, raw)
    original = os.fdopen
    sizes = []

    class Observed:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            sizes.append(size)
            assert 0 < size <= READ_BLOCK_BYTES
            return self.stream.read(size)

    monkeypatch.setattr(os, "fdopen", lambda *args, **kwargs: Observed(original(*args, **kwargs)))
    result = backend.read_range(actor, artifact, len(raw) - 20, MAX_RANGE_BYTES)
    assert result.data == raw[-20:] and result.verified_byte_length == len(raw)
    assert result.read_calls == len(sizes) and max(sizes) == READ_BLOCK_BYTES
    assert (
        hashlib.sha256(base64.b64decode(base64.b64encode(result.data))).digest()
        == hashlib.sha256(raw[-20:]).digest()
    )
    assert backend.read_range(actor, artifact, len(raw), 1).data == b""


@pytest.mark.skipif(
    os.name != "posix", reason="Existing local artifact backend requires POSIX dir_fd/O_NOFOLLOW"
)
def test_tampering_outside_the_requested_range_is_rejected_before_return(tmp_path):
    backend = LocalArtifactBackend(tmp_path / "objects")
    actor = uuid4()
    raw = b"a" * (3 * READ_BLOCK_BYTES)
    artifact = backend.publish(actor, raw)
    path = backend.root / artifact.locator
    with path.open("r+b") as stream:
        stream.seek(len(raw) - 1)
        stream.write(b"b")
    with pytest.raises(ArtifactIntegrityError, match="digest"):
        backend.read_range(actor, artifact, 0, 32)
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(ArtifactIntegrityError, match="file type"):
        backend.read_range(actor, artifact, 0, 32)
