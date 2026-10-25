# S2: concurrent shared workspace tools, agent identities and DanceFlow resource audit

**Reviewed:** 25 October 2026. **Source:** [Potbot](https://github.com/kieransimkin/potbot) `main` at `9a0c9ed9e410e93d5e0a4a6d7262c8e529bdb067` plus the additive S1 shared-rootfs patch. This specification extends the S1 design in [26](26-shared-workspace-sandbox-and-concurrency.md). Do **not** interpret source-code/unit checks as qualified Linux/WSL2 OS isolation.

## Identity and home layout

Each workspace owns **one versioned rootfs** and **one persistent `/home`**, shared across its agents and tools. Agents are distinct logical identities, not separately provisioned Linux login accounts. The controller allocates simulated UIDs starting at `420`, never reuses a retired ID, and identifies the workspace's shared group by a POSIX-safe form of its name with **GID 69** (within that workspace's user/group namespace). Example:

```
/home/                               # persistent workspace root; sticky parent recommended
  .tmp/                              # per-workspace scratch, NOT an OS-global /tmp
  <sha256-of-agent-uuid>/            # first name, group directory mode 0775
  coding-agent/                      # same identity after controller-guarded rename
  research-agent/                    # same workspace GID 69
```

Directory rename is CAS/versioned and protected by an exclusive workspace lease. A root-level sticky `/home` directory (mode `01775` when supported by the host's qualified mapping) prevents peers from renaming another agent's *top-level* directory without authority. The host must forbid symlink aliases and inode substitution of registered directories. The controller's identity ledger, lease database, credential broker and image store stay **outside** `/home`.

**User-specified modes:** new agent directories `0775`, ordinary files `0644` (`0664` offered explicitly for shared in-place editing). `0775` allows group members to create/delete/replace entries; `0644` does **not** allow group members to edit a file in place, but a group-writable parent may allow atomic replacement. `0755` on a directory blocks peer creation/rename there; it does **not** hide file contents or prevent a peer changing an existing `0664` file. Use `0700` for a private directory and `0600` for secret files. `0755` on a *file* likewise offers no privacy. Logical `mode` checks are implemented for mediated file writes and tested; directory permissions do not bypass a higher-level workspace policy.

**Important enforcement qualification:** Bubblewrap's `--uid 420 --gid 69` can set the *displayed guest IDs*, but mapping both identities onto the same **host** UID does not enforce POSIX DAC between them. A real mode-protection claim requires independently qualified subordinate UID/GID mappings, idmapped per-workspace mounts, or an enforcing per-agent filesystem mediator, plus proof that a peer cannot bypass protections by unlinking/replacing the parent entry. Until then, arbitrary Bash runs retain an exclusive workspace-wide lease, and the API must advertise `kernel_identity_qualified: false`. The rootfs image's `/etc/passwd` and `/etc/group` must later be populated by a **host-owned, read-only per-workspace projection** with workspace group name and allocated UID records; never give `/etc` to guests as writable. Test on actual WSL2 + Bubblewrap and native Linux before enabling multi-UID collaboration.

## What is executable in this increment

- `WorkspaceLeaseLedger`: cross-process fenced read/write claims, independent resource claims, durable indeterminate state after crashed dispatch; GitHub/external resource conflicts span workspaces.
- `ToolGateway` in shared-rootfs mode: exactly pinned qualified runtime and workspace-exclusive lease around **arbitrary** executables (including those whose manifest calls them read-only), with renewal and no uncertain replay. It remains integrated with existing task budgets and SYN execution evidence.
- `SharedBashSessionSupervisor`: bounded interactive Bash PTY with authorized creation, resize, detach, reattach and explicit termination. Detaching a window does **not** release process leases. Session state is in-process; separate host-service/WS transport and crash reattachment remain future work. Named agent directories can provide the initial CWD and logical UID/GID; these are **not** kernel protection receipts.
- `SharedFileBroker`: a narrow, host-mediated file-write route. Requires an authorized actor, pinned image, exact canonical path, expected SHA-256 (or explicit absence), staged write + fsync + atomic replace + receipt and exclusive same-file lease; different files can be modified concurrently. It never follows symlinks and checks simulated directory permissions for registered agent homes. Rogue unbrokered writers can still race this primitive until filesystem mediation or OS credential isolation is qualified.
- `concurrency_profiles`: footprint constructors for typed file mutations, package managers, SQLite, per-digest models, result directories and unknown shell commands. **These footprints are only trusted when host-enforced input/output boundaries have been separately qualified.**
- `GitHubFileChangePlan`: version-pinned files on an agent-owned staging branch; file-level resource claims permit nonoverlapping changes. Final remote ref publication remains exclusively serialized through `GitHubChangeIntent` and atomic GraphQL `updateRefs(beforeOid=...)`. A moved branch/head means rebase and re-test—not silent conflict resolution.

## DanceFlow audited resource-risk matrix

The review inspected [the canonical DanceFlow 17-component catalogue](https://github.com/kieransimkin/DanceFlow/blob/8963a2f270ec5264ab78974ec22a77258e1e2161/catalogue.json), tree listings and `.gitignore` patterns for all 17 components plus the DanceFlow MCP hub. The current source commit, repository and hazard descriptors are retained as structured data in `potbot.tools.danceflow_concurrency_catalog.DANCEFLOW_RISKS`. **This is exhaustive at the published component-inventory level**, not a claim that every runtime write site or plugin-configurable path has been dynamically traced. The `*.sqlite`/cache output directories listed below are often intentionally untracked; an absent checked-in file does not mean an absent runtime conflict.

| Component | Concrete shared writes / risk | Minimum lock coverage |
| --- | --- | --- |
| **DanceFlow hub** | `demos/package-lock.json`, `node_modules`, MCP background jobs and registry recipe outputs | Demo environment writer; per-job output IDs; external registry publication |
| **StemLab** | `package-lock.json`, `docker/requirements.lock.txt`, `.cache`, `.work`, `results`, `models`, stem/cue outputs | Package manifest and env; model digest; per-output session; cue destinations |
| **DanceRudiments** | `package-lock.json`, build/wasm, `generated`, `.cache`, optional motion packs, pattern/recording manifests | Env + release-generated tree; each downloaded pack/digest |
| **FrameRudiments** | `visualiser_assets/package-lock.json`, `.uv-cache`, `models`, `visualiser-analysis` catalogues, render/checkpoint outputs | Dependency env, model digest, per-render output, shared preset DB |
| **DanceMoves** | `package-lock.json`, `.build`, `.next`, `dist`, `release-output`, WordPress packages | Build target/version and release destination; external WordPress deployment |
| **KeywordMoves** | `.keywordmoves-cache`, `models`, private SQLite monitor, WAL/SHM/journal and watchlist records | SQLite DB-family transaction; watch ID/capture; external account quota |
| **RasterMoves** | `models`, `model-cache`, downloaded `*.part`/`*.lock`, upscaling outputs | Per-model checksum+revision, atomic publish; output per job |
| **LikenessForge** | `uv.lock`, `models`, LoRA checkpoints, `outputs`, training scratch | uv environment and lock; model identity+checkpoint; GPU; output per run |
| **PixelCue** | `uv.lock`, model manifests, `.cache`, `models`, SQLite WAL, scanner/thumbnail cache | Model digest/manifest, SQLite DB, GPU, thumbnail cache key |
| **ThumbMoves** | Shared OS thumbnail cache (within PixelCue repository) | Per-device/item cache key; OS cache mutation may require global broker |
| **PrismMoves** | `outputs`, `models`, preset reports; uses PixelCue backend | Per-report output and shared PixelCue model/GPU token |
| **React Timeline Sequence** | `package-lock.json`, `node_modules`, `dist`, `package-artifacts` | JS environment, npm cache, dist publication |
| **EPK Social Metadata** | `dist`, `build`, WordPress metadata, release artifacts | Per-asset staging; WordPress page/record and release |
| **WorkflowMoves** | `install.py` writes managed project instruction targets and an install record | **Target project paths as well as source files**; lock preview+write+readback |
| **DanceVault** | `private`, encrypted export, integration state, WordPress delivery | Private vault path, destination ID and database write transaction |
| **MediaSlide** | `package-lock.json`, `node_modules`, `dist`, demo/release assets | Dependency env, dist output and deployment version |
| **Potbot** | `uv.lock`, Rust `Cargo.lock`, frontend dependencies, memory graph, PostgreSQL and guest `/home` | Per-package env + lock; snapshot/versioned memory; lease ledger; GitHub branch |
| **DanceFlow for Unity** | `asset-lock.json`, `song-lock.json`, `Library`, `Temp`, `Builds`, asset DB, WebGL outputs | **Unity project Library/asset database exclusive writer**; independent build outputs |

## Concrete broker rules for high-contention resources

1. **Package lockfiles are versioned input manifests, not synchronization locks.** `uv.lock`, `package-lock.json`, `Cargo.lock`, etc. can have arbitrarily many readers. Regeneration takes a write claim for the exact manifest and the affected package environment; installation claims the environment for write and the manifest for read. The separate transient `*.lock` files owned by package managers must not be deleted as a broker's recovery technique. If a dependency manager uses an external shared cache, add a global `external` resource claim; if it supports reliable per-key atomic immutable blobs, refine that claim by content digest.
2. **Python/Node/Unity build directories:** one writer to the same `.venv`, `node_modules`, `target`, `.next`, `Library`, `Obj`, or build output. Different repo or target directories can proceed simultaneously. Unity's Library database gets an entire-project exclusive claim unless a qualified Unity editor service already provides finer transaction semantics.
3. **Model stores and download partials:** install into a unique staged directory `models/<tool>/<model-id>/<revision>/<quantization>/<digest>`, verify checksum, publish atomically. Readers share immutable digests. Parallel distinct digests are fine; concurrent writers of the same digest conflict. Maintain a separate cache-GC exclusivity claim to avoid deleting an in-use mapped checkpoint. GPU VRAM reservation is a separate non-filesystem resource.
4. **SQLite journals:** `store.db`, `store.db-wal`, `store.db-shm`, `store.db-journal` share one logical key. Use SQLite's normal transactional concurrency inside a single qualified writer service; coordination locks guard migrations, snapshots, backup/restore, out-of-band file replacement and multiple agent-owned SQLite connection workflows. Never copy only the `.db` file while WAL is live.
5. **Memories and indexes:** journal append-only memory facts with CAS revisions, immutable embedding snapshots and generation-scoped vector manifests. Readers share published generations; index compaction, mapping changes and embedding re-creation hold the target index's write lease. Do not lock all memories for an unrelated conversation read. Distinguish an agent's own draft from shared promoted memory.
6. **Render and multimedia outputs:** use a unique `run_id` directory for StemLab, FrameRudiments, RasterMoves, LikenessForge, PixelCue, PrismMoves and Unity jobs. Reserve final destination names for publication; if `latest` aliases are updated, use source-bound CAS. Never let separate jobs render to the same `.mp4`, frame-sequence or comparison-sheet path without a broker claim.
7. **Cross-project installs and external publication:** WorkflowMoves editing another repository must lock that target's instruction file and install record, not just its own checkout. WordPress upload, GitHub releases, model registry, PyPI/npm/Conda, and other remote mutations use globally scoped external resource keys and independent host authorization.
8. **Secrets and credentials:** credential files, login tokens, auth context and private keys are not generally sharable. Store them outside `/home` with a native broker. A model-proposed tool resource name cannot grant access to one.
9. **Git metadata:** `.git/index`, refs, `packed-refs`, objects/pack and worktree files form a special consistency group. A `git fetch` mutates objects and refs: it is **not** a read-only file operation. Use separate agent worktrees and a broker around shared common `.git` directories, or independent clones for true parallel builds.
10. **Unknown exec commands and PTYs:** broad workspace-exclusive write claims stay mandatory because a shell's observed command arguments do not bound its filesystem effects. Narrow execution can only be admitted when the host has actually constrained writes to the declared resources (OS read-only mounts, mediated syscalls, trusted typed operations, or an enforcing filesystem).

## Concurrent GitHub file lifecycle

1. Pin the source repository and exact branch head `beforeOid`; collect per-file blob OIDs and a content-hashed patch plan. Use separate agent-owned staging refs (stable agent UUID, not mutable home-folder display name).
2. Allocate read leases for source inspection, then write leases for exact *staged files*; disjoint files can be processed in parallel. Compare source blob hashes immediately before patch admission. Keep untracked local object-store/packfile changes under distinct local worktrees or a separate git-metadata broker.
3. Build new tree/commit **without** moving the shared remote target ref. Validate formatting, tests, provenance and credentials/permissions outside the untrusted guest.
4. Acquire the short **remote branch write** lease, atomically call GitHub GraphQL [`updateRefs`](https://docs.github.com/en/graphql/reference/git) with both `beforeOid` and `afterOid`, then read back the actual ref. A changed head requires a new comparison of all affected blobs and repeat qualification. Never use a non-forced REST ref update as exact CAS.
5. The reference writer only updates the agent's own branch; protected `main` requires a separately approved PR/review/merge path. Neither file-level read rights nor a branch resource grants permission to submit or merge a PR.
6. After a connection timeout or ambiguous response, mark the mutation **indeterminate** and reconcile the remote ref before any repeat. Keep a journal of the expected old head, patch hash, model/tool run and resulting verified head. Concurrent branch writes across *different workspaces* must still conflict globally.
7. For different files on the same eventual target branch: GitHub commits still form a **linear** history. File-scoped staging parallelism does not imply that two independent final head updates can both succeed without rebasing.

## OS qualification tests that must precede enabling kernel-protected agent modes

- WSL2 distro has Windows automount/interop disabled, no DrvFS/9p mount escapes, user namespaces enabled and Bubblewrap/seccomp available; guest cannot access Windows files or host network by default.
- `id -u` returns 420, 421, … and `id -g` 69 for separate agent sessions. `id -gn` resolves the workspace's named group via a host-provisioned **read-only** identity projection, without changing shared image bytes.
- Verify real file ownership mapping (not just guest `id` output): agent A and B can use a default `0775` directory, and one cannot edit/rename protected `0755`/`0700` trees after agent A changes modes. Verify sticky `/home` prevents stealing A's directory, even if B has `/home` group write.
- Verify no host UID mapping collision, no `CAP_DAC_OVERRIDE`/`CAP_FOWNER` in guest, and no privilege regain through `setuid`, user namespaces, FUSE, hardlinks, symlinks, open file descriptors, or other agent shell processes.
- Crash/restart tests for PTY lease expiry and reconciliation; memory/index snapshots during concurrent compaction; parallel builds and cache GC; GitHub remote CAS conflict/readback; test that model output cannot declare its own resource entitlements.
- Only after above may the frontend display "mode-protected from other agents" or permit general concurrent **unrestricted** shells in one `/home`. Until then, logical UID/GID and mediated-file checks must be presented accurately, not as OS proof.

## Implementation phases still to complete

**S2 (this increment):** source-backed hazard catalogue, narrow typed-operation leases, guarded atomic file writer, synthetic UID/GID registry, namespace launch arguments, Bash session lifecycle, legacy ToolGateway exclusive execution integration, concurrent GitHub file intent and tests.

**S3:** qualified per-agent UID/GID remapping/identity projection under WSL2/Linux, sticky root home and kernel access-control proofs, broker-mediated operations for other tool protocols, filesystem write-set attestation. Translate legacy `ToolDefinition` effects into trusted footprints only after sandbox-enforceable write limits are available.

**S4:** PostgreSQL-backed distributed lease journal (SQLite current code works on one trusted host), multi-host job workers, persistent PTY reattachment and authorized browser sessions, adaptive concurrency and package-manager-specific adapters. Model cache/download staging, memory promotion and release tooling must emit durable progress events and use the same broker.

**S5:** CI performs a source-pinned complete DanceFlow component-rescan, compares identified write sites with registered effects, fails when new mutable paths are unclassified, and runs adversarial concurrent executions on Linux and WSL2; continuously re-qualify after kernel/WSL/image/package upgrades.

## Agent-home skeleton v1 — Kieran Simkin's original welcome scripts

Agent creation installs a **copy** of the immutable, versioned skeleton under the
new `/home/<sha256(agent UUID)>` directory, before the agent-home mapping becomes
visible. It never copies on a subsequent lookup, name change, shell reopen or
rootfs image upgrade. Later source/template releases are intentionally **not**
automatically merged into customised agent dotfiles.

The original scripts come from the public repository
[`kieransimkin/update-motd.d-scripts`](https://github.com/kieransimkin/update-motd.d-scripts/tree/2485c90143dae3dd336769e7ab68ebcca781fa8a),
exact Git commit `2485c90143dae3dd336769e7ab68ebcca781fa8a`:

- `10-figbanner` — `figlet -c Welcome` piped to `lolcat -f`, Git blob
  `1c8ec80cb1f6e23f31545303296d86cdd47d1540`.
- `20-fortune` — `/usr/games/fortune` piped to `lolcat -f`, Git blob
  `afd270e021bc3905a678c31ed220f3c371fa9766`.
- `99-insult` — arrays of Shakespearean adjectives, modifiers and nouns,
  with `$RANDOM` choosing each phrase, Git blob
  `97f27ca76757794327e3b803a4a2108bfa506172`.

The source files are vendored **byte-for-byte** at
`src/potbot/tools/agent_skeleton/.local/share/potbot/motd.d/` and checked
against pinned SHA-256 bytes at provisioning. The copied `.bashrc` invokes a
bundled `.local/bin/potbot-welcome` only when Bash is interactive, stdout is a
TTY, and `POTBOT_WELCOME` is not `0`. The wrapper checks whether the three
programs are installed, skips missing optional decoration without downloading
anything, and does not run from non-interactive tool executions. The versioned
system rootfs build adds Debian `figlet`, `fortune-mod`, `fortunes-min` and `lolcat`, exactly as
the user's upstream README specifies. Bash PTY startup loads `.bashrc` rather
than using `--norc`; the rootfs sets `$HOME` to the named agent home.

The skeleton contains no secrets, private credentials or software-update hooks.
Ordinary dotfiles are `0644`, script executables `0755`, and new folders `0775`
with a documented `potbot-agent-home/v1` template receipt in
`.config/potbot/skeleton.json`. The receipt describes source/template bytes but
is **not** an authentication token or proof of kernel UID isolation.

Provisioning is under the existing workspace-exclusive lease, with no-follow
file-descriptor operations, create-only writes and mode verification. A crash
while copying leaves an indeterminate lease and may leave an unregistered
partial directory; an administrator must reconcile it, not silently replay or
overwrite. Renaming the directory keeps the original files and version receipt.

Tests verify the exact three upstream Git blob IDs, file/dir modes, independent
agent copies, immutable first provisioning, edited files preserved on rename,
source tampering and symlink rejection, and noninteractive Bash silence. The
source-installed tests do **not** claim rootfs package presence or a successful
Linux/WSL2 Bubblewrap boot; run the qualified shell/TTY smoke against an actual
published image before enabling default welcome messages in production.
