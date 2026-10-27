"""Trusted browser-control service bound to one operator Xpra workspace.

No browser is launched here. No CDP host port is exposed. All browser control
passes through the private, bounded Xpra browser RPC of a previously admitted
session. This is a *controller-owned control-plane* service, not a general
Python work handler. Only exact SYN Cogs and independently admitted sessions
can invoke it. Raw DOM data and JPEGs are stored privately as workspace artifacts.
"""
from __future__ import annotations

import json
import threading
from typing import Any
from uuid import UUID

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    IndeterminateExecutionError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.tools.browser_cog_contracts import SESSION_ID, validate_request
from potbot.tools.browser_evidence import BrowserEvidenceRecorder, BrowserEvidenceScope
from potbot.tools.browser_session_leases import BrowserLease
from potbot.tools.browser_gallery import BrowserEvidenceGallery
from potbot.tools.execution_contracts import ExecutionPolicy
from potbot.tools.xpra_app_sessions import XpraApplicationSupervisor


class BrowserControlService:
    """Host-installed browser lease and private evidence boundary.

    Approved session IDs are selected by the operator, never by a model. The
    authorizing ExecutionCogRuntime still calls its independent `admit` hook
    before and after the effect. A new invocation ID is one-use even on error.
    """

    def __init__(self, *, actor: WorkspaceContext, apps: XpraApplicationSupervisor,
                 artifacts: Any, policy: ExecutionPolicy,
                 approved_sessions: tuple[str, ...],
                 allow_model_preview: bool = False,
                 gallery: BrowserEvidenceGallery | None = None,
                 reservations: tuple[BrowserLease, ...] = ()) -> None:
        if not isinstance(actor, WorkspaceContext) or not isinstance(
            apps, XpraApplicationSupervisor
        ) or apps.workspace_id != actor.workspace_id or apps.principal_id != actor.principal_id:
            raise ScopeDeniedError("browser control requires the owning operator workspace")
        if not isinstance(policy, ExecutionPolicy) or not isinstance(
            approved_sessions, tuple
        ) or not 1 <= len(approved_sessions) <= 12 or len(set(approved_sessions)) != len(
            approved_sessions
        ) or any(not isinstance(x, str) or SESSION_ID.fullmatch(x) is None
                 for x in approved_sessions) or type(allow_model_preview) is not bool:
            raise ScopeDeniedError("browser sessions require a bounded host allowlist")
        self.actor, self.apps, self.policy = actor, apps, policy
        self.approved_sessions = frozenset(approved_sessions)
        self.allow_model_preview = allow_model_preview
        self.recorder = BrowserEvidenceRecorder(artifacts, actor)
        if gallery is not None and not isinstance(gallery, BrowserEvidenceGallery):
            raise ScopeDeniedError("browser gallery is not an operator index")
        if (not isinstance(reservations, tuple) or len(reservations) > 12 or
                any(not isinstance(v, BrowserLease) or v.workspace_id != actor.workspace_id
                        or v.owner_kind != "agent" or v.session_id not in self.approved_sessions
                        for v in reservations) or len({v.session_id for v in reservations}) != len(reservations)):
            raise ScopeDeniedError("browser reservations must be host-issued for this workspace")
        if reservations and (apps.browser_leases is None):
            raise ScopeDeniedError("agent browser reservations require the real Xpra coordinator")
        self.gallery = gallery
        self.reservations = {v.session_id: v for v in reservations}
        self.artifacts = artifacts
        self._lock = threading.RLock()
        self._seen: set[UUID] = set()

    def _authorize(self, actor: WorkspaceContext, session: str, invocation_id: UUID) -> None:
        if actor != self.actor or not isinstance(invocation_id, UUID) or (
            session not in self.approved_sessions
        ):
            raise ScopeDeniedError("browser session is not approved for this controller scope")
        with self._lock:
            if len(self._seen) >= 4096 or invocation_id in self._seen:
                raise StateMismatchError("browser invocation is duplicate or capacity reached")
            self._seen.add(invocation_id)

    def fence(self, session: str) -> None:
        """Operator-only; terminate an uncertain browser, never silently replay."""
        if session not in self.approved_sessions:
            raise ScopeDeniedError("cannot fence a foreign browser")
        self.apps.fence_browser(session)

    @staticmethod
    def _preview(group: str, raw: dict[str, Any]) -> dict[str, Any] | None:
        """Bounded opt-in *model-visible* projection; no raw forms/JPEG/base64."""
        if group == "capture":
            return None
        if group == "observe":
            permitted = ("aria", "url", "title", "picker", "selected", "elements", "engine", "stealth")
        elif group == "pick":
            permitted = ("picker",)
        else:
            permitted = ("url", "title", "acted", "role", "name", "sent")
        result = {key: raw[key] for key in permitted if key in raw}
        # Explicit size bound rather than truncating arbitrary structured JSON.
        if len(json.dumps(result, ensure_ascii=True).encode("utf-8")) > 8192:
            if "aria" in result and isinstance(result["aria"], str):
                result["aria"] = result["aria"][:3000]
            for name in ("selected", "elements"):
                result.pop(name, None)
        if len(json.dumps(result, ensure_ascii=True).encode("utf-8")) > 8192:
            return None
        return result or None

    def dispatch(self, group: str, request: object, invocation_id: UUID,
                 actor: WorkspaceContext) -> dict[str, Any]:
        """Execute once, write immutable artifacts, then return digest/UUID refs.

        A failed *side-effecting* call is indeterminate, even if the RPC itself
        succeeded but writing its evidence failed. The operator fences the
        session so nobody can retry a possibly submitted form blindly.
        """
        session, cmd = validate_request(group, request)
        self._authorize(actor, session, invocation_id)
        kind = {"observe": "observation", "act": "action", "pick": "action",
                "capture": "capture"}[group]
        try:
            reserved = self.reservations.get(session)
            if reserved is not None:
                raw = self.apps.browser_command(session, cmd, lease_token=reserved.token,
                                            actor_agent_id=reserved.owner_id)
            else:
                raw = self.apps.browser_command(session, cmd)
            if not isinstance(raw, dict):
                raise StateMismatchError("browser returned an invalid observation")
            receipt = self.recorder.record(
                scope=BrowserEvidenceScope(
                    workspace_id=actor.workspace_id, principal_id=actor.principal_id,
                    session_id=session, invocation_id=invocation_id,
                    policy_sha256=self.policy.fingerprint,
                ),
                command=cmd, result=raw, kind=kind, require_image=group == "capture",
            )
            if self.gallery is not None:
                self.gallery.append(actor, BrowserEvidenceScope(
                    workspace_id=actor.workspace_id, principal_id=actor.principal_id,
                    session_id=session, invocation_id=invocation_id,
                    policy_sha256=self.policy.fingerprint,
                ), receipt)
        except BaseException as error:
            if group in {"act", "pick"}:
                try:
                    self.fence(session)
                except Exception:
                    pass  # Preserve the original failure: no retry or success claim.
                raise IndeterminateExecutionError(
                    "browser action or evidence outcome is uncertain; reconcile fenced session"
                ) from error
            raise
        receipt["preview"] = self._preview(group, raw) if self.allow_model_preview else None
        return receipt
