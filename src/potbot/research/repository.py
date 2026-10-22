"""Bounded source inspection exclusively from a pinned immutable repository snapshot.

No working-tree reads, directory traversal, network access or code execution.
Snapshot capture and authorisation remain duties of the repository controller.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from potbot.domain.errors import InvalidInputError, RevisionConflictError
from potbot.research.contracts import (
    ObservationStatus,
    ResearchAction,
    ResearchCategory,
    ResearchObservation,
    ResearchOperation,
)
from potbot.tools.repository_snapshot import RepositorySnapshot

_EXAMPLE_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".java", ".cs", ".cpp", ".c")


class SnapshotResearchAdapter:
    """Inspect exactly the controller-approved snapshot, never a mutable checkout."""

    def __init__(self, snapshot: RepositorySnapshot, *, repository_state_id: UUID,
                 workspace_id: UUID, task_id: UUID):
        if not isinstance(snapshot, RepositorySnapshot):
            raise InvalidInputError("source inspector requires validated snapshot bytes")
        if any(not isinstance(item, UUID) for item in
               (repository_state_id, workspace_id, task_id)):
            raise InvalidInputError("source inspector requires exact scope references")
        self.snapshot = snapshot
        self.repository_state_id = repository_state_id
        self.workspace_id = workspace_id
        self.task_id = task_id
        manifest: dict[str, Any] = json.loads(snapshot.payload)
        self.entries: dict[str, dict[str, Any]] = {
            entry["path"]: entry for entry in manifest["files"]
        }
        self.revision = snapshot.content_digest

    def inspect(self, action: ResearchAction, *, now: datetime | None = None) -> tuple[ResearchObservation, ...]:
        if not isinstance(action, ResearchAction) or action.query.category != ResearchCategory.REPOSITORY:
            raise InvalidInputError("repository adapter accepts typed repository actions only")
        scope = action.scope
        if (scope.workspace_id, scope.task_id, scope.repository_state_id) != (
            self.workspace_id, self.task_id, self.repository_state_id
        ):
            raise RevisionConflictError("research scope does not match the pinned repository")
        timestamp = datetime.now(UTC) if now is None else now
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            raise InvalidInputError("retrieval timestamp must be timezone aware")
        q = action.query

        def observation(status: ObservationStatus, resolved: str, *, digest: str | None = None,
                        excerpt: str | None = None, first: int | None = None,
                        last: int | None = None, complete: bool = True) -> ResearchObservation:
            return ResearchObservation(
                action_id=action.action_id, status=status,
                requested_locator=q.locator, resolved_locator=resolved,
                source_revision=self.revision, retrieved_at=timestamp,
                content_digest=digest, excerpt=excerpt,
                line_start=first, line_end=last, scan_complete=complete,
            )

        if q.operation == ResearchOperation.READ_FILE:
            entry = self.entries.get(q.locator)
            if entry is None:
                return (observation(ObservationStatus.NOT_FOUND, q.locator),)
            if entry["kind"] != "file":
                return (observation(ObservationStatus.UNAVAILABLE, q.locator),)
            if entry["size"] > q.max_source_bytes:
                return (observation(ObservationStatus.OVER_LIMIT, q.locator, complete=False),)
            raw = base64.b64decode(entry["data"], validate=True)
            try:
                text = raw.decode("utf-8")
            except UnicodeError:
                return (observation(ObservationStatus.NON_TEXT, q.locator,
                                    digest=entry["sha256"]),)
            if not text:
                return (observation(ObservationStatus.NO_MATCH, q.locator,
                                    digest=entry["sha256"]),)
            return (observation(ObservationStatus.INSPECTED, q.locator,
                                digest=entry["sha256"], excerpt=text,
                                first=1, last=len(text.splitlines())),)

        if q.operation not in {ResearchOperation.FIND_LITERAL, ResearchOperation.SEARCH_EXAMPLE_CODE}:
            raise InvalidInputError("unsupported repository inspection operation")

        # Prefixes are literal slash-separated directories. There is no globbing,
        # shell expansion or filesystem access. Stable sorted traversal supports replay.
        allowed = [name for name in sorted(self.entries)
                   if name == q.locator or name.startswith(q.locator + "/")]
        if not allowed:
            return (observation(ObservationStatus.NOT_FOUND, q.locator),)
        if q.operation == ResearchOperation.SEARCH_EXAMPLE_CODE:
            allowed = [name for name in allowed if name.endswith(_EXAMPLE_SUFFIXES)]
        matches: list[ResearchObservation] = []
        inspected_bytes = 0
        complete = True
        for path in allowed:
            entry = self.entries[path]
            if entry["kind"] != "file":
                continue  # no symlink traversal, including internal symlinks
            size = entry["size"]
            if inspected_bytes + size > q.max_source_bytes:
                complete = False
                break
            inspected_bytes += size
            raw = base64.b64decode(entry["data"], validate=True)
            try:
                text = raw.decode("utf-8")
            except UnicodeError:
                continue
            for line_number, line in enumerate(text.splitlines(), 1):
                if q.search_text not in line:
                    continue
                if not line.strip():
                    continue
                # A bounded, actual inspected excerpt (not a search-index snippet).
                lines = text.splitlines()
                start = max(1, line_number - 2)
                end = min(len(lines), line_number + 2)
                excerpt = "\n".join(lines[start - 1:end])
                while len(excerpt.encode("utf-8")) > 8192 and end > line_number:
                    end -= 1
                    excerpt = "\n".join(lines[start - 1:end])
                if len(excerpt.encode("utf-8")) > 8192:
                    complete = False
                    continue
                matches.append(observation(
                    ObservationStatus.INSPECTED, path, digest=entry["sha256"],
                    excerpt=excerpt, first=start, last=end, complete=False,
                ))
                if len(matches) >= q.max_results:
                    complete = False
                    break
            if len(matches) >= q.max_results:
                break
        if not matches:
            status = ObservationStatus.NO_MATCH if complete else ObservationStatus.OVER_LIMIT
            return (observation(status, q.locator, complete=complete),)
        # Completeness describes whether the search exhausted its declared scope.
        return tuple(ResearchObservation(
            action_id=hit.action_id, status=hit.status,
            requested_locator=hit.requested_locator,
            resolved_locator=hit.resolved_locator,
            source_revision=hit.source_revision, retrieved_at=hit.retrieved_at,
            content_digest=hit.content_digest, excerpt=hit.excerpt,
            line_start=hit.line_start, line_end=hit.line_end,
            scan_complete=complete,
        ) for hit in matches)
