"""Scoped semantic catalogs and frozen task/packet release extensions."""

from __future__ import annotations

from functools import lru_cache
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    CapabilityUnavailableError,
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.serialization import content_hash
from potbot.persistence.database import Database
from potbot.semantics.registry import CONSUMER_CONTRACT, SemanticRegistry


@lru_cache(maxsize=1)
def bundled_registry() -> SemanticRegistry:
    return SemanticRegistry.load()


def register_release(
    connection: Any,
    context: WorkspaceContext,
    registry: SemanticRegistry,
    *,
    consumer_contract: str,
) -> dict[str, Any]:
    """An installation-owned operation; proposal frames cannot select releases."""
    from psycopg.types.json import Jsonb

    if consumer_contract != CONSUMER_CONTRACT:
        raise CapabilityUnavailableError("semantic release lacks a supported consumer declaration")
    manifest = registry.manifest
    documents = registry.documents()
    for name, component in manifest["components"].items():
        key = name + "#" + component["identity"]
        connection.execute(
            "INSERT INTO semantic_component_releases(workspace_id,component_key,content_hash,content) "
            "VALUES(%s,%s,%s,%s) ON CONFLICT(workspace_id,component_key) DO NOTHING",
            (context.workspace_id, key, component["content_hash"], Jsonb(documents[name])),
        )
        previous = connection.execute(
            "SELECT content_hash,content FROM semantic_component_releases "
            "WHERE workspace_id=%s AND component_key=%s",
            (context.workspace_id, key),
        ).fetchone()
        if (
            previous["content_hash"] != component["content_hash"]
            or previous["content"] != documents[name]
        ):
            raise RevisionConflictError(
                "a published semantic component release cannot be redefined"
            )
    release_id = uuid5(NAMESPACE_URL, registry.release_key)
    connection.execute(
        "INSERT INTO semantic_releases(workspace_id,id,release_key,content_hash,consumer_contract,manifest,documents) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(workspace_id,release_key) DO NOTHING",
        (
            context.workspace_id,
            release_id,
            registry.release_key,
            registry.fingerprint,
            consumer_contract,
            Jsonb(manifest),
            Jsonb(documents),
        ),
    )
    release = read_release(connection, context, release_id)
    if release["manifest"] != manifest or release["documents"] != documents:
        raise RevisionConflictError("semantic release hash collision or changed content")
    for kind in manifest["catalogs"]:
        for definition in registry.inventory(kind):
            connection.execute(
                "INSERT INTO semantic_catalog_entries(workspace_id,release_id,kind,entry_id,content_hash,definition) "
                "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    context.workspace_id,
                    release_id,
                    kind,
                    definition["id"],
                    content_hash(definition),
                    Jsonb(definition),
                ),
            )
        catalog_inventory(connection, context, release_id, kind)
    return release


def read_release(connection: Any, context: WorkspaceContext, release_id: UUID) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM semantic_releases WHERE workspace_id=%s AND id=%s",
        (context.workspace_id, release_id),
    ).fetchone()
    if row is None:
        raise ScopeDeniedError("semantic release is unavailable")
    if content_hash(row["manifest"]) != row["content_hash"]:
        raise StateMismatchError("semantic release manifest integrity failed")
    if row["release_key"] != "urn:potbot:semantic-release:sha256:" + row["content_hash"]:
        raise StateMismatchError("semantic release key does not match its content identity")
    if any(
        name not in row["documents"]
        or content_hash(row["documents"][name]) != definition["content_hash"]
        for name, definition in row["manifest"]["components"].items()
    ):
        raise StateMismatchError("semantic release component integrity failed")
    if row["consumer_contract"] != CONSUMER_CONTRACT:
        raise CapabilityUnavailableError("semantic consumer compatibility has not been declared")
    return dict(row)


