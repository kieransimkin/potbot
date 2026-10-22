"""Durable, scoped inherited Ember contracts and explicitly public projections."""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID, uuid4

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import canonical_json
from potbot.domain.syn import SynType
from potbot.persistence.database import Database

from .catalog import canonical_type_ref, catalog_fingerprint, type_ancestors
from .embers import EmberContractBundle, EmberContractResolver, EmberContractScope
from .service import SynService


class EmberContractService:
    """Keep private bindings outside artifacts, vectors and public SYN payloads."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _catalog(
        connection: Any, context: WorkspaceContext, snapshot_id: UUID
    ) -> tuple[tuple[SynType, ...], EmberContractScope]:
        snapshot = SynService._snapshot(connection, context, snapshot_id)
        rows = connection.execute(
            "SELECT definition FROM syn_types WHERE workspace_id=%s AND snapshot_id=%s "
            'ORDER BY ref COLLATE "C"',
            (context.workspace_id, snapshot_id),
        ).fetchall()
        types = tuple(SynType.from_dict(row["definition"]) for row in rows)
        if catalog_fingerprint(types) != snapshot["catalog_hash"]:
            raise InvalidInputError("Stored Ember catalog fingerprint is inconsistent")
        scope = EmberContractScope(
            workspace_id=context.workspace_id,
            namespace=snapshot["namespace"],
            snapshot_id=snapshot_id,
            catalog_hash=snapshot["catalog_hash"],
        )
        return types, scope

    def import_bundle(
        self, context: WorkspaceContext, snapshot_id: UUID, bundle: EmberContractBundle
    ) -> dict[str, Any]:
        """Validate every instance and atomically persist owner and public views."""
        from psycopg.types.json import Jsonb

        if not isinstance(bundle, EmberContractBundle) or bundle.projection != "owner":
            raise InvalidInputError("Import requires a complete owner Ember bundle")
        with self.database.transaction(context) as connection:
            types, scope = self._catalog(connection, context, snapshot_id)
            resolver = EmberContractResolver(types, bundle, expected_scope=scope)
            self._validate_subjects(connection, context, snapshot_id, resolver)
            resolver.validate_all(projection="owner")
            public = resolver.public_projection()
            owner_hash, public_hash = bundle.fingerprint, public.fingerprint
            # Same-owner duplicate imports serialize without exposing the private
            # digest in shared command-result records. The UNIQUE constraint is
            # a second guard; the lock lets concurrent callers reuse one result.
            lock_material = canonical_json(
                [
                    "syn-ember-owner-bundle-v1",
                    str(context.workspace_id),
                    str(snapshot_id),
                    str(context.principal_id),
                    owner_hash,
                ]
            ).encode()
            lock = int.from_bytes(hashlib.sha256(lock_material).digest()[:8], "big", signed=True)
            connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (lock,))
            prior = connection.execute(
                "SELECT o.id,p.public_hash FROM syn_ember_owner_bundles o "
                "JOIN syn_ember_public_bundles p ON "
                "(p.workspace_id,p.id,p.snapshot_id)=(o.workspace_id,o.id,o.snapshot_id) "
                "WHERE o.workspace_id=%s AND o.snapshot_id=%s "
                "AND o.owner_principal_id=%s AND o.owner_hash=%s",
                (context.workspace_id, snapshot_id, context.principal_id, owner_hash),
            ).fetchone()
            if prior is not None:
                if prior["public_hash"] != public_hash:
                    raise InvalidInputError("Stored Ember public projection is inconsistent")
                return self._result(prior["id"], snapshot_id, public_hash)
            identity = uuid4()
            connection.execute(
                "INSERT INTO syn_ember_public_bundles "
                "(workspace_id,id,snapshot_id,public_hash,document) VALUES (%s,%s,%s,%s,%s)",
                (context.workspace_id, identity, snapshot_id, public_hash, Jsonb(public.to_dict())),
            )
            connection.execute(
                "INSERT INTO syn_ember_owner_bundles "
                "(workspace_id,id,snapshot_id,owner_principal_id,owner_hash,document) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    context.workspace_id,
                    identity,
                    snapshot_id,
                    context.principal_id,
                    owner_hash,
                    Jsonb(bundle.to_dict()),
                ),
            )
            return self._result(identity, snapshot_id, public_hash)

    @staticmethod
    def _validate_subjects(
        connection: Any,
        context: WorkspaceContext,
        snapshot_id: UUID,
        resolver: EmberContractResolver,
    ) -> None:
        rows = connection.execute(
            "SELECT ref,type_ref FROM syn_objects WHERE workspace_id=%s AND snapshot_id=%s",
            (context.workspace_id, snapshot_id),
        ).fetchall()
        resolver.validate_subject_types({row["ref"]: row["type_ref"] for row in rows})

    @staticmethod
    def _result(identity: UUID, snapshot_id: UUID, public_hash: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "bundle_id": str(identity),
            "snapshot_id": str(snapshot_id),
            "public_hash": public_hash,
        }

    def read_bundle(
        self, context: WorkspaceContext, bundle_id: UUID, *, projection: str = "public"
    ) -> tuple[EmberContractBundle, tuple[SynType, ...]]:
        if projection not in {"owner", "public"}:
            raise InvalidInputError("Unknown Ember projection")
        # Table and digest columns are closed application constants, never SQL input.
        table = "syn_ember_owner_bundles" if projection == "owner" else "syn_ember_public_bundles"
        digest = "owner_hash" if projection == "owner" else "public_hash"
        with self.database.transaction(context) as connection:
            row = connection.execute(
                f"SELECT snapshot_id,{digest} AS digest,document FROM {table} "
                "WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, bundle_id),
            ).fetchone()
            if row is None:
                raise ScopeDeniedError("Ember bundle is unavailable in this context")
            bundle = EmberContractBundle.from_dict(row["document"])
            if bundle.projection != projection or bundle.fingerprint != row["digest"]:
                raise InvalidInputError("Stored Ember bundle fingerprint is inconsistent")
            types, scope = self._catalog(connection, context, row["snapshot_id"])
            resolver = EmberContractResolver(types, bundle, expected_scope=scope)
            self._validate_subjects(connection, context, row["snapshot_id"], resolver)
            resolver.validate_all(projection=projection)
            return bundle, types

    def resolve(
        self,
        context: WorkspaceContext,
        bundle_id: UUID,
        object_ref: str,
        *,
        projection: str = "public",
    ) -> dict[str, Any]:
        bundle, types = self.read_bundle(context, bundle_id, projection=projection)
        return (
            EmberContractResolver(types, bundle, expected_scope=bundle.scope)
            .resolve(object_ref, projection=projection)
            .to_dict()
        )

    def list_embers(
        self,
        context: WorkspaceContext,
        bundle_id: UUID,
        *,
        type_ref: str,
        include_subtypes: bool = False,
        projection: str = "public",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Enumerate visible Ember revisions by their own Type in one pinned bundle.

        The represented value Type is a separate assertion and is not the Ember's
        own Type. Public totals concern the public projection only; they make no
        claim about the existence or absence of owner-private revisions.
        """
        if type(include_subtypes) is not bool:
            raise InvalidInputError("include_subtypes must be a boolean")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise InvalidInputError("Ember inventory limit must be between 1 and 1000")
        if type(offset) is not int or not 0 <= offset <= 10000:
            raise InvalidInputError("Ember inventory offset must be between 0 and 10000")
        selected_type = str(canonical_type_ref(type_ref))
        bundle, types = self.read_bundle(context, bundle_id, projection=projection)
        ancestors = type_ancestors(types)
        if selected_type not in ancestors:
            raise InvalidInputError("Ember inventory Type is absent from the selected catalog")
        matching = sorted(
            (
                ember
                for ember in bundle.embers
                if selected_type
                in (
                    ancestors[str(canonical_type_ref(ember.type_ref))]
                    if include_subtypes
                    else (str(canonical_type_ref(ember.type_ref)),)
                )
            ),
            key=lambda ember: (ember.ref, ember.revision),
        )
        return {
            "schema_version": 1,
            "bundle_id": str(bundle_id),
            "scope": bundle.scope.to_dict(),
            "projection": projection,
            "coverage": "selected_bundle",
            "record_kind": "ember_revisions",
            "type_ref": selected_type,
            "type_filter": "ember_type",
            "include_subtypes": include_subtypes,
            "total_count": len(matching),
            "offset": offset,
            "limit": limit,
            "items": [ember.to_dict() for ember in matching[offset : offset + limit]],
        }
