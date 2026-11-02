# SYN release dependency and independent node deployment

**Status:** Mandatory architecture decision, updated 2 November 2026. SYN-R02 package installation is implemented in the [operator installer](syn-release-installation.md); actual upstream acceptance and remaining node qualification must be recorded separately.
**Scope:** U02/U03/U04/U06/U16 and Syn phases 35, 39–48. This document overrides conflicting implementation suggestions without rewriting historical phase records.

## Non-negotiable ownership

SYN is independently developed and released by its canonical upstream repository. Potbot is an orchestrator and consumer, not a SYN runtime distributor or maintainer. Potbot MUST NOT copy SYN source, publish a fork, build SYN from arbitrary Git revisions, embed a second node runtime, or reimplement production identity, P2P, Ember, Cog or key-custody functionality. Potbot MAY retain narrow protocol clients, adapters, manifest verification, supervised process launch, lifecycle metadata and test doubles that are clearly isolated from production. Avoid direct imports of SYN internal modules; use upstream documented stable APIs/CLI/wire contracts.

`potbot/syn-node/` is legacy transitional implementation. Inventory its protocol, custody, transport, tests and release behavior against canonical upstream; propose any missing capabilities upstream. Keep the transitional code only as an explicitly nonproduction test fixture until the upstream package passes acceptance, then remove it and update tests, workflows and packaging. Do not delete it before parity and migration qualification. No runtime fallback to the embedded node is permitted.

## Release acquisition and trust

1. Record the canonical SYN repository URL, publisher identity, supported platforms/architectures, release manifest schema, trust-root provisioning and version compatibility matrix. SYN-R01 now defines the official five payloads, detached signature and canonical manifest. The first upstream release supports only service mode; operators still supply independently trusted pins and current revocations.
2. Discover releases from the configured canonical upstream release channel. Require immutable version, platform artifact, SHA-256 digest and authenticated publisher signature or equivalent verified release provenance rooted in an operator-approved trust root. HTTPS or checksum alone is not publisher authentication. Reject untrusted redirects, mismatched metadata, revoked keys, missing artifacts and mutable tags. Verify before unpacking or executing.
3. Stage downloads outside the sandbox, with bounded size, retries, concurrency-safe locking and content-addressed storage. Pin the exact digest in an operator-controlled installation manifest and audit journal. No network retrieval from model-controlled inputs or agent-writable locations.
4. Extract safely (no traversal, symlink escape or unsafe permissions), install atomically into an operator-owned immutable directory, then mount approved runtime files read-only into sandboxes. Never expose updater authority to an agent. Package files may be shared; identities, keystores, configuration, logs and process lifecycles MUST be separate.
5. Maintain a tested compatibility policy for Syn API/protocol and schema versions, plus offline pinned-artifact install, staged upgrade, downgrade constraints, rollback, revocation and supply-chain incident response. Prevent rollback of identity/enrollment state.

## Required running topology

- **Operator:** a genuine upstream SYN release running independently in operator mode outside untrusted agent sandboxes, with protected host keystore, pinned installation identity and supervised health/readiness. Potbot attaches to a verified existing operator or performs a separately authorized first installation. Missing/corrupt/ambiguous registrations fail closed; no silent identity regeneration.
- **Agent:** each provisioned agent has its own genuine upstream SYN agent-mode installation instance running inside its qualified workspace sandbox under its own restricted OS identity, protected agent keystore and persistent identity. Start and verify the SYN node before admitting its Cogs. Preserve identity across process restart; restart does not mint new keys. Do not equate a per-invocation `SynWorkerNodeMesh` actor with a real node.
- **Workers:** worker invocations remain individually accounted for by Potbot's operator, with existing cgroup, filesystem, syscall, resource and provenance controls. Worker heartbeats are operational signals, not identity or custody proofs. Agent SYN service lifetime is distinct from individual Cog lifetime; any per-worker node requirement must be fulfilled by a documented upstream-supported mechanism, not a fabricated local daemon.
- **Communication:** use upstream authenticated local IPC and supported encrypted P2P. Preserve workspace isolation and operator-mediated capability/side-effect authorization; discovery never grants authority. Keep the default read-only, with explicit operator unlock for write/execute effects. No agent-to-agent network bypass of sandbox policy.

## Identity and adoption invariants

SYN alone owns entropy, key derivation, private-key custody, enrollment and signing. Preserve operator root `m/6969'/420'`, base `m/6969'/420'/0'/0/0`, installation identifier SHA-256 of raw 32-byte base public key, and hardware-bound inheritance `uint24_be(SHA256(raw signed hardware ID || operator byte)[:3])` with eight candidate paths `m/6969'/420'/inheritance'/{1..8}/0`. Temporary agent keys come from OS CSPRNG; their public hash authenticates signed offers and MUST NOT replace the hardware identifier in inheritance. Verify actual agent-held selected-key possession and qualified OS custody, not merely operator-signed assertions. Potbot stores public receipts and authorization decisions, never private or extended keys. Existing SQLite-based transitional ledgers require a separate authoritative-storage reconciliation with Potbot's PostgreSQL policy before production.

## Delivery milestones and acceptance gates

| Gate | Deliverable | Proof required |
| --- | --- | --- |
| SYN-R01 | Upstream inventory | Canonical repository/release publisher confirmed, upstream issues for missing capabilities, compatibility matrix and signed trust policy |
| SYN-R02 | [Operator release installer](syn-release-installation.md) implemented | Exact signature/payload verification, private cache, atomic admission, concurrent/offline reuse and tamper/revocation/downgrade checks. Actual official-package acceptance is separate from synthetic public fixtures; no node admission follows from installing packages. |
| SYN-R03 | [Native operator deployment candidate](syn-operator-deployment.md) | Managed Ubuntu WSL2 installation on Windows; native Linux/macOS; explicit upstream bootstrap, protected custody/allocation, authenticated attach, owned-child supervision and fail-closed health. Official-release and actual persistent-host acceptance remain separately recorded gates. |
| SYN-R04 | [Agent provisioning candidate](syn-agent-deployment.md) | Canonical real Agent process, protected selected-leaf and current-group custody, owner-approved host-bound virtual-device IDs, distinct native UIDs and retained identity. Two-node native source-package acceptance passed; signed-release and official-package acceptance are separately gated. |
| SYN-R05 | Execution integration | Real node identity bound to Cog authorization and monitored sandbox dispatch; no fallback to synthetic actors |
| SYN-R06 | Upgrade and recovery | Staged upgrades, compatibility, crash recovery, rollback and nonrollback of identity/enrollment state |
| SYN-R07 | Removal and CI | Transitional `syn-node/` runtime removed, tests use pinned upstream release artifacts, negative security and multi-agent live-host qualification |

SYN-R02 must qualify actual package installation against the pinned upstream release on a supported host. SYN-R03 through SYN-R07 additionally require process isolation, live P2P/IPC, multiple simultaneous agents, revocation, restart, fail-closed behavior, operator permission unlock and resource accounting. Synthetic fixtures do not qualify production. CI must not download an unpinned `latest` release. Preserve prior phase records as historical evidence; do not mark them complete by rewriting their descriptions.

## Integration sequencing

Block production SYN agent launch until SYN-R01–R04; block production Cog dispatch until SYN-R05; block general rollout until SYN-R06–R07 and U16. U03's worker accounting, U06's identity custody and U04's Cog semantics are independent mandatory gates. Reconcile release distribution with the sandbox image workflow: Docker may build Potbot base images, but must not compile an unapproved SYN runtime. Store exact artifact digest and release identity in provenance and deployment manifests.