def catalog_inventory(
    connection: Any, context: WorkspaceContext, release_id: UUID, kind: str
) -> list[dict[str, Any]]:
    release = read_release(connection, context, release_id)
    expected = release["manifest"]["catalogs"].get(kind)
    if expected is None:
        raise InvalidInputError("unknown semantic catalog kind")
    rows = connection.execute(
        "SELECT entry_id,definition,content_hash FROM semantic_catalog_entries "
        "WHERE workspace_id=%s AND release_id=%s AND kind=%s ORDER BY entry_id",
        (context.workspace_id, release_id, kind),
    ).fetchall()
    definitions = {row["entry_id"]: row["definition"] for row in rows}
    if (
        len(rows) != expected["count"]
        or sorted(definitions) != expected["identifiers"]
        or content_hash(definitions) != expected["content_hash"]
        or any(content_hash(row["definition"]) != row["content_hash"] for row in rows)
    ):
        raise StateMismatchError("semantic catalog inventory is incomplete or inconsistent")
    return [dict(row["definition"]) for row in rows]


def active_release(connection: Any, context: WorkspaceContext) -> dict[str, Any]:
    """Seed the bundled release only when no installation selection exists."""
    selected = connection.execute(
        "SELECT release_id FROM semantic_active_releases WHERE workspace_id=%s FOR SHARE",
        (context.workspace_id,),
    ).fetchone()
    if selected is None:
        release = register_release(
            connection, context, bundled_registry(), consumer_contract=CONSUMER_CONTRACT
        )
        connection.execute(
            "INSERT INTO semantic_active_releases(workspace_id,release_id) VALUES(%s,%s) "
            "ON CONFLICT(workspace_id) DO NOTHING",
            (context.workspace_id, release["id"]),
        )
        selected = connection.execute(
            "SELECT release_id FROM semantic_active_releases WHERE workspace_id=%s FOR SHARE",
            (context.workspace_id,),
        ).fetchone()
    return read_release(connection, context, selected["release_id"])


def release_snapshot(release: dict[str, Any]) -> dict[str, Any]:
    components = release["manifest"]["components"]
    return {
        "schema_version": 1,
        "extension": "semantic-consumer-v1",
        "release_id": str(release["id"]),
        "release_key": release["release_key"],
        "release_digest": release["content_hash"],
        "consumer_contract": release["consumer_contract"],
        "vocabulary": components["vocabulary.json"],
        "type_registry": components["data-types.json"],
        "classifier_policy": {
            "identity": components["data-types.json"]["identity"] + "#classification_rules",
            "content_hash": content_hash(
                release["documents"]["data-types.json"]["classification_rules"]
            ),
        },
        "query_profiles": components["retrieval-profiles.json"],
    }


def pin_task(
    connection: Any,
    context: WorkspaceContext,
    episode_id: UUID,
    configuration_id: UUID,
) -> dict[str, Any]:
    from psycopg.types.json import Jsonb

    config = connection.execute(
        "SELECT episode_id FROM resolved_configurations WHERE workspace_id=%s AND id=%s",
        (context.workspace_id, configuration_id),
    ).fetchone()
    if config is None or config["episode_id"] != episode_id:
        raise ScopeDeniedError("configuration does not belong to the semantic consumer task")
    release = active_release(connection, context)
    snapshot = release_snapshot(release)
    connection.execute(
        "INSERT INTO task_semantic_pins(workspace_id,episode_id,release_id,configuration_id,content_hash,content) "
        "VALUES(%s,%s,%s,%s,%s,%s)",
        (
            context.workspace_id,
            episode_id,
            release["id"],
            configuration_id,
            content_hash(snapshot),
            Jsonb(snapshot),
        ),
    )
    result = read_task_pin(connection, context, episode_id)
    assert result is not None
    return result


def read_task_pin(
    connection: Any, context: WorkspaceContext, episode_id: UUID, *, required: bool = True
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM task_semantic_pins WHERE workspace_id=%s AND episode_id=%s",
        (context.workspace_id, episode_id),
    ).fetchone()
    if row is None:
        if required:
            raise CapabilityUnavailableError("task has no declared semantic consumer extension")
        return None
    release = read_release(connection, context, row["release_id"])
    if content_hash(row["content"]) != row["content_hash"] or row["content"] != release_snapshot(
        release
    ):
        raise StateMismatchError("frozen task semantic identity is inconsistent")
    return dict(row)


def packet_snapshot(
    connection: Any, context: WorkspaceContext, episode_id: UUID | None
) -> dict[str, Any]:
    if episode_id is None:
        return release_snapshot(active_release(connection, context))
    pin = read_task_pin(connection, context, episode_id)
    assert pin is not None
    return dict(pin["content"])


