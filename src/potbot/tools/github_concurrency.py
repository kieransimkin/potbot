"""Concurrency-safe GitHub intent contracts; host connector owns real network I/O.

GitHub mutation is NOT a shell-side `git push`. Each agent works on its own
branch/worktree and the trusted broker performs a protected ref comparison.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Protocol
from uuid import UUID

from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.tools.workspace_concurrency import (
    AccessRequest,
    AccessResource,
    Mode,
    WorkspaceLeaseLedger,
)

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")


@dataclass(frozen=True, slots=True)
class GitHubChangeIntent:
    workspace_id: UUID
    agent_id: UUID
    repository: str
    target_branch: str
    expected_head: str
    patch_sha256: str

    def __post_init__(self) -> None:
        parts = self.repository.split("/") if isinstance(self.repository, str) else []
        if len(parts) != 2 or not all(_NAME.fullmatch(part) for part in parts):
            raise InvalidInputError("GitHub changes require an exact owner/repository")
        if not isinstance(self.workspace_id, UUID) or not isinstance(self.agent_id, UUID):
            raise InvalidInputError("GitHub changes require workspace and agent identities")
        if (
            not isinstance(self.target_branch, str)
            or not self.target_branch
            or not re.fullmatch(r"[A-Za-z0-9_./-]{1,150}", self.target_branch)
            or self.target_branch.startswith("/")
            or ".." in self.target_branch
            or "//" in self.target_branch
            or self.target_branch.endswith(".lock")
        ):
            raise InvalidInputError("GitHub branch is not canonical")
        if not isinstance(self.expected_head, str) or not _SHA.fullmatch(self.expected_head):
            raise InvalidInputError("GitHub branch mutations require expected 40-hex SHA")
        if not isinstance(self.patch_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.patch_sha256
        ):
            raise InvalidInputError("GitHub mutation needs a retained patch SHA-256")

    @property
    def branch_resource(self) -> AccessResource:
        return AccessResource(
            "github", f"{self.repository}#refs/heads/{self.target_branch}", "write"
        )

    @property
    def agent_branch(self) -> str:
        return f"potbot/{self.workspace_id.hex[:12]}/{self.agent_id.hex[:12]}"

    def verify_head(self, actual_head: str) -> None:
        """Pure CAS guard; re-read on server during the atomic ref update."""
        if actual_head != self.expected_head:
            raise StateMismatchError("GitHub branch moved; rebase and revalidate before mutation")

    def instructions(self) -> dict[str, object]:
        """Non-secret plan for a trusted GitHub connector; never claims publication."""
        return {
            "repository": self.repository,
            "workspace_id": str(self.workspace_id),
            "agent_id": str(self.agent_id),
            "scratch_branch": self.agent_branch,
            "target_branch": self.target_branch,
            "expected_head": self.expected_head,
            "patch_sha256": self.patch_sha256,
            "requires_atomic_ref_update": True,
            "requires_verified_patch": True,
            "publish_directly_to_protected_branch": False,
        }



class GitHubRefClient(Protocol):
    """Trusted server-side transport; update MUST use GraphQL updateRefs.beforeOid.

    GitHub REST git/refs PATCH with force=false is *not* exact CAS. Transport
    must never retry a request after a timeout without reconciliation.
    """

    def head(self, repository: str, branch: str) -> str: ...

    def compare_and_swap(self, repository: str, branch: str, *,
                         before_oid: str, after_oid: str) -> str: ...


def graphql_update_refs_variables(
    *, repository_id: str, branch: str, before_oid: str, after_oid: str
) -> dict[str, object]:
    """GraphQL updateRefs input: exact expected old SHA, no force-update."""
    if not repository_id or len(repository_id) > 256 or not isinstance(repository_id, str):
        raise InvalidInputError("GitHub node ID is required")
    if not isinstance(branch, str) or not branch.startswith("refs/heads/"):
        raise InvalidInputError("only GitHub branch refs can be updated")
    AccessResource("github", f"owner/repo#{branch}", "write")
    if not all(isinstance(x, str) and _SHA.fullmatch(x) for x in (before_oid, after_oid)):
        raise InvalidInputError("GitHub CAS requires immutable commit OIDs")
    return {
        "repositoryId": repository_id,
        "refUpdates": [{"name": branch, "beforeOid": before_oid,
                        "afterOid": after_oid, "force": False}],
    }


@dataclass(frozen=True, slots=True)
class GitHubMutationReceipt:
    repository: str
    branch: str
    before_oid: str
    after_oid: str
    patch_sha256: str
    workspace_id: str
    agent_id: str
    lease_generation: int


class ConcurrentGitHubTool:
    """Controller-only GitHub ref writer: scope, fenced lease, exact CAS and readback.

    The commit OID must already be produced/verified by the controller. The
    transport holds credentials outside /home. This is intentionally not an
    untrusted agent shell command and does not create or merge PRs.
    """

    def __init__(self, *, ledger: WorkspaceLeaseLedger, client: GitHubRefClient) -> None:
        self.ledger = ledger
        self.client = client

    def update_agent_branch(
        self, intent: GitHubChangeIntent, *, new_commit: str, image_digest: str,
        idempotency_key: str,
    ) -> GitHubMutationReceipt:
        if intent.target_branch != intent.agent_branch:
            raise InvalidInputError("only the scoped agent branch may be changed by this tool")
        if not isinstance(new_commit, str) or not _SHA.fullmatch(new_commit):
            raise InvalidInputError("publish requires a verified new commit SHA")
        request = AccessRequest(
            workspace_id=intent.workspace_id, actor_id=intent.agent_id,
            operation="github.update_ref", idempotency_key=idempotency_key,
            effect="write", resources=(intent.branch_resource,), image_digest=image_digest,
        )
        lease = self.ledger.claim(request)
        if lease.state != "active":
            # No implicit repeat after completed/uncertain dispatch.
            raise StateMismatchError("GitHub mutation already attempted; inspect its receipt")
        from hashlib import sha256
        from json import dumps

        dispatched = False
        try:
            intent.verify_head(self.client.head(intent.repository, intent.target_branch))
            dispatched = True
            returned_oid = self.client.compare_and_swap(
                intent.repository, intent.target_branch,
                before_oid=intent.expected_head, after_oid=new_commit,
            )
            if returned_oid != new_commit:
                raise StateMismatchError("GitHub mutation response cannot be verified")
            if self.client.head(intent.repository, intent.target_branch) != new_commit:
                raise StateMismatchError("GitHub mutation readback cannot be verified")
            receipt = GitHubMutationReceipt(
                repository=intent.repository, branch=intent.target_branch,
                before_oid=intent.expected_head, after_oid=new_commit,
                patch_sha256=intent.patch_sha256,
                workspace_id=str(intent.workspace_id), agent_id=str(intent.agent_id),
                lease_generation=lease.generation,
            )
            digest = sha256(dumps(asdict(receipt), sort_keys=True).encode()).hexdigest()
            self.ledger.finish(lease, result_sha256=digest)
            return receipt
        except BaseException:
            # Before dispatch, no remote write occurred. A dispatched timeout
            # is uncertain and MUST be reconciled before any new attempt.
            try:
                self.ledger.finish(
                    lease, result_sha256=sha256(
                        b"github-unresolved-v1" if dispatched else b"github-preflight-rejected-v1"
                    ).hexdigest(),
                    uncertain=dispatched,
                )
            except StateMismatchError:
                pass  # expired lease is already indeterminate
            raise


class GitHubGraphQLClient:
    """Bounded, host-only GitHub GraphQL transport for atomic updateRefs CAS.

    The caller supplies a vault-backed token provider and a strict repository
    allowlist. This client is not exposed to sandbox shells or model prompts.
    HTTP/GraphQL error bodies (which may contain private data) are discarded.
    """

    def __init__(self, *, token_supplier: Callable[[], str],
                 allowed_repositories: frozenset[str]) -> None:
        if not callable(token_supplier) or not allowed_repositories or any(
            not isinstance(repo, str)
            or len(repo.split("/")) != 2
            or not all(_NAME.fullmatch(x) for x in repo.split("/"))
            for repo in allowed_repositories
        ):
            raise InvalidInputError("GitHub client requires vault and allowed repositories")
        self._token_supplier = token_supplier
        self._allowlist = frozenset(repo.casefold() for repo in allowed_repositories)

    def _query(self, repository: str, query: str, variables: dict[str, object]) -> dict:
        import http.client
        import json
        import ssl

        if repository.casefold() not in self._allowlist:
            raise InvalidInputError("GitHub repository is not authorized")
        token = self._token_supplier()
        if (not isinstance(token, str) or not (20 <= len(token) <= 4096)
                or any(ord(char) < 33 or ord(char) > 126 for char in token)):
            raise InvalidInputError("GitHub credential is unavailable")
        body = json.dumps({"query": query, "variables": variables}, separators=(",", ":"))
        if len(body) > 100000:
            raise InvalidInputError("GitHub query exceeds size bound")
        conn = http.client.HTTPSConnection("api.github.com", timeout=15, context=ssl.create_default_context())
        try:
            conn.request("POST", "/graphql", body=body.encode(), headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "potbot-workspace-broker",
            })
            response = conn.getresponse()
            raw = response.read(1_048_577)
            if response.status != 200 or len(raw) > 1_048_576:
                raise StateMismatchError("GitHub GraphQL request was not accepted")
            try:
                payload = json.loads(raw)
            except (UnicodeDecodeError, ValueError) as exc:
                raise StateMismatchError("GitHub GraphQL response is invalid") from exc
            if not isinstance(payload, dict) or payload.get("errors") or not isinstance(
                payload.get("data"), dict
            ):
                raise StateMismatchError("GitHub rejected the authorized operation")
            return payload["data"]
        finally:
            conn.close()

    def _ref(self, repository: str, branch: str) -> tuple[str, str]:
        owner, name = repository.split("/")
        result = self._query(repository, """query($owner:String!,$name:String!,$ref:String!){
            repository(owner:$owner,name:$name){
              id ref(qualifiedName:$ref){target{oid}}
            }
          }""", {"owner": owner, "name": name, "ref": "refs/heads/" + branch})
        entry = result.get("repository")
        ref = entry.get("ref") if isinstance(entry, dict) else None
        oid = ref.get("target", {}).get("oid") if isinstance(ref, dict) else None
        node_id = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(oid, str) or not _SHA.fullmatch(oid) or not isinstance(node_id, str):
            raise StateMismatchError("GitHub branch or repository cannot be resolved")
        return node_id, oid

    def head(self, repository: str, branch: str) -> str:
        return self._ref(repository, branch)[1]

    def compare_and_swap(self, repository: str, branch: str, *,
                         before_oid: str, after_oid: str) -> str:
        node_id, observed = self._ref(repository, branch)
        if observed != before_oid:
            raise StateMismatchError("GitHub HEAD moved before CAS")
        variables = graphql_update_refs_variables(
            repository_id=node_id, branch="refs/heads/" + branch,
            before_oid=before_oid, after_oid=after_oid,
        )
        result = self._query(repository, """mutation($input:UpdateRefsInput!){
            updateRefs(input:$input){clientMutationId}
          }""", {"input": variables})
        if not isinstance(result.get("updateRefs"), dict):
            raise StateMismatchError("GitHub did not acknowledge the ref transaction")
        actual = self.head(repository, branch)
        if actual != after_oid:
            raise StateMismatchError("GitHub ref transaction readback differs")
        return actual


@dataclass(frozen=True, slots=True)
class GitHubFileVersion:
    """An exact blob precondition; missing files use `None` explicitly."""

    path: str
    expected_blob: str | None

    def __post_init__(self) -> None:
        # Reuse the broker's canonical repository-path validation.
        AccessResource("github_file", "example/repo#refs/heads/staging:" + self.path, "read")
        if self.expected_blob is not None and (
            not isinstance(self.expected_blob, str) or not _SHA.fullmatch(self.expected_blob)
        ):
            raise InvalidInputError("GitHub file must bind expected 40-character blob OID")


@dataclass(frozen=True, slots=True)
class GitHubFileChangePlan:
    """Source-pinned file-level staged mutation, NOT permission to push to main.

    Agents may inspect/change nonoverlapping files concurrently *in their own
    branches*. The final remote target ref update always uses `beforeOid`
    under an exclusive branch lease. A moved ref requires rebase and rechecking
    every listed base blob; never auto-merge an unverified concurrent edit.
    """

    intent: GitHubChangeIntent
    files: tuple[GitHubFileVersion, ...]

    def __post_init__(self) -> None:
        if not 1 <= len(self.files) <= 30 or len({f.path for f in self.files}) != len(self.files):
            raise InvalidInputError("GitHub patch must list 1-30 unique paths")
        if self.intent.target_branch != self.intent.agent_branch:
            raise InvalidInputError("file changes must stage on agent-owned branches")

    def resources(self, *, read_only: bool = False) -> tuple[AccessResource, ...]:
        mode: Mode = "read" if read_only else "write"
        return tuple(AccessResource(
            "github_file",
            f"{self.intent.repository}#refs/heads/{self.intent.target_branch}:{item.path}",
            mode,
        ) for item in self.files)

    def access_request(self, *, image_digest: str, idempotency_key: str) -> AccessRequest:
        return AccessRequest(
            self.intent.workspace_id, self.intent.agent_id,
            "github.stage_files", idempotency_key, "write",
            self.resources(), image_digest,
        )
