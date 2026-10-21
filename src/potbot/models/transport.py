"""Bounded transport events and an explicitly synthetic, offline fixture backend.

No provider client, network transport or model loader is installed by this module.
The event contract is reusable by later supervised adapters; the Phase 5 gateway
currently admits only the offline fixture backend while egress policy is pending.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from threading import Lock
from typing import Protocol
from uuid import UUID

from potbot.domain.errors import InvalidInputError
from potbot.models.invocations import GenerateRequest


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    money_microunits: int = 0
    origin: str = "synthetic"

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or not 0 <= value <= 2**53 - 1
            for value in (self.input_tokens, self.output_tokens, self.money_microunits)
        ) or (
            not isinstance(self.origin, str)
            or self.origin not in {"synthetic", "measured", "provider_reported"}
        ):
            raise InvalidInputError("Invalid terminal inference usage.")
        if self.input_tokens + self.output_tokens > 2**53 - 1:
            raise InvalidInputError("Terminal token usage exceeds the accounting bound.")


@dataclass(frozen=True, slots=True)
class TextDelta:
    data: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise InvalidInputError("Inference deltas must be bytes.")


@dataclass(frozen=True, slots=True)
class Terminal:
    reason: str = "stop"
    usage: TokenUsage | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.reason, str)
            or self.reason not in {"stop", "length", "cancelled", "failed"}
            or (self.usage is not None and not isinstance(self.usage, TokenUsage))
        ):
            raise InvalidInputError("Invalid terminal inference event.")


StreamEvent = TextDelta | Terminal


class OfflineTransport(Protocol):
    backend: str
    backend_version: str
    fixture_only: bool

    def stream(
        self, request: GenerateRequest, wire_text: str, operation_id: UUID
    ) -> Iterator[StreamEvent]: ...

    def stop(self, operation_id: UUID) -> bool:
        """Whether the backend confirms work stopped; false preserves uncertainty."""
        ...


class ScriptedTransport:
    """One finite sequence of supplied events per invocation, with no inference."""

    backend = "potbot-offline-fixture"
    backend_version = "1"
    fixture_only = True

    def __init__(self, responses: tuple[tuple[StreamEvent, ...], ...]) -> None:
        if (
            not isinstance(responses, tuple)
            or not responses
            or len(responses) > 64
            or any(
                not isinstance(events, tuple)
                or len(events) > 4096
                or any(not isinstance(item, TextDelta | Terminal) for item in events)
                for events in responses
            )
        ):
            raise InvalidInputError("Fixture responses must be finite transport event tuples.")
        self.responses = responses
        self.requests: list[tuple[GenerateRequest, str, UUID]] = []
        self._lock = Lock()

    def stream(
        self, request: GenerateRequest, wire_text: str, operation_id: UUID
    ) -> Iterator[StreamEvent]:
        with self._lock:
            ordinal = len(self.requests)
            self.requests.append((request, wire_text, operation_id))
        if ordinal >= len(self.responses):
            raise InvalidInputError("No scripted response was supplied for this invocation.")
        yield from self.responses[ordinal]

    def stop(self, operation_id: UUID) -> bool:
        return True