def pin_packet(
    connection: Any, context: WorkspaceContext, packet_id: UUID, snapshot: dict[str, Any]
) -> None:
    from psycopg.types.json import Jsonb

    release_id = UUID(snapshot["release_id"])
    release = read_release(connection, context, release_id)
    if snapshot != release_snapshot(release):
        raise StateMismatchError("packet semantic snapshot does not match its release")
    connection.execute(
        "INSERT INTO packet_semantic_pins(workspace_id,packet_id,release_id,content_hash,content) "
        "VALUES(%s,%s,%s,%s,%s)",
        (context.workspace_id, packet_id, release_id, content_hash(snapshot), Jsonb(snapshot)),
    )


def check_packet_pin(connection: Any, context: WorkspaceContext, packet_id: UUID) -> None:
    row = connection.execute(
        "SELECT p.content,s.content AS semantic_content,s.content_hash,s.release_id "
        "FROM context_packets p LEFT JOIN packet_semantic_pins s "
        "ON (s.workspace_id,s.packet_id)=(p.workspace_id,p.id) WHERE p.workspace_id=%s AND p.id=%s",
        (context.workspace_id, packet_id),
    ).fetchone()
    if row is None:
        raise ScopeDeniedError("context packet is unavailable")
    if row["content"].get("schema_version") == 1 and row["semantic_content"] is None:
        # Explicit legacy profile; it can be inspected and replayed as its original contract.
        return
    if row["semantic_content"] is None:
        raise CapabilityUnavailableError("context packet is missing its semantic extension")
    release = read_release(connection, context, row["release_id"])
    snapshot = row["semantic_content"]
    if (
        content_hash(snapshot) != row["content_hash"]
        or snapshot != release_snapshot(release)
        or row["content"].get("semantic_release") != snapshot
    ):
        raise StateMismatchError("context semantic identity is inconsistent")


class SemanticReleaseStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def register(
        self,
        context: WorkspaceContext,
        registry: SemanticRegistry | None = None,
        *,
        consumer_contract: str | None = None,
    ) -> dict[str, Any]:
        selected = registry or bundled_registry()
        if consumer_contract is None:
            if selected.fingerprint != bundled_registry().fingerprint:
                raise CapabilityUnavailableError(
                    "candidate semantic releases require an explicit compatibility declaration"
                )
            consumer_contract = CONSUMER_CONTRACT
        with self.database.transaction(context) as connection:
            return register_release(
                connection,
                context,
                selected,
                consumer_contract=consumer_contract,
            )

    def inspect(self, context: WorkspaceContext, release_id: UUID) -> dict[str, Any]:
        with self.database.transaction(context) as connection:
            return read_release(connection, context, release_id)

    def inventory(
        self, context: WorkspaceContext, release_id: UUID, kind: str
    ) -> list[dict[str, Any]]:
        with self.database.transaction(context) as connection:
            return catalog_inventory(connection, context, release_id, kind)

    def select(
        self, context: WorkspaceContext, release_id: UUID, *, expected_token: int | None
    ) -> dict[str, Any]:
        if expected_token is not None and (type(expected_token) is not int or expected_token < 0):
            raise InvalidInputError("selection token must be a nonnegative integer")
        with self.database.transaction(context) as connection:
            read_release(connection, context, release_id)
            # Serialize first selection as well as later CAS changes.
            connection.execute(
                "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s,0))",
                (str(context.workspace_id) + ":semantic-selection",),
            )
            current = connection.execute(
                "SELECT * FROM semantic_active_releases WHERE workspace_id=%s FOR UPDATE",
                (context.workspace_id,),
            ).fetchone()
            if current is None:
                if expected_token is not None:
                    raise RevisionConflictError("semantic selection has no expected predecessor")
                row = connection.execute(
                    "INSERT INTO semantic_active_releases(workspace_id,release_id) VALUES(%s,%s) RETURNING *",
                    (context.workspace_id, release_id),
                ).fetchone()
            else:
                if current["update_token"] != expected_token:
                    raise RevisionConflictError("semantic selection changed")
                row = connection.execute(
                    "UPDATE semantic_active_releases SET release_id=%s,update_token=update_token+1,"
                    "updated_at=clock_timestamp() WHERE workspace_id=%s RETURNING *",
                    (release_id, context.workspace_id),
                ).fetchone()
            return dict(row)
