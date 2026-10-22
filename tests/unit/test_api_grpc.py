"""Pinned unary gRPC method adapter tests without unapproved sockets."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from potbot.domain.errors import ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.tools.api_contracts import ApiCallScope, ApiOperation
from potbot.tools.api_grpc import ApprovedGrpcAdapter, InstalledGrpcCodec, protobuf_struct_codec
from potbot.tools.capability_contracts import ToolInstallationDescriptor, ToolInvocationIntent


def scope_for(
    op: ApiOperation, args: dict[str, object], *, approve=None, egress=None
) -> ApiCallScope:
    w = uuid4()
    installation = ToolInstallationDescriptor(
        installation_ref="syn://thing/test-api-installation",
        workspace_id=w,
        implementation_hash="b" * 64,
        configuration_hash="c" * 64,
        policy_revision_hash="d" * 64,
        permitted_scopes=("read",),
    )
    intent = ToolInvocationIntent(
        invocation_id=uuid4(),
        workspace_id=w,
        task_revision_id=uuid4(),
        installation_hash=installation.fingerprint,
        input_type_ref=op.input_type_ref,
        input_hash=content_hash(args),
        effect_class=op.effect_class,
        policy_revision_hash=installation.policy_revision_hash,
        max_wall_time_ms=op.timeout_ms,
        max_output_bytes=op.max_output_bytes,
        idempotency_key="api-test-001",
    )
    return ApiCallScope(
        installation,
        intent,
        operation_hash=op.fingerprint,
        authorize=approve or (lambda *_: None),
        egress=egress or (lambda *_: None),
    )


def op(effect="read_only") -> ApiOperation:
    return ApiOperation(
        name="lookup",
        protocol="grpc",
        effect_class=effect,
        target="/sample.Registry/Lookup",
        method="UNARY",
        source_hash="a" * 64,
        input_schema_json=canonical_json(
            {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
                "additionalProperties": False,
            }
        ),
        output_schema_json=canonical_json(
            {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            }
        ),
    )


class FakeChannel:
    def __init__(self, result=b"ok", fail=False):
        self.calls = []
        self.result = result
        self.fail = fail

    def unary_unary(self, method, **kwargs):
        async def invoke(body, **kwargs):
            self.calls.append((method, body, kwargs))
            if self.fail:
                raise RuntimeError("connection vanished")
            return self.result

        return invoke


def codec() -> InstalledGrpcCodec:
    return InstalledGrpcCodec("a" * 64, lambda v: v["id"].encode(), lambda b: {"value": b.decode()})


def test_valid_grpc_unary_call() -> None:
    async def check():
        method = op()
        args = {"id": "x"}
        channel = FakeChannel(b"answer")
        adapter = ApprovedGrpcAdapter(
            channel=channel,
            installed_codecs={method.target: codec()},
            channel_approval=lambda _: None,
        )
        result = await adapter.call(method, args, scope_for(method, args))
        assert result.status == "succeeded" and result.value == {"value": "answer"}
        assert len(channel.calls) == 1 and channel.calls[0][0] == method.target

    asyncio.run(check())


def test_unregistered_method_and_codec_drift_denied() -> None:
    async def check():
        method = op()
        args = {"id": "1"}
        channel = FakeChannel()
        for installed in (
            {},
            {method.target: InstalledGrpcCodec("b" * 64, lambda v: b"x", lambda v: {"value": "x"})},
        ):
            adapter = ApprovedGrpcAdapter(
                channel=channel, installed_codecs=installed, channel_approval=lambda _: None
            )
            with pytest.raises(ScopeDeniedError):
                await adapter.call(method, args, scope_for(method, args))
        assert not channel.calls

    asyncio.run(check())


def test_grpc_credential_or_channel_grant_cannot_be_inferred() -> None:
    async def check():
        method = op()
        args = {"id": "1"}
        channel = FakeChannel()
        adapter = ApprovedGrpcAdapter(
            channel=channel,
            installed_codecs={method.target: codec()},
            channel_approval=lambda _: False,
        )
        with pytest.raises(ScopeDeniedError):
            await adapter.call(method, args, scope_for(method, args))
        assert not channel.calls

    asyncio.run(check())


def test_unavailable_grpc_write_is_indeterminate_and_never_retried() -> None:
    async def check():
        method = op(effect="non_idempotent_write")
        args = {"id": "1"}
        channel = FakeChannel(fail=True)
        adapter = ApprovedGrpcAdapter(
            channel=channel,
            installed_codecs={method.target: codec()},
            channel_approval=lambda _: None,
        )
        result = await adapter.call(method, args, scope_for(method, args))
        assert result.status == "indeterminate" and len(channel.calls) == 1

    asyncio.run(check())


def test_grpc_bad_output_is_failed_without_trusting_content() -> None:
    async def check():
        method = op()
        args = {"id": "1"}
        channel = FakeChannel()
        bad = InstalledGrpcCodec("a" * 64, lambda _: b"x", lambda _: {"value": 7})
        adapter = ApprovedGrpcAdapter(
            channel=channel, installed_codecs={method.target: bad}, channel_approval=lambda _: None
        )
        result = await adapter.call(method, args, scope_for(method, args))
        assert result.status == "failed" and result.error_code == "grpc_response_invalid"

    asyncio.run(check())


def test_protobuf_struct_codec_deterministic_round_trip() -> None:
    pytest.importorskip("google.protobuf")
    c = protobuf_struct_codec("f" * 64)
    a = c.serialize({"key": "x", "count": 2})
    b = c.serialize({"count": 2, "key": "x"})
    assert a == b
    assert c.deserialize(a) == {"key": "x", "count": 2}


def test_grpc_success_with_invalid_response_after_write_is_indeterminate() -> None:
    async def check():
        method = op(effect="non_idempotent_write")
        args = {"id": "1"}
        channel = FakeChannel()
        bad = InstalledGrpcCodec("a" * 64, lambda _: b"x", lambda _: {"value": 7})
        adapter = ApprovedGrpcAdapter(
            channel=channel, installed_codecs={method.target: bad}, channel_approval=lambda _: None
        )
        result = await adapter.call(method, args, scope_for(method, args))
        assert result.status == "indeterminate" and result.error_code == "grpc_response_invalid"
        assert len(channel.calls) == 1

    asyncio.run(check())
