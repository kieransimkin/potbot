"""Adversarial accounting/event bounds for the explicitly offline transport."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.models.invocations import GenerateRequest
from potbot.models.transport import ScriptedTransport, Terminal, TextDelta, TokenUsage


def request() -> GenerateRequest:
    return GenerateRequest(
        uuid4(),
        "reasoning",
        uuid4(),
        "fixture-only-v1",
        '{"temperature":0}',
        64,
        datetime.now(UTC) + timedelta(minutes=1),
        uuid4(),
    )


@pytest.mark.parametrize("invalid", (-1, 2**53, True, 1.0, float("inf"), "1"))
@pytest.mark.parametrize("field", ("input_tokens", "output_tokens", "money_microunits"))
def test_usage_never_coerces_or_truncates_ledger_amounts(field: str, invalid: Any) -> None:
    with pytest.raises(InvalidInputError):
        replace(TokenUsage(10, 20), **{field: invalid})


def test_total_tokens_must_fit_the_budget_ledger_dimension() -> None:
    limit = 2**53 - 1
    assert TokenUsage(limit - 1, 1).input_tokens + 1 == limit
    with pytest.raises(InvalidInputError):
        TokenUsage(limit, 1)


@pytest.mark.parametrize("invalid", (None, [], {}, "estimated", True))
def test_usage_origin_is_explicit_and_malformed_json_types_are_classified(invalid: Any) -> None:
    with pytest.raises(InvalidInputError):
        TokenUsage(1, 1, origin=invalid)


@pytest.mark.parametrize("invalid", (None, [], {}, "success", True))
def test_terminal_discriminator_does_not_accept_coerced_or_unrecognized_values(
    invalid: Any,
) -> None:
    with pytest.raises(InvalidInputError):
        Terminal(invalid)


def test_terminal_usage_is_a_validated_record_and_text_is_exact_bytes() -> None:
    with pytest.raises(InvalidInputError):
        Terminal(usage={"input_tokens": 1, "output_tokens": 1})
    with pytest.raises(InvalidInputError):
        TextDelta("not-encoded")
    chunk = b"\xff\x00"
    assert TextDelta(chunk).data == chunk


def test_fixture_preserves_fragmented_utf8_and_terminal_event() -> None:
    encoded = '{"text":"caf\u00e9"}'.encode()
    offset = encoded.index("\u00e9".encode()) + 1
    terminal = Terminal("stop", TokenUsage(2, 3))
    events = (TextDelta(encoded[:offset]), TextDelta(encoded[offset:]), terminal)
    transport = ScriptedTransport((events,))
    operation = uuid4()
    generation = request()
    observed = tuple(transport.stream(generation, "frozen wire input", operation))
    assert b"".join(event.data for event in observed if isinstance(event, TextDelta)) == encoded
    assert observed[-1] == terminal
    assert transport.requests == [(generation, "frozen wire input", operation)]
    assert transport.fixture_only is True


def test_concurrent_fixture_consumers_receive_each_scripted_response_once() -> None:
    responses = tuple((TextDelta(str(index).encode()), Terminal()) for index in range(8))
    transport = ScriptedTransport(responses)

    def consume(operation: UUID) -> bytes:
        return b"".join(
            event.data
            for event in transport.stream(request(), "fixture", operation)
            if isinstance(event, TextDelta)
        )

    operations = tuple(uuid4() for _ in responses)
    with ThreadPoolExecutor(max_workers=4) as pool:
        output = tuple(pool.map(consume, operations))
    assert set(output) == {str(index).encode() for index in range(8)}
    assert {item[2] for item in transport.requests} == set(operations)
    with pytest.raises(InvalidInputError):
        tuple(transport.stream(request(), "fixture", uuid4()))


@pytest.mark.parametrize(
    "responses",
    (
        (),
        ((),) * 65,
        ((TextDelta(b""),) * 4097,),
        (({"kind": "terminal"},),),
        [[TextDelta(b"not-a-frozen-event-tuple")]],
    ),
)
def test_scripted_sequences_are_finite_and_validate_their_event_types(responses: Any) -> None:
    with pytest.raises(InvalidInputError):
        ScriptedTransport(responses)
