"""Portable upload contracts plus actual bounded POSIX publication/failure cleanup."""

import base64
import hashlib
import os
from dataclasses import replace
from uuid import uuid4

import pytest

from potbot.cogs.artifact_uploads import CHUNK, INSPECT, UPLOAD_COGS
from potbot.cogs.contracts import TypedCogValue
from potbot.cogs.registry import BoundCogRegistry
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError
from potbot.persistence.artifacts import ArtifactIntegrityError, LocalArtifactBackend
from potbot.tools.artifact_ranges import MAX_ARTIFACT_BYTES, MAX_RANGE_BYTES
from potbot.tools.artifact_uploads import MAX_CHUNKS, PAGE_SLOTS, schemas
from potbot.tools.execution_contracts import MAX_INLINE_BYTES
from potbot.tools.semantic_contracts import ToolSemanticCatalog


def test_upload_profile_preserves_previous_catalog_and_compiles_ten_independent_ports():
    registry = BoundCogRegistry.artifact_uploads()
    assert len(registry.installed()) == 31
    assert set(UPLOAD_COGS) <= set(registry.installed())
    assert (
        BoundCogRegistry.artifact_ranges().snapshot.fingerprint
        == "157781bf9112d0c4334c6993755303a4c0bc3791c6986d078a8e7c9ce1e13f97"
    )
    catalog = ToolSemanticCatalog(registry.snapshot)
    assert len(schemas()) == 10
    assert all(len(catalog.schema_hash(ref)) == 64 for ref in schemas())
    altered = schemas()
    altered[CHUNK.input_type_ref]["properties"]["offset"]["maximum"] = -1
    assert schemas()[CHUNK.input_type_ref]["properties"]["offset"]["maximum"] == MAX_ARTIFACT_BYTES


def test_largest_upload_chunk_and_inspection_page_fit_independent_inline_budget():
    catalog = ToolSemanticCatalog(BoundCogRegistry.artifact_uploads().snapshot)
    pin = {"schema_version": 1, "upload_id": str(uuid4()), "artifact_sha256": "a" * 64}
    raw = b"\xff" * MAX_RANGE_BYTES
    chunk = TypedCogValue(
        CHUNK.input_type_ref,
        {
            **pin,
            "offset": MAX_ARTIFACT_BYTES - MAX_RANGE_BYTES,
            "chunk_sha256": hashlib.sha256(raw).hexdigest(),
            "data_base64": base64.b64encode(raw).decode("ascii"),
        },
    )
    page = TypedCogValue(
        INSPECT.output_type_ref,
        {
            **pin,
            "byte_length": MAX_ARTIFACT_BYTES,
            "state": "open",
            "expected_chunks": MAX_CHUNKS,
            "received_chunks": 0,
            "received_bytes": 0,
            "missing_offsets": [
                (MAX_CHUNKS - PAGE_SLOTS + i) * MAX_RANGE_BYTES for i in range(PAGE_SLOTS)
            ],
            "next_cursor": None,
        },
    )
    assert max(len(catalog.encode(chunk)), len(catalog.encode(page))) < MAX_INLINE_BYTES


@pytest.mark.parametrize(
    "change", [{"offset": True}, {"offset": -1}, {"data_base64": "!"}, {"data_base64": "A" * 20000}]
)
def test_invalid_chunks_reject_at_the_single_registered_port(change):
    value = {
        "schema_version": 1,
        "upload_id": str(uuid4()),
        "artifact_sha256": "a" * 64,
        "offset": 0,
        "chunk_sha256": "b" * 64,
        "data_base64": "YQ==",
        **change,
    }
    with pytest.raises(InvalidInputError):
        ToolSemanticCatalog(BoundCogRegistry.artifact_uploads().snapshot).validate(
            TypedCogValue(CHUNK.input_type_ref, value)
        )


@pytest.mark.parametrize("signature", UPLOAD_COGS)
def test_untrusted_metadata_cannot_replace_an_installed_upload_handler(signature):
    registry = BoundCogRegistry.artifact_uploads()
    objects = tuple(
        replace(o, payload_json=o.payload_json.replace(signature.handler_ref, "untrusted.run"))
        if o.ref == signature.cog_ref
        else o
        for o in registry.snapshot.objects
    )
    with pytest.raises(ScopeDeniedError):
        BoundCogRegistry(replace(registry.snapshot, objects=objects), artifact_upload_profile=True)


