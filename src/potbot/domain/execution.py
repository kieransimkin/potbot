"""Versioned proposals and controller-owned local tool capabilities."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import content_hash


def relative_path(value: object, *, allow_dot: bool = False) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise InvalidInputError("A bounded relative POSIX path is required.")
    if len(value) > 2000:
        raise InvalidInputError("Path exceeds its allowed length.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or ":" in value:
        raise ScopeDeniedError("The requested path is outside the candidate workspace.")
    if value == "." and allow_dot:
        return value
    if str(path) != value or not path.parts or value == ".":
        raise InvalidInputError("Paths must be normalized and relative.")
    return value


def bounded_strings(value: object, name: str, *, limit: int = 128) -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > limit:
        raise InvalidInputError(f"{name} must be a bounded immutable sequence.")
    if any(not isinstance(v, str) or not v or len(v) > 8192 or "\x00" in v for v in value):
        raise InvalidInputError(f"{name} contains an invalid value.")
    return value


@dataclass(frozen=True, slots=True)
class ActionProposal:
    """Untrusted arguments cannot supply execution identity or permissions."""

    kind: str
    arguments: tuple[str, ...] = ()
    working_directory: str = "."
    input_paths: tuple[str, ...] = ()
    output_paths: tuple[str, ...] = ()
    expected_output: str = "A captured process result and resulting repository state."
    stopping_condition: str = "Stop on completion, cancellation or the configured deadline."
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise InvalidInputError("Unsupported action proposal version.")
        if not isinstance(self.kind, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", self.kind):
            raise InvalidInputError("Invalid action kind.")
        bounded_strings(self.arguments, "arguments")
        relative_path(self.working_directory, allow_dot=True)
        for name in ("input_paths", "output_paths"):
            for path in bounded_strings(getattr(self, name), name):
                relative_path(path)
        for name in ("expected_output", "stopping_condition"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 4000:
                raise InvalidInputError(f"{name} must be explicit and bounded.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "arguments": list(self.arguments),
            "working_directory": self.working_directory,
            "input_paths": list(self.input_paths),
            "output_paths": list(self.output_paths),
            "expected_output": self.expected_output,
            "stopping_condition": self.stopping_condition,
        }

    @classmethod
    def from_dict(cls, value: object) -> ActionProposal:
        expected = {
            "schema_version",
            "kind",
            "arguments",
            "working_directory",
            "input_paths",
            "output_paths",
            "expected_output",
            "stopping_condition",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise InvalidInputError("Action proposal fields do not match its registered schema.")
        for field in ("arguments", "input_paths", "output_paths"):
            if not isinstance(value[field], list):
                raise InvalidInputError(f"{field} must be an array.")
        return cls(
            kind=value["kind"],
            arguments=tuple(value["arguments"]),
            working_directory=value["working_directory"],
            input_paths=tuple(value["input_paths"]),
            output_paths=tuple(value["output_paths"]),
            expected_output=value["expected_output"],
            stopping_condition=value["stopping_condition"],
            schema_version=value["schema_version"],
        )


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Installed by the controller/operator, never decoded from a model proposal."""

    name: str
    capability: str
    argv_prefix: tuple[str, ...]
    mutates: bool = True
    allow_arguments: bool = False
    timeout_ms: int = 30_000
    max_output_bytes: int = 1_048_576
    environment: tuple[tuple[str, str], ...] = ()
    secret_refs: tuple[tuple[str, str], ...] = ()
    accepts_stdin: bool = False
    memory_bytes: int | None = None
    cpu_seconds: int | None = None

    def __post_init__(self) -> None:
        ActionProposal(self.name)
        bounded_strings(self.argv_prefix, "argv_prefix")
        if not self.argv_prefix:
            raise InvalidInputError("A tool requires an explicit executable.")
        if not isinstance(self.capability, str) or not self.capability.strip():
            raise InvalidInputError("A tool requires an explicit capability.")
        if any(
            type(value) is not bool
            for value in (self.mutates, self.allow_arguments, self.accepts_stdin)
        ):
            raise InvalidInputError("Tool effect and argument policies must be boolean.")
        if type(self.timeout_ms) is not int or not 1 <= self.timeout_ms <= 3_600_000:
            raise InvalidInputError("Tool deadline must be between 1 ms and one hour.")
        if type(self.max_output_bytes) is not int or not 1 <= self.max_output_bytes <= 16 * 1024**2:
            raise InvalidInputError("Invalid tool capture limit.")
        for name, value, minimum, maximum in (
            ("memory_bytes", self.memory_bytes, 16_777_216, 1024**4),
            ("cpu_seconds", self.cpu_seconds, 1, 3600),
        ):
            if value is not None and (type(value) is not int or not minimum <= value <= maximum):
                raise InvalidInputError(f"Tool {name} is outside its supported bound.")
        for field in ("environment", "secret_refs"):
            pairs = getattr(self, field)
            if (
                not isinstance(pairs, tuple)
                or any(
                    not isinstance(pair, tuple)
                    or len(pair) != 2
                    or any(not isinstance(v, str) or "\x00" in v for v in pair)
                    or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", pair[0])
                    for pair in pairs
                )
                or len(dict(pairs)) != len(pairs)
            ):
                raise InvalidInputError("Tool environment bindings must be distinct named strings.")
        if set(dict(self.environment)) & set(dict(self.secret_refs)):
            raise InvalidInputError("Literal and secret environment bindings overlap.")
        if any(not ref.startswith("secret://") for _, ref in self.secret_refs):
            raise InvalidInputError("Credentials require secret references.")

    @property
    def fingerprint(self) -> str:
        fields = {name: getattr(self, name) for name in self.__dataclass_fields__}
        if not self.accepts_stdin:
            fields.pop("accepts_stdin")  # Preserve every Phase 3 definition identity.
        for name in ("memory_bytes", "cpu_seconds"):
            if fields[name] is None:
                fields.pop(name)  # Keep historical definition fingerprints stable.
        return content_hash(fields)

    def admit(self, proposal: ActionProposal) -> None:
        if proposal.kind != self.name or (proposal.arguments and not self.allow_arguments):
            raise ScopeDeniedError("The requested tool arguments are not authorized.")


class ToolRegistry:
    def __init__(self, definitions: tuple[ToolDefinition, ...]) -> None:
        self._tools = {definition.name: definition for definition in definitions}
        if len(self._tools) != len(definitions):
            raise InvalidInputError("Tool kinds must be unique.")

    def get(self, kind: str) -> ToolDefinition:
        try:
            return self._tools[kind]
        except KeyError:
            raise InvalidInputError("Unknown registered action kind.") from None

    def inventory(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))


@dataclass(frozen=True, slots=True)
class CapabilityContext:
    """Controller authority, further restricted by the persisted task contract."""

    allowed_tools: tuple[str, ...]
    allowed_secret_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        bounded_strings(self.allowed_tools, "allowed_tools")
        bounded_strings(self.allowed_secret_refs, "allowed_secret_refs")

    def authorize(self, tool: ToolDefinition, contract: Mapping[str, Any]) -> None:
        if tool.name not in self.allowed_tools or tool.capability not in contract["capabilities"]:
            raise ScopeDeniedError("The active task does not permit this tool capability.")
        if any(ref not in self.allowed_secret_refs for _, ref in tool.secret_refs):
            raise ScopeDeniedError("The tool is not authorized to receive this secret reference.")
