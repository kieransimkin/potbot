"""Strict named-vector references derived from immutable source and space records.

Names select labels within registered spaces. They are neither data types nor
claims that a custom projection has run. The first format serializes the exact
source text as UTF-8; artifact bytes and existing profile identities stay intact.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Self
from uuid import UUID

from .errors import InvalidInputError
from .records import JsonRecord, _nonempty, _object, _parse_uuid, _uuid, _version

DEFAULT_EMBEDDING_NAME = "general_text"
SOURCE_TEXT_INPUT_FORMAT = "source_text_v1"
_NAME = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*", re.ASCII)
_DIGEST = re.compile(r"[0-9a-f]{64}", re.ASCII)


def validate_embedding_name(value: object) -> str:
    """Return an exact canonical ASCII label; never normalize caller input."""
    if not isinstance(value, str) or not 1 <= len(value) <= 64 or not _NAME.fullmatch(value):
        raise InvalidInputError(
            "embedding name must be a canonical ASCII label of 1 to 64 characters"
        )
    return value


def _digest(value: object, field: str) -> None:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise InvalidInputError(f"{field} must be 64 lowercase hexadecimal characters")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise InvalidInputError("embedding records cannot contain duplicate JSON object keys")
        result[name] = value
    return result


class _EmbeddingRecord(JsonRecord):
    __slots__ = ()

    @classmethod
    def from_json(cls, value: str) -> Self:
        try:
            data = json.loads(value, object_pairs_hook=_unique_object)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InvalidInputError("invalid JSON embedding record") from error
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class NamedEmbeddingReference(_EmbeddingRecord):
    embedding_name: str
    view_id: UUID
    vector_id: UUID
    source_id: UUID
    repository_id: UUID
    source_key: str
    source_content_hash: str
    space_id: UUID
    profile_hash: str
    encoding_input_hash: str
    input_format: str
    dimensions: int
    metric: str
    generation: str
    usage_origin: str

    def __post_init__(self) -> None:
        validate_embedding_name(self.embedding_name)
        for field in ("view_id", "vector_id", "source_id", "repository_id", "space_id"):
            _uuid(getattr(self, field), field)
        for field in ("source_content_hash", "profile_hash", "encoding_input_hash"):
            _digest(getattr(self, field), field)
        if not isinstance(self.source_key, str) or not 1 <= len(self.source_key) <= 1000:
            raise InvalidInputError("source_key must contain 1 to 1000 characters")
        if self.input_format != SOURCE_TEXT_INPUT_FORMAT:
            raise InvalidInputError("unsupported named-embedding input format")
        if type(self.dimensions) is not int or self.dimensions != 1024 or self.metric != "cosine":
            raise InvalidInputError("named embeddings currently require 1024 dimensions and cosine")
        _nonempty(self.generation, "generation")
        if not isinstance(self.usage_origin, str) or self.usage_origin not in {
            "synthetic",
            "measured",
            "provider_reported",
        }:
            raise InvalidInputError("invalid named-embedding usage origin")

    @property
    def sort_key(self) -> tuple[str, ...]:
        return (
            str(self.repository_id),
            self.source_key,
            str(self.source_id),
            str(self.space_id),
            str(self.view_id),
            str(self.vector_id),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "embedding_name": self.embedding_name,
            "view_id": str(self.view_id),
            "vector_id": str(self.vector_id),
            "source_id": str(self.source_id),
            "repository_id": str(self.repository_id),
            "source_key": self.source_key,
            "source_content_hash": self.source_content_hash,
            "space_id": str(self.space_id),
            "profile_hash": self.profile_hash,
            "encoding_input_hash": self.encoding_input_hash,
            "input_format": self.input_format,
            "dimensions": self.dimensions,
            "metric": self.metric,
            "generation": self.generation,
            "usage_origin": self.usage_origin,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        fields = {
            "embedding_name",
            "view_id",
            "vector_id",
            "source_id",
            "repository_id",
            "source_key",
            "source_content_hash",
            "space_id",
            "profile_hash",
            "encoding_input_hash",
            "input_format",
            "dimensions",
            "metric",
            "generation",
            "usage_origin",
        }
        if not isinstance(data, dict) or set(data) != fields:
            raise InvalidInputError("named-embedding reference fields do not match schema")
        return cls(
            embedding_name=data["embedding_name"],
            view_id=_parse_uuid(data["view_id"], "view_id"),
            vector_id=_parse_uuid(data["vector_id"], "vector_id"),
            source_id=_parse_uuid(data["source_id"], "source_id"),
            repository_id=_parse_uuid(data["repository_id"], "repository_id"),
            source_key=data["source_key"],
            source_content_hash=data["source_content_hash"],
            space_id=_parse_uuid(data["space_id"], "space_id"),
            profile_hash=data["profile_hash"],
            encoding_input_hash=data["encoding_input_hash"],
            input_format=data["input_format"],
            dimensions=data["dimensions"],
            metric=data["metric"],
            generation=data["generation"],
            usage_origin=data["usage_origin"],
        )


@dataclass(frozen=True, slots=True)
class ArtifactEmbeddingManifest(_EmbeddingRecord):
    workspace_id: UUID
    artifact_id: UUID
    artifact_digest: str
    byte_length: int
    media_type: str
    named_embeddings: Mapping[str, tuple[NamedEmbeddingReference, ...]]
    schema_version: int = 1

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _uuid(self.workspace_id, "workspace_id")
        _uuid(self.artifact_id, "artifact_id")
        _digest(self.artifact_digest, "artifact_digest")
        if type(self.byte_length) is not int or not 0 <= self.byte_length <= 2**63 - 1:
            raise InvalidInputError("byte_length must be a nonnegative bigint")
        if not isinstance(self.media_type, str) or not 1 <= len(self.media_type) <= 255:
            raise InvalidInputError("media_type must contain 1 to 255 characters")
        if not isinstance(self.named_embeddings, Mapping):
            raise InvalidInputError("named_embeddings must be a name-to-reference mapping")
        groups: dict[str, tuple[NamedEmbeddingReference, ...]] = {}
        vector_ids: set[UUID] = set()
        materializations: set[tuple[UUID, str, UUID]] = set()
        for name, references in self.named_embeddings.items():
            validate_embedding_name(name)
            if not isinstance(references, tuple) or not references:
                raise InvalidInputError("each embedding name requires an immutable nonempty tuple")
            for reference in references:
                if not isinstance(reference, NamedEmbeddingReference):
                    raise InvalidInputError("named_embeddings requires typed embedding references")
                if reference.embedding_name != name:
                    raise InvalidInputError("embedding reference name does not match its map key")
                if reference.encoding_input_hash != self.artifact_digest:
                    raise InvalidInputError(
                        "source_text_v1 input must match the source artifact bytes"
                    )
                key = (reference.source_id, name, reference.space_id)
                if reference.vector_id in vector_ids or key in materializations:
                    raise InvalidInputError("duplicate named-embedding materialization")
                vector_ids.add(reference.vector_id)
                materializations.add(key)
            groups[name] = tuple(sorted(references, key=lambda reference: reference.sort_key))
        object.__setattr__(self, "named_embeddings", MappingProxyType(dict(sorted(groups.items()))))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "workspace_id": str(self.workspace_id),
            "artifact_id": str(self.artifact_id),
            "artifact_digest": self.artifact_digest,
            "byte_length": self.byte_length,
            "media_type": self.media_type,
            "named_embeddings": {
                name: [reference.to_dict() for reference in references]
                for name, references in self.named_embeddings.items()
            },
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        value = _object(
            data,
            {
                "schema_version",
                "workspace_id",
                "artifact_id",
                "artifact_digest",
                "byte_length",
                "media_type",
                "named_embeddings",
            },
        )
        if not isinstance(value["named_embeddings"], dict):
            raise InvalidInputError("named_embeddings must be a JSON object")
        groups: dict[str, tuple[NamedEmbeddingReference, ...]] = {}
        for name, references in value["named_embeddings"].items():
            validate_embedding_name(name)
            if not isinstance(references, list):
                raise InvalidInputError("each embedding name must contain a JSON array")
            groups[name] = tuple(NamedEmbeddingReference.from_dict(item) for item in references)
        return cls(
            workspace_id=_parse_uuid(value["workspace_id"], "workspace_id"),
            artifact_id=_parse_uuid(value["artifact_id"], "artifact_id"),
            artifact_digest=value["artifact_digest"],
            byte_length=value["byte_length"],
            media_type=value["media_type"],
            named_embeddings=groups,
            schema_version=value["schema_version"],
        )
