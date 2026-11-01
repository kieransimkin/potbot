"""Transactional agent skill authoring and workspace-wide, digest-pinned discovery.

This service borrows an authenticated context-session transaction. It never
opens an admin connection, executes skill text or accepts a filesystem path.
Published code and live MODEL/MEMORY documents have separate revision histories.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid5

from potbot.context.model import document_templates, sha256
from potbot.context.rpc_contract import SkillRequest
from potbot.context.service import WorkspaceContextService
from potbot.context.skills import validate_skill
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError

if TYPE_CHECKING:
    from potbot.context.sessions import ContextSession

_DENIED = "Skill is unavailable or the operation is not authorized"


def skill_identity(workspace_id: UUID, agent_id: UUID, slug: str) -> tuple[UUID, UUID, str]:
    """An agent cannot squat an operator or another agent's namespace."""
    import re

    if (not isinstance(workspace_id, UUID) or not isinstance(agent_id, UUID)
            or not isinstance(slug, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", slug)):
        raise InvalidInputError("Invalid authored skill identity")
    name = f"agent-{agent_id.hex}.{slug}"
    identity = uuid5(NAMESPACE_URL, f"potbot:authored-skill:{workspace_id}:{name}")
    obj = uuid5(NAMESPACE_URL, f"potbot:context:{workspace_id}:skill:{name}")
    return identity, obj, name


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "skill_id": str(row["id"]), "name": row["slug"],
        "namespace": "agent-" + row["owner_agent_id"].hex,
        "context_name": row["context_name"], "context_object_id": str(row["context_object_id"]),
        "revision": row["draft_revision"], "release_sha256": row["latest_sha256"],
        "state": row["state"], "trusted": False, "automatically_activated": False,
    }


def published_skills(conn: Any, context: WorkspaceContext, *, limit: int = 25,
                     after: UUID | None = None) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= 50 or (after is not None and not isinstance(after, UUID)):
        raise InvalidInputError("Invalid skill page")
    rows = conn.execute(
        "SELECT * FROM workspace_context_skills WHERE workspace_id=%s AND published "
        "AND state='active' AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s",
        (context.workspace_id, after, after, limit + 1),
    ).fetchall()
    page = rows[:limit]
    return {"skills": [_summary(dict(row)) for row in page],
            "next": str(page[-1]["id"]) if len(rows) > limit else None}


def load_skill(conn: Any, context: WorkspaceContext, service: WorkspaceContextService,
               skill_id: UUID, release_sha256: str) -> dict[str, Any]:
    """Read an explicit immutable release plus one coherent live context triple.

    Returned text is data for an explicitly selected skill, not a higher-priority
    instruction or permission to execute anything. No implicit 'latest' loading.
    """
    row = conn.execute(
        "SELECT k.*,r.source_revision,v.content,v.sha256 AS content_sha256 "
        "FROM workspace_context_skills k JOIN workspace_context_skill_releases r "
        "ON r.workspace_id=k.workspace_id AND r.skill_id=k.id "
        "JOIN workspace_context_skill_versions v ON v.workspace_id=r.workspace_id "
        "AND v.skill_id=r.skill_id AND v.revision=r.source_revision "
        "WHERE k.workspace_id=%s AND k.id=%s AND r.sha256=%s "
        "AND k.published AND k.state='active'",
        (context.workspace_id, skill_id, release_sha256),
    ).fetchone()
    if row is None:
        raise ScopeDeniedError(_DENIED)
    if row["content_sha256"] != release_sha256 or sha256(row["content"]) != release_sha256:
        raise RevisionConflictError("Published skill integrity check failed")
    docs = service.read_triple(context, "skill", row["context_name"])
    return {**_summary(dict(row)), "revision": row["source_revision"],
            "sha256": release_sha256, "content": row["content"], "context": docs,
            "classification": "untrusted-skill-data"}


class SessionSkillCatalogue:
    """No independent commits: call only inside ContextSessionStore.execute()."""

    def __init__(self, conn: Any, grant: ContextSession, session: dict[str, Any],
                 service: WorkspaceContextService):
        self.conn, self.grant, self.session, self.service = conn, grant, session, service
        self.context = grant.principal

    def _row(self, skill_id: UUID, *, write: bool) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM workspace_context_skills WHERE workspace_id=%s AND id=%s" +
            (" FOR UPDATE" if write else ""), (self.context.workspace_id, skill_id),
        ).fetchone()
        if row is None:
            raise ScopeDeniedError(_DENIED)
        row = dict(row)
        owns = (row["owner_principal_id"] == self.context.principal_id
                and row["owner_agent_id"] == self.grant.agent_id)
        # Agent isolation is checked even when the host login owns both agents.
        if (write and not owns) or (not write and not row["published"] and not owns):
            raise ScopeDeniedError(_DENIED)
        return row

    def authorize(self, request: SkillRequest) -> None:
        # Recheck ownership before idempotent replay. An archived skill's original
        # receipt can be recovered, but execute() refuses any new mutation to it.
        if request.method in ("skills.save", "skills.publish", "skills.archive"):
            assert request.skill_id is not None
            self._row(request.skill_id, write=True)

    def context_target(self, skill_id: UUID, *, write: bool) -> tuple[str, UUID]:
        row = self._row(skill_id, write=write)
        if row["state"] != "active":
            raise ScopeDeniedError(_DENIED)
        # For writes the row lock serializes with archival/revocation. Other
        # agents may read published skill context, but cannot alter the author's.
        return row["context_name"], row["context_object_id"]

    def _version(self, row: dict[str, Any]) -> dict[str, Any]:
        version = self.conn.execute(
            "SELECT revision,content,sha256 FROM workspace_context_skill_versions "
            "WHERE workspace_id=%s AND skill_id=%s AND revision=%s",
            (self.context.workspace_id, row["id"], row["draft_revision"]),
        ).fetchone()
        if version is None or sha256(version["content"]) != version["sha256"]:
            raise RevisionConflictError("Skill draft history is inconsistent")
        return dict(version)

    def _insert_version(self, identity: UUID, revision: int, content: str) -> str:
        checksum = sha256(content)
        self.conn.execute(
            "INSERT INTO workspace_context_skill_versions "
            "(workspace_id,skill_id,revision,content,sha256,session_id) VALUES (%s,%s,%s,%s,%s,%s)",
            (self.context.workspace_id, identity, revision, content, checksum, self.grant.session_id),
        )
        return checksum

    def _seed_context(self, object_id: UUID, name: str) -> None:
        from psycopg.types.json import Jsonb

        context = self.context
        # Metadata was inserted first with a deferred FK. RLS admits ONLY that
        # exact shared skill context object, not arbitrary shared primitives.
        self.conn.execute(
            "INSERT INTO workspace_context_objects (workspace_id,id,kind,name,visibility) "
            "VALUES (%s,%s,'skill',%s,'shared')", (context.workspace_id, object_id, name),
        )
        actor = f"principal:{context.principal_id}"
        for role, text in document_templates("skill", name).items():
            digest = sha256(text)
            self.conn.execute(
                "INSERT INTO workspace_context_documents "
                "(workspace_id,object_id,role,content,revision,sha256) VALUES (%s,%s,%s,%s,1,%s)",
                (context.workspace_id, object_id, role, text, digest),
            )
            self.conn.execute(
                "INSERT INTO workspace_context_revisions "
                "(workspace_id,object_id,role,revision,content,sha256,actor,reason) "
                "VALUES (%s,%s,%s,1,%s,%s,%s,'initial authored skill context')",
                (context.workspace_id, object_id, role, text, digest, actor),
            )
            self.conn.execute(
                "INSERT INTO workspace_context_events "
                "(workspace_id,object_id,role,revision,actor,event_type,payload) "
                "VALUES (%s,%s,%s,1,%s,'document.provisioned',%s)",
                (context.workspace_id, object_id, role, actor, Jsonb({"sha256": digest,
                    "agent_id": str(self.grant.agent_id), "run_id": str(self.grant.run_id),
                    "session_id": str(self.grant.session_id)})),
            )

    def execute(self, request: SkillRequest) -> dict[str, Any]:
        context = self.context
        if request.method == "skills.list":
            return published_skills(self.conn, context, limit=request.limit, after=request.after)
        if request.method == "skills.load":
            assert request.skill_id is not None and request.release_sha256 is not None
            return load_skill(self.conn, context, self.service, request.skill_id, request.release_sha256)
        if request.method == "skills.create":
            assert request.name is not None and request.content is not None
            validate_skill(request.name, request.content)
            identity, object_id, name = skill_identity(context.workspace_id, self.grant.agent_id, request.name)
            row = self.conn.execute(
                "INSERT INTO workspace_context_skills "
                "(workspace_id,id,owner_principal_id,owner_agent_id,agent_object_id,slug,"
                "context_object_id,context_name) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (workspace_id,owner_agent_id,slug) DO NOTHING RETURNING *",
                (context.workspace_id, identity, context.principal_id, self.grant.agent_id,
                 self.session["agent_object_id"], request.name, object_id, name),
            ).fetchone()
            if row is None:
                raise RevisionConflictError("Skill name already exists in this agent's namespace")
            self._seed_context(object_id, name)
            digest = self._insert_version(identity, 1, request.content)
            return {**_summary(dict(row)), "sha256": digest, "reason": request.reason}
        assert request.skill_id is not None
        row = self._row(request.skill_id, write=True)
        if row["state"] != "active":
            raise ScopeDeniedError(_DENIED)
        if row["draft_revision"] != request.expected_revision:
            raise RevisionConflictError("Skill draft revision is stale")
        if request.method == "skills.save":
            assert request.content is not None
            validate_skill(row["slug"], request.content)
            revision = row["draft_revision"] + 1
            digest = self._insert_version(row["id"], revision, request.content)
            self.conn.execute(
                "UPDATE workspace_context_skills SET draft_revision=%s,updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND id=%s", (revision, context.workspace_id, row["id"]),
            )
            row["draft_revision"] = revision
            return {**_summary(row), "sha256": digest, "reason": request.reason}
        if request.method == "skills.publish":
            version = self._version(row)
            self.conn.execute(
                "INSERT INTO workspace_context_skill_releases "
                "(workspace_id,skill_id,sha256,source_revision,session_id) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (workspace_id,skill_id,sha256) DO NOTHING",
                (context.workspace_id, row["id"], version["sha256"], version["revision"], self.grant.session_id),
            )
            self.conn.execute(
                "UPDATE workspace_context_skills SET published=true,latest_sha256=%s,"
                "updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s",
                (version["sha256"], context.workspace_id, row["id"]),
            )
            row["latest_sha256"] = version["sha256"]
            return {**_summary(row), "sha256": version["sha256"], "reason": request.reason}
        if request.method == "skills.archive":
            self.conn.execute(
                "UPDATE workspace_context_skills SET state='archived',updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND id=%s", (context.workspace_id, row["id"]),
            )
            row["state"] = "archived"
            return {**_summary(row), "reason": request.reason}
        raise InvalidInputError("Unsupported skill operation")
