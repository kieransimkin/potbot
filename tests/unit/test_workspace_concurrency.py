"""Deterministic controller-side shared-workspace admission and image pin tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.tools.github_concurrency import GitHubChangeIntent
from potbot.tools.workspace_concurrency import (
    AccessRequest,
    AccessResource,
    WorkspaceLeaseLedger,
    conflict,
)

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
RECEIPT = "f" * 64
COMMIT = "c" * 40


class SharedWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.private = self.root / "controller"
        self.private.mkdir()
        self.home = self.root / "workspace" / "home"
        self.home.mkdir(parents=True)
        self.image = self.root / "images" / "image-a"
        self.image.mkdir(parents=True)
        self.db_file = self.private / "admission.sqlite"
        self.ledger = WorkspaceLeaseLedger(self.db_file, workspace_home=self.home)
        self.workspace = uuid4()
        self.agent = uuid4()
        self.ledger.pin_image(
            self.workspace, digest=DIGEST_A,
            image_ref="ghcr.io/example/potbot-sandbox@sha256:" + DIGEST_A,
            build_commit=COMMIT, platform="linux/amd64", rootfs_path=self.image,
            qualification_sha256=RECEIPT,
        )

    def request(self, key: str, *resources: AccessResource,
                effect: str = "write", image: str = DIGEST_A) -> AccessRequest:
        return AccessRequest(
            self.workspace, self.agent, "git.operation", key, effect, resources, image
        )

    def test_lexical_home_and_github_scopes_reject_escapes(self) -> None:
        for value in ("/etc/passwd", "/home/../etc", "/home//repo", "/home/./repo", "/home2"):
            with self.subTest(value=value), self.assertRaises((InvalidInputError, ScopeDeniedError)):
                AccessResource("home", value, "write")
        for value in ("a/b#refs/heads/../main", "a/b#refs/heads/", "a/b#refs/tags/v1"):
            with self.subTest(value=value), self.assertRaises(InvalidInputError):
                AccessResource("github", value, "write")

    def test_directory_ancestors_and_read_parallelism(self) -> None:
        parent = AccessResource("home", "/home/projects", "write")
        child = AccessResource("home", "/home/projects/app", "read")
        sibling = AccessResource("home", "/home/other", "write")
        self.assertTrue(conflict(parent, child))
        self.assertFalse(conflict(parent, sibling))
        self.assertFalse(conflict(child, AccessResource("home", "/home/projects/app", "read")))

    def test_sqlite_serializes_two_independent_controller_connections(self) -> None:
        first = self.ledger.claim(self.request(
            "first", AccessResource("home", "/home/repo", "write")
        ))
        other = WorkspaceLeaseLedger(self.db_file, workspace_home=self.home)
        with self.assertRaises(StateMismatchError):
            other.claim(self.request(
                "conflict", AccessResource("home", "/home/repo/file", "read"),
                effect="read"
            ))
        separate = other.claim(self.request(
            "different", AccessResource("home", "/home/notes", "write")
        ))
        self.assertNotEqual(first.generation, separate.generation)
        self.ledger.finish(first, result_sha256=RECEIPT)
        self.ledger.finish(separate, result_sha256=RECEIPT)
        self.assertEqual(self.ledger.active(self.workspace), [])

    def test_idempotency_and_stale_token(self) -> None:
        req = self.request("idemp", AccessResource("home", "/home/src", "write"))
        first = self.ledger.claim(req)
        self.assertEqual(self.ledger.claim(req), first)
        with self.assertRaises(StateMismatchError):
            self.ledger.claim(self.request("idemp", AccessResource("home", "/home/other", "write")))
        with self.assertRaises(StateMismatchError):
            self.ledger.finish(first.__class__(first.lease_id, first.workspace_id, first.generation,
                                              first.expires_ns, first.state, "invalid"),
                               result_sha256=RECEIPT)
        self.ledger.finish(first, result_sha256=RECEIPT)
        self.assertEqual(self.ledger.claim(req).state, "completed")

    def test_uncertain_and_expired_effects_block_until_reconciled(self) -> None:
        with patch("potbot.tools.workspace_concurrency.time.time_ns", return_value=10**12):
            lease = self.ledger.claim(self.request(
                "uncertain", AccessResource("home", "/home/src", "write")
            ), ttl_seconds=1)
        with patch("potbot.tools.workspace_concurrency.time.time_ns", return_value=10**12 + 10**9):
            with self.assertRaises(StateMismatchError):
                self.ledger.claim(self.request(
                    "second", AccessResource("home", "/home/src", "write")
                ))
            with self.assertRaises(StateMismatchError):
                self.ledger.finish(lease, result_sha256=RECEIPT)
            self.assertEqual(self.ledger.active(self.workspace)[0]["state"], "indeterminate")
            self.ledger.reconcile(lease, evidence_sha256=RECEIPT)
            self.assertEqual(self.ledger.active(self.workspace), [])
            another = self.ledger.claim(self.request(
                "after-reconcile", AccessResource("home", "/home/src", "write")
            ))
            self.assertEqual(another.state, "active")

    def test_pinned_image_revisions_block_running_tools(self) -> None:
        lease = self.ledger.claim(self.request("img", AccessResource("home", "/home", "write")))
        with self.assertRaises(StateMismatchError):
            self.ledger.pin_image(
                self.workspace, digest=DIGEST_B,
                image_ref="ghcr.io/example/sandbox@sha256:" + DIGEST_B,
                build_commit=COMMIT, platform="linux/amd64", rootfs_path=self.image,
                qualification_sha256=RECEIPT, expected_revision=1,
            )
        self.ledger.finish(lease, result_sha256=RECEIPT)
        revision = self.ledger.pin_image(
            self.workspace, digest=DIGEST_B,
            image_ref="ghcr.io/example/sandbox@sha256:" + DIGEST_B,
            build_commit=COMMIT, platform="linux/amd64", rootfs_path=self.image,
            qualification_sha256=RECEIPT, expected_revision=1,
        )
        self.assertEqual(revision, 2)
        self.assertEqual(self.ledger.image_status(self.workspace)["digest"], DIGEST_B)
        with self.assertRaises(StateMismatchError):
            self.ledger.claim(self.request("stale", AccessResource("home", "/home", "write")))
        with self.assertRaises(StateMismatchError):
            self.ledger.pin_image(
                self.workspace, digest=DIGEST_A,
                image_ref="ghcr.io/example/sandbox@sha256:" + DIGEST_A,
                build_commit=COMMIT, platform="linux/amd64", rootfs_path=self.image,
                qualification_sha256=RECEIPT, expected_revision=1,
            )

    def test_unknown_effect_requires_workspace_wide_exclusive(self) -> None:
        with self.assertRaises(InvalidInputError):
            self.request("bad", AccessResource("home", "/home/src", "write"), effect="unknown")
        first = self.ledger.claim(self.request(
            "broad", AccessResource("workspace", "*", "write"), effect="unknown"
        ))
        with self.assertRaises(StateMismatchError):
            self.ledger.claim(self.request(
                "blocked", AccessResource("github", "owner/repo#refs/heads/main", "read"),
                effect="read"
            ))
        self.ledger.finish(first, result_sha256=RECEIPT)

    def test_github_branch_scopes_and_cas(self) -> None:
        intent = GitHubChangeIntent(self.workspace, self.agent, "owner/repo", "main", COMMIT, RECEIPT)
        self.assertEqual(intent.branch_resource.name, "owner/repo#refs/heads/main")
        self.assertTrue(intent.agent_branch.startswith("potbot/"))
        intent.verify_head(COMMIT)
        with self.assertRaises(StateMismatchError):
            intent.verify_head("d" * 40)
        self.assertTrue(intent.instructions()["requires_atomic_ref_update"])
        self.assertFalse(intent.instructions()["publish_directly_to_protected_branch"])
        main = self.ledger.claim(self.request("main", intent.branch_resource))
        feature = self.ledger.claim(self.request(
            "feature", AccessResource("github", "owner/repo#refs/heads/feature", "write")
        ))
        with self.assertRaises(StateMismatchError):
            self.ledger.claim(self.request(
                "global", AccessResource("github", "owner/repo#*", "write")
            ))
        self.ledger.finish(main, result_sha256=RECEIPT)
        self.ledger.finish(feature, result_sha256=RECEIPT)

    def test_lease_ledger_cannot_reside_in_guest_home(self) -> None:
        with self.assertRaises(ScopeDeniedError):
            WorkspaceLeaseLedger(self.home / "claims.sqlite", workspace_home=self.home)


if __name__ == "__main__":
    unittest.main()

class ConcurrentGitHubAcceptance(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "controller").mkdir()
        (self.root / "images").mkdir()
        (self.root / "workspaces" / "a" / "home").mkdir(parents=True)
        (self.root / "workspaces" / "b" / "home").mkdir(parents=True)
        self.ws_a, self.ws_b = uuid4(), uuid4()
        self.agent_a, self.agent_b = uuid4(), uuid4()
        self.a = WorkspaceLeaseLedger(
            self.root / "controller" / "leases.sqlite",
            workspace_home=self.root / "workspaces" / "a" / "home",
        )
        self.b = WorkspaceLeaseLedger(
            self.root / "controller" / "leases.sqlite",
            workspace_home=self.root / "workspaces" / "b" / "home",
        )
        for ledger, ws in ((self.a, self.ws_a), (self.b, self.ws_b)):
            ledger.pin_image(
                ws, digest=DIGEST_A,
                image_ref="ghcr.io/example/potbot@sha256:" + DIGEST_A,
                build_commit=COMMIT, platform="linux/amd64",
                rootfs_path=self.root / "images", qualification_sha256=RECEIPT,
            )

    def test_github_global_mutations_block_across_workspaces_not_home(self) -> None:
        repo = AccessResource("github", "owner/repo#refs/heads/feature", "write")
        first = self.a.claim(AccessRequest(
            self.ws_a, self.agent_a, "github.update_ref", "one", "write", (repo,), DIGEST_A
        ))
        with self.assertRaises(StateMismatchError):
            self.b.claim(AccessRequest(
                self.ws_b, self.agent_b, "github.update_ref", "two", "write", (repo,), DIGEST_A
            ))
        # /home is isolated per workspace even if guest paths are identical.
        home = AccessResource("home", "/home/src", "write")
        la = self.a.claim(AccessRequest(
            self.ws_a, self.agent_a, "file.write", "homea", "write", (home,), DIGEST_A
        ))
        lb = self.b.claim(AccessRequest(
            self.ws_b, self.agent_b, "file.write", "homeb", "write", (home,), DIGEST_A
        ))
        self.a.finish(first, result_sha256=RECEIPT)
        self.a.finish(la, result_sha256=RECEIPT)
        self.b.finish(lb, result_sha256=RECEIPT)
        second = self.b.claim(AccessRequest(
            self.ws_b, self.agent_b, "github.update_ref", "two", "write", (repo,), DIGEST_A
        ))
        self.b.finish(second, result_sha256=RECEIPT)

    def test_symlink_alias_rejected(self) -> None:
        home = self.root / "workspaces" / "a" / "home"
        (home / "alias").symlink_to(self.root / "controller", target_is_directory=True)
        with self.assertRaises(ScopeDeniedError):
            self.a.claim(AccessRequest(
                self.ws_a, self.agent_a, "file.read", "alias", "read",
                (AccessResource("home", "/home/alias/private", "read"),), DIGEST_A
            ))

    def test_host_scoped_github_writer_cas_readback_and_no_direct_main(self) -> None:
        from potbot.tools.github_concurrency import (
            ConcurrentGitHubTool,
            graphql_update_refs_variables,
        )

        class Remote:
            def __init__(self) -> None:
                self.value = COMMIT
                self.calls = 0
                self.fail_post_dispatch = False

            def head(self, repository: str, branch: str) -> str:
                self.calls += 1
                return self.value

            def compare_and_swap(self, repository: str, branch: str, *,
                                 before_oid: str, after_oid: str) -> str:
                if self.value != before_oid:
                    raise StateMismatchError("real GraphQL CAS failed")
                self.value = after_oid
                if self.fail_post_dispatch:
                    raise TimeoutError("outcome unknown")
                return after_oid

        remote = Remote()
        writer = ConcurrentGitHubTool(ledger=self.a, client=remote)
        base = GitHubChangeIntent(self.ws_a, self.agent_a, "owner/repo", "main", COMMIT, RECEIPT)
        with self.assertRaises(InvalidInputError):
            writer.update_agent_branch(
                base, new_commit="d" * 40, image_digest=DIGEST_A, idempotency_key="main"
            )
        intent = GitHubChangeIntent(
            self.ws_a, self.agent_a, "owner/repo", base.agent_branch, COMMIT, RECEIPT
        )
        vars = graphql_update_refs_variables(
            repository_id="R_abc", branch="refs/heads/" + intent.agent_branch,
            before_oid=COMMIT, after_oid="d" * 40,
        )
        self.assertFalse(vars["refUpdates"][0]["force"])
        self.assertEqual(vars["refUpdates"][0]["beforeOid"], COMMIT)
        receipt = writer.update_agent_branch(
            intent, new_commit="d" * 40, image_digest=DIGEST_A, idempotency_key="first"
        )
        self.assertEqual(receipt.after_oid, "d" * 40)
        with self.assertRaises(StateMismatchError):
            writer.update_agent_branch(
                intent, new_commit="d" * 40, image_digest=DIGEST_A, idempotency_key="first"
            )
        with self.assertRaises(StateMismatchError):
            writer.update_agent_branch(
                intent, new_commit="e" * 40, image_digest=DIGEST_A, idempotency_key="stale"
            )
        # Pre-dispatch head mismatch does not falsely claim a remote mutation.
        self.assertFalse(self.a.active(self.ws_a))
        attempt2 = GitHubChangeIntent(
            self.ws_a, self.agent_a, "owner/repo", intent.agent_branch, "d" * 40, RECEIPT
        )
        remote.fail_post_dispatch = True
        with self.assertRaises(TimeoutError):
            writer.update_agent_branch(
                attempt2, new_commit="e" * 40, image_digest=DIGEST_A,
                idempotency_key="uncertain",
            )
        self.assertEqual(self.a.active(self.ws_a)[0]["state"], "indeterminate")
        with self.assertRaises(StateMismatchError):
            writer.update_agent_branch(
                attempt2, new_commit="f" * 40, image_digest=DIGEST_A,
                idempotency_key="retry-after-uncertain",
            )

class GraphQLTransportContracts(unittest.TestCase):
    def test_graphql_client_uses_exact_before_oid_and_scopes_repository(self) -> None:
        from potbot.tools.github_concurrency import GitHubGraphQLClient

        client = GitHubGraphQLClient(
            token_supplier=lambda: "token-not-used-in-mock-123456789",
            allowed_repositories=frozenset({"owner/repo"}),
        )
        received: list[tuple[str, dict[str, object]]] = []

        def mocked(repository: str, query: str, variables: dict[str, object]) -> dict:
            received.append((query, variables))
            if "mutation" in query:
                update = variables["input"]["refUpdates"][0]  # type: ignore[index]
                self.assertEqual(update["beforeOid"], "a" * 40)
                self.assertEqual(update["afterOid"], "b" * 40)
                self.assertIs(update["force"], False)
                return {"updateRefs": {"clientMutationId": None}}
            return {"repository": {"id": "R_123", "ref": {"target": {
                "oid": "b" * 40 if any("mutation" in q for q, _ in received) else "a" * 40,
            }}}}

        client._query = mocked  # type: ignore[method-assign]
        result = client.compare_and_swap(
            "owner/repo", "potbot/w/a", before_oid="a" * 40, after_oid="b" * 40
        )
        self.assertEqual(result, "b" * 40)
        self.assertEqual(len(received), 3)
        forbidden = GitHubGraphQLClient(
            token_supplier=lambda: "token-not-used-in-mock-123456789",
            allowed_repositories=frozenset({"different/repo"}),
        )
        with self.assertRaises(InvalidInputError):
            forbidden._query("owner/repo", "query{}", {})
