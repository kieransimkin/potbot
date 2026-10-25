# S9 — Tested shared-tool upgrades, atomic activation, and rollback

**Target:** `kieransimkin/potbot` S8 main `77386eb9a8a3b7ebd734dae3c94921724acda4dd` (25 October 2026). This is an **additive host-operator** increment, not a new container manager, remote download mechanism, or permission for agents to run host commands. It extends the existing S4 `SharedToolStore`, S5 read-only tool mount / installed-binary hash gate, S6/S7 monitored `/etc/ai`, and S8 operator CLI.

## Complete upgrade state machine

| State | Active installation | Stage / retained backup | Allowed next action |
| --- | --- | --- | --- |
| Unregistered | Existing registered tool unchanged | Host-prepared candidate stage | Run approved tests |
| `ready` | Old pinned release | Candidate, tests passed | Explicit `activate` |
| `switching` | Exchange may have occurred; block all brokered uses | Candidate or old release | **Only** lease reconciliation then explicit `reconcile` |
| `active` | New release, SQLite pin updated | Exact old release retained | Explicit `rollback`, or a separately prepared later upgrade |
| `rolling_back` | Exchange may have occurred; block brokered uses | Prior/new version | **Only** lease reconciliation then explicit `reconcile` |
| `rolled_back` | Old release restored and re-pinned | Rejected/new release retained | Operator review/prune in later stage |
| `aborted` | Old release retained | Candidate still staged for inspection | New operation UUID required to test again |

Activation uses Linux `renameat2(RENAME_EXCHANGE)` on **two physical sibling directories in the same registered workspace tool store**. The OS either exchanges both directory entries or neither. S9 does **not** fall back to two sequential renames or cross-filesystem copies. The installed `trusted_shared_tools` record and S9 transaction state are committed in **one SQLite transaction after the exchange**. A crash between exchange and commit leaves `switching`/`rolling_back`; the previous pin alone is not authority to continue running a tool. `SharedToolStore.definition` now rejects pending uncertain upgrades. Recovery requires a separate operator action, and an *indeterminate workspace lease must first be independently reconciled by the host controller*. Nothing silently retries an uncertain side effect.

## Test admission and evidence

1. **Build outside the active checkout.** An existing qualified installer/staging process (not this module) prepares `<tools-root>/.potbot-s9-stage-<slug>-<operation-uuid-hex>/{checkout,environment,static,downloads}`. The candidate directory must be an unlinked physical directory, on the same filesystem as the active `<tools-root>/<slug>`. Build-time network access, package licensing, artifact hash verification and origin trust are separate operator responsibilities; **S9 never runs `git clone`, `pip install` or arbitrary tool scripts on the host**.
2. **Review exact pins.** Declare the canonical GitHub origin, 40-character commit SHA, dependency lockfiles, reusable artifacts with SHA-256 and explicit checkout cleanup paths. The candidate must have that `HEAD` and origin, clean tracked/untracked/ignored state, intact lockfiles and declared SHA-256 assets. Host-owned Git uses the existing no-hooks/no-implicit-network introspection.
3. **Run an operator-pinned test suite.** At least one **smoke** and one **unit** test are required, with unique names, versioned `argv` and bounded per-test/aggregate deadlines. The concrete `SandboxedToolTestExecutor` uses `ProcessSupervisor` and the S5/S7 shared Linux Bubblewrap backend with per-agent identity, cgroup/process telemetry and cancellation when the S7 watcher fails. Stage content is visible **read-only** as `/usr/local/tools/.potbot-s9-stage-…`; all test output must be written into the agent home. There is no host-subprocess fallback. Each test must actually dispatch, exit 0, terminate its entire supervised process tree and return an isolation identity.
4. **Verify cleanup.** S9 creates sentinel files under each **declared ephemeral checkout path** in the disposable staged checkout, executes the **same `SharedToolStore._remove_declared` cleaner used for normal tools**, then rechecks exact Git cleanliness. It does not invoke a repository-supplied cleanup script on the operator host. This proves the generic path cleaner, not every language-specific cache behavior; tool-specific cleanup needs an additional smoke case.
5. **Capture complete reusable artifact evidence.** In addition to individual operator-pinned SHA-256 files, S9 records a bounded content fingerprint of every regular file and symlink under `environment/`, `static/`, and `downloads/`, including file modes, without following symlinks. Unexpected hardlinks/special files fail closed. This is rechecked before activation; unpinned changes also reject the upgrade.
6. **Keep tests out of the active release.** A successful `test` records a durable `ready` transaction with suite digest, exit/status evidence, output hashes, isolation identity, source, environment and cleanup evidence. The *old* installation remains available and unchanged.
7. **Explicit promotion / rollback.** `activate --confirm` requires old commit CAS and a current recheck of both trees, then atomic swap, readback and DB CAS. `rollback --confirm` independently rechecks both trees and the new commit, swaps back, and updates the installed pin and journal transaction atomically.