def test_unsupported_publisher_fails_before_any_filesystem_access(monkeypatch):
    backend = object.__new__(LocalArtifactBackend)
    monkeypatch.setattr(LocalArtifactBackend, "supports_chunk_uploads", False)
    with pytest.raises(CapabilityUnavailableError, match="POSIX"):
        backend.publish_chunks(
            uuid4(), (), expected_digest="a" * 64, byte_length=0, before_publish=lambda: None
        )


@pytest.mark.skipif(
    os.name != "posix", reason="Qualified local publisher requires POSIX descriptor operations"
)
def test_stream_publication_verifies_whole_content_without_whole_object_reader(
    tmp_path, monkeypatch
):
    backend, actor = LocalArtifactBackend(tmp_path / "objects"), uuid4()
    raw = bytes(range(256)) * 200 + b"\x00\xff end"
    sizes, checks = [], []
    monkeypatch.setattr(backend, "read", lambda *args: pytest.fail("Whole-object reader used"))

    def pieces():
        for start in range(0, len(raw), MAX_RANGE_BYTES):
            chunk = raw[start : start + MAX_RANGE_BYTES]
            sizes.append(len(chunk))
            yield chunk

    result = backend.publish_chunks(
        actor,
        pieces(),
        expected_digest=hashlib.sha256(raw).hexdigest(),
        byte_length=len(raw),
        before_publish=lambda: checks.append("checked"),
    )
    assert (backend.root / result.locator).read_bytes() == raw
    assert max(sizes) == MAX_RANGE_BYTES and len(checks) == 2
    assert not list(backend.root.rglob(".pending-*"))


@pytest.mark.skipif(
    os.name != "posix", reason="Qualified local publisher requires POSIX descriptor operations"
)
def test_failed_integrity_or_revoked_grant_never_publishes_final_object_and_cleans_staging(
    tmp_path,
):
    backend, actor, raw = LocalArtifactBackend(tmp_path / "objects"), uuid4(), b"private binary\xff"
    expected = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ArtifactIntegrityError, match="SHA-256"):
        backend.publish_chunks(
            actor,
            [raw],
            expected_digest="b" * 64,
            byte_length=len(raw),
            before_publish=lambda: None,
        )
    assert not (backend.root / backend.locator(actor, "b" * 64)).exists()

    def deny():
        raise ScopeDeniedError("Grant withdrawn during staging")

    with pytest.raises(ScopeDeniedError):
        backend.publish_chunks(
            actor, [raw], expected_digest=expected, byte_length=len(raw), before_publish=deny
        )
    assert not (backend.root / backend.locator(actor, expected)).exists()
    assert not list(backend.root.rglob(".pending-*"))


@pytest.mark.skipif(
    os.name != "posix", reason="Qualified local publisher requires POSIX descriptor operations"
)
def test_partial_generator_failure_cleans_staging_and_existing_corruption_is_not_overwritten(
    tmp_path,
):
    backend, actor, raw = (
        LocalArtifactBackend(tmp_path / "objects"),
        uuid4(),
        b"a" * MAX_RANGE_BYTES,
    )
    expected = hashlib.sha256(raw).hexdigest()

    def fail():
        yield raw
        raise RuntimeError("Owned source interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        backend.publish_chunks(
            actor,
            fail(),
            expected_digest=expected,
            byte_length=len(raw),
            before_publish=lambda: None,
        )
    assert not list(backend.root.rglob(".pending-*"))
    artifact = backend.publish(actor, raw)
    path = backend.root / artifact.locator
    path.write_bytes(b"b" * len(raw))
    with pytest.raises(ArtifactIntegrityError):
        backend.publish_chunks(
            actor,
            [raw],
            expected_digest=expected,
            byte_length=len(raw),
            before_publish=lambda: None,
        )
    assert path.read_bytes() == b"b" * len(raw)
