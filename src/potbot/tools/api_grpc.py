"""T02.4 pinned unary gRPC invocation, using controller-created channels.

Only explicitly registered full method names can execute. Protobuf codecs are
installed by the operator (no arbitrary descriptor/extension loading). Reflection
and dynamic package downloads are intentionally not enabled in this stage.
The controller is responsible for securing channels, egress, leases and budgets.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.tools.api_contracts import (
    ApiCallResult,
    ApiCallScope,
    ApiOperation,
    validate_json,
)
from potbot.tools.capability_contracts import (
    ToolImplementationDescriptor,
    ToolInstallationDescriptor,
)
from potbot.tools.connect_policy import ConnectPolicy, checked_host


@dataclass(frozen=True, slots=True)
class InstalledGrpcCodec:
    """Trusted deterministic conversion functions, pinned by descriptor digest."""

    descriptor_hash: str
    serialize: Callable[[Mapping[str, Any]], bytes]
    deserialize: Callable[[bytes], Any]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.descriptor_hash, str)
            or len(self.descriptor_hash) != 64
            or any(c not in "0123456789abcdef" for c in self.descriptor_hash)
        ):
            raise InvalidInputError("gRPC codec requires a pinned descriptor SHA-256")
        if not callable(self.serialize) or not callable(self.deserialize):
            raise InvalidInputError("gRPC codec functions must be installed code")


def protobuf_struct_codec(descriptor_hash: str) -> InstalledGrpcCodec:
    """Useful for tests/JSON-Struct services; generated messages need named codecs."""
    try:
        from google.protobuf import json_format
        from google.protobuf.struct_pb2 import Struct
    except ImportError as error:
        raise CapabilityUnavailableError("Protobuf support is not installed") from error

    def serialize(value: Mapping[str, Any]) -> bytes:
        message = json_format.ParseDict(dict(value), Struct())
        return message.SerializeToString(deterministic=True)

    def deserialize(value: bytes) -> Any:
        message = Struct()
        message.ParseFromString(value)
        return json_format.MessageToDict(message)

    return InstalledGrpcCodec(descriptor_hash, serialize, deserialize)


class ApprovedGrpcAdapter:
    """Fixed-method unary gRPC wrapper over a pre-authorized channel."""

    def __init__(
        self,
        *,
        channel: Any,
        installed_codecs: Mapping[str, InstalledGrpcCodec],
        channel_approval: Callable[[str], None],
    ) -> None:
        if channel is None or not callable(getattr(channel, "unary_unary", None)):
            raise InvalidInputError("gRPC requires an existing controller-created channel")
        if not callable(channel_approval):
            raise ScopeDeniedError("gRPC requires a qualified channel admission check")
        self.channel = channel
        self.codecs = dict(installed_codecs)
        self.channel_approval = channel_approval

    async def call(
        self, operation: ApiOperation, arguments: Mapping[str, Any], scope: ApiCallScope
    ) -> ApiCallResult:
        if operation.protocol != "grpc":
            raise InvalidInputError("Not a gRPC operation")
        approved_arguments = json.loads(canonical_json(dict(arguments)))
        scope.check(operation, approved_arguments)
        return await self._call_admitted(operation, approved_arguments)

    async def _call_admitted(
        self, operation: ApiOperation, approved_arguments: dict[str, Any]
    ) -> ApiCallResult:
        if self.channel_approval(operation.target) is not None:
            raise ScopeDeniedError("gRPC channel check must authorize by returning None")
        codec = self.codecs.get(operation.target)
        if codec is None or codec.descriptor_hash != operation.source_hash:
            raise ScopeDeniedError("gRPC method descriptor or protobuf codec was not pinned")
        try:
            payload = codec.serialize(approved_arguments)
        except (ValueError, TypeError, KeyError) as error:
            raise InvalidInputError(
                "gRPC input cannot be serialized using its declared protobuf codec"
            ) from error
        if not isinstance(payload, bytes) or len(payload) > 1048576:
            raise InvalidInputError("gRPC serialized request exceeds message limit")
        # The pre-authorized channel determines any transport retry behavior.
        method = self.channel.unary_unary(
            operation.target,
            request_serializer=lambda v: v,
            response_deserializer=lambda v: v,
        )
        try:
            response = await method(payload, timeout=operation.timeout_ms / 1000)
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception:
            return ApiCallResult(
                operation.fingerprint,
                "failed" if operation.effect_class == "read_only" else "indeterminate",
                None,
                "grpc_unavailable",
                attempts=1,
            )
        if not isinstance(response, bytes) or len(response) > operation.max_output_bytes:
            return ApiCallResult(
                operation.fingerprint,
                "failed" if operation.effect_class == "read_only" else "indeterminate",
                None,
                "grpc_response_too_large",
                attempts=1,
            )
        try:
            value = codec.deserialize(response)
            validate_json(operation.output_schema_json, value, label="gRPC decoded response")
            if len(canonical_json(value).encode("utf-8")) > operation.max_output_bytes:
                raise InvalidInputError("gRPC decoded response exceeds the admitted bound")
        except Exception:
            return ApiCallResult(
                operation.fingerprint,
                "failed" if operation.effect_class == "read_only" else "indeterminate",
                None,
                "grpc_response_invalid",
                attempts=1,
            )
        return ApiCallResult(operation.fingerprint, "succeeded", value, None, attempts=1)


def grpc_read_configuration_hash(
    actor: WorkspaceContext, host: str, operation: ApiOperation
) -> str:
    return content_hash(
        {
            "profile": "potbot.pinned-grpc-read/1",
            "workspace_id": str(actor.workspace_id),
            "principal_id": str(actor.principal_id),
            "authority": f"{host}:443",
            "operation": operation.fingerprint,
        }
    )


@dataclass(frozen=True, slots=True)
class InstalledGrpcRead:
    """One operator-selected read-only method and installed codec over named TLS."""

    actor: WorkspaceContext
    host: str
    operation: ApiOperation
    codec: InstalledGrpcCodec
    implementation: ToolImplementationDescriptor
    installation: ToolInstallationDescriptor

    def __post_init__(self) -> None:
        checked_host(self.host)
        op = self.operation
        if (
            op.protocol != "grpc"
            or op.effect_class != "read_only"
            or not isinstance(self.codec, InstalledGrpcCodec)
            or self.codec.descriptor_hash != op.source_hash
            or self.actor.workspace_id != self.installation.workspace_id
            or self.installation.configuration_hash
            != grpc_read_configuration_hash(self.actor, self.host, op)
        ):
            raise ScopeDeniedError("gRPC read differs from the pinned installation")
        self.implementation.assert_capability(op.to_capability())
        self.installation.assert_implementation(self.implementation)

    async def call(
        self, actor: WorkspaceContext, arguments: Mapping[str, Any], scope: ApiCallScope
    ) -> ApiCallResult:
        if (
            actor != self.actor
            or scope.installation != self.installation
            or scope.egress is not None
        ):
            raise ScopeDeniedError("gRPC read is unavailable to this principal or installation")
        try:
            import grpc  # type: ignore[import-untyped]
        except ImportError as exc:
            raise CapabilityUnavailableError(
                "gRPC installation requires the providers extra"
            ) from exc

        op = self.operation
        frozen = json.loads(canonical_json(dict(arguments)))
        scope.check(op, frozen)
        async with asyncio.timeout(op.timeout_ms / 1000):
            address = await asyncio.to_thread(
                ConnectPolicy((self.host,)).resolve,
                f"{self.host}:443",
                resolver=partial(socket.getaddrinfo, family=socket.AF_INET),
            )
            # Use a numeric resolver target and independently verify the installed DNS name.
            async with grpc.aio.secure_channel(
                f"ipv4:{address}:443",
                grpc.ssl_channel_credentials(),
                options=(
                    ("grpc.ssl_target_name_override", self.host),
                    ("grpc.default_authority", self.host),
                    ("grpc.enable_http_proxy", 0),
                    ("grpc.enable_retries", 0),
                    ("grpc.service_config_disable_resolution", 1),
                    ("grpc.max_receive_message_length", op.max_output_bytes),
                    ("grpc.max_send_message_length", 1048576),
                ),
            ) as channel:

                def admit_method(target: str) -> None:
                    if target != op.target:
                        raise ScopeDeniedError("gRPC method differs from the pinned installation")

                adapter = ApprovedGrpcAdapter(
                    channel=channel,
                    installed_codecs={op.target: self.codec},
                    channel_approval=admit_method,
                )
                return await adapter._call_admitted(op, frozen)