## Operator command and input contract

Run the commands from a **qualified Linux/WSL2 host**, in the same credential/identity context as Potbot's S8 operator. These are **maintenance-mode commands**; the S7 `/etc/ai` monitor owns an exclusive lock, so cleanly stop an already-running operator daemon and ensure all leases are drained before invoking mutating commands. All arguments, stages and plans are operator-provided; never accept them from a model or user-facing API without an independent security decision.

```bash
OPERATOR=/srv/potbot/operator-manifest.json
AGENT=11111111-1111-4111-8111-111111111111
OPERATION=22222222-2222-4222-8222-222222222222
BASE=(--manifest "$OPERATOR" --workspace-name studio --agent-id "$AGENT" \
      --operation-id "$OPERATION" --slug stemlab)

# Read-only: inspect latest transaction (does not start a new S7 watcher).
uv run --locked python scripts/tool_upgrade_s9.py "${BASE[@]}" inspect

# Operate ONLY on a previously provisioned and independently approved stage.
uv run --locked python scripts/tool_upgrade_s9.py "${BASE[@]}" test \
  --plan /srv/potbot/private/stemlab-upgrade.json

# After inspecting test evidence and explicitly approving the new release:
uv run --locked python scripts/tool_upgrade_s9.py "${BASE[@]}" activate \
  --expected-old-commit OLD_40_HEX_SHA --confirm

# Require a new explicit decision to revert:
uv run --locked python scripts/tool_upgrade_s9.py "${BASE[@]}" rollback \
  --expected-current-commit NEW_40_HEX_SHA --confirm

# After externally resolving an uncertain workspace lease, recover an
# interrupted *known* directory exchange; never auto-retry it:
uv run --locked python scripts/tool_upgrade_s9.py "${BASE[@]}" reconcile --confirm

# Inspect the new executable hash pins for runtime admission:
uv run --locked python scripts/tool_upgrade_s9.py "${BASE[@]}" runtime-pins
```

The `potbot-shared-tool-upgrade-plan/v1` JSON uses exactly `schema`, `expected_old_commit`, `replacement` (an existing `SharedToolDefinition` with `slug`, `repository`, `commit`, `cleanup_paths`, `lockfiles`, `model_downloads`, `reusable_artifacts`), and `tests` with bounded `name`, `kind`, `argv`, `timeout_ms`. Templates `{checkout}`, `{tool}`, `{agent_home}` expand only to operator-derived guest paths; shell invocation is **not automatic**. Example test cases might run a pinned `/bin/bash` binary with explicitly written arguments or an image-pinned Python/Node/Rust interpreter with a version-pinned source test path. The minimal rootfs does **not guarantee those interpreters are installed**. An unrecognized executable fails without host fallback.

**After activation:** Existing `SharedWorkspaceRuntime.tool_entrypoints` hashes for changed installed executables become stale. S9 extends the S8 operator manifest with an **optional** `tool_entrypoints` object of approved guest executable path → SHA-256; legacy manifests remain accepted without this field. They must not be ignored: `runtime-pins` proposes new executable hashes from tested SHA-pinned reusable assets, plus prior pin entries to retire. The operator must explicitly approve/recreate its immutable runtime admission contract and restart/reconcile affected workers. Until re-pinned, execution of a changed binary fails closed. The upgrade operation does **not** itself rewrite the signed image digest or grant new network/credential permissions.

## Acceptance and limits

Offline real-Git suite (not proof of an actual Bubblewrap host):

```bash
uv run --locked python -m pytest -q \
  tests/unit/test_shared_tool_upgrades_s9.py \
  tests/unit/test_s9_sandbox_test_executor.py \
  tests/unit/test_operator_runtime_pins_s9.py \
  tests/unit/test_s9_operator_cli_contract.py \
  tests/unit/test_shared_tool_store.py \
  tests/unit/test_shared_tool_mounts_s5.py
```

Live qualification must additionally demonstrate an actual staged smoke and unit test on Linux/WSL2 with **dispatched + reaped processes**, read-only shared mounts, working agent-home isolation, denied network / Windows drive access, real executable admission after operator repin, concurrency with a held read lease, `SIGKILL` before and after the physical exchange, controller restart, explicit lease reconciliation, tested rollback and protection from symlink/hardlink races. The existing `scripts/qualify_shared_workspace.py` is only a prerequisite, not this complete gate.

The implementation does **not** yet provide: a remote repository fetch/build/stage orchestrator, globally distributed leases across multiple hosts, live UI controls for mutations, full operator-signature protection of SQLite, automatic signed image-runtime repinning, complete language-specific cleanup scripts, telemetry benchmark comparison across versions, guaranteed resistance to malicious writes from the host UID, or host-to-worker IPC while the S7 watcher is held by a separate process. These require independent qualification. Avoid representing S9 as a universal tool upgrade service until those parts are connected.
