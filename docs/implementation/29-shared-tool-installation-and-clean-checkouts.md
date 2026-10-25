# S4 — Shared workspace tools, clean checkouts, and per-agent editable clones

**Status:** implementation increment (unit-qualified, not host-isolation-qualified).
**Baseline:** Potbot public main `7bc8794615426ffeb6e8619ba97d60c2d02fb51c`, plus the S1/S2/S3 implementation prerequisites.
**Catalogue:** DanceFlow's source-audited seventeen components and the hub, as recorded in `potbot.tools.danceflow_concurrency_catalog`. This catalogue is a risk inventory, **not** a statement that all packages have been installed or that every download route has been observed.

## Persistent guest layout

```
/                                  pinned, read-only rootfs
/home/                             writable, persistent workspace home
  <agent-sha256-or-name>/          independently versioned work, Git checkouts and state
    Tools -> /usr/local/tools      symlink installed once from the agent skeleton
    projects/<tool>/              agent's EDITABLE, independent Git clone
    .cache/ .tmp/ runs/           per-agent ephemeral or reconstructible state
/usr/local/tools/                  writable, persistent workspace tool store (new exception)
  <tool-slug>/
    checkout/                     pristine, source-pinned Git checkout
    environment/                  reusable lockfile/ABI-pinned virtual environment
    static/                       reusable verified compile outputs, WASM and binaries
    downloads/                    reusable package tarballs/wheels/crates/npm archives
/var/ai/                           workspace-local EPHEMERAL model download staging
/opt/potbot/models/                operator's GLOBAL verified model library (read-only)
```

`/usr/local/tools` is **separate from `/home` and the rootfs**, persistent through
image upgrades, guest writable and backed by the workspace's own storage. The
shared model library is host-global, not a per-workspace tool directory. The image
needs the bind mount destination and a `/etc/skel/Tools` symlink. The trusted
agent-home skeleton independently copies `Tools -> /usr/local/tools` for each new
agent, without altering existing customized homes.

The workspace group is guest GID **69**, with simulated agent UIDs **420+**.
Shared directories are intended to be group-accessible and setgid (`2775`);
ordinary user files default to `0644`, directories to `0775`. Existing code has
not yet demonstrated per-agent kernel DAC enforcement on WSL2 idmapped mounts.
Different guest UIDs alone don't make a cooperative concurrency lease an OS
security policy. All agents are permitted to collaborate under this shared group.

## Shared tool lifecycle

1. **Discover.** Check the host-owned `SharedToolStore` entry for the exact tool
   and installed source commit. No download/reinstall if a qualified, pinned
   installation exists and the agent only needs to use it. Require host
   authorization and a per-tool **read** lease that remains renewed until the
   supervised execution ends. Different tools and multiple readers of the same
   tool are concurrent.
2. **Install new.** Stage a canonical Git checkout under a **write** lease for
   that tool. The host must validate source URL, exact commit, source cleanliness,
   dependency lockfile hashes, reusable artifact hashes, supported ABI and
   actual model download paths. Install the tool's environment outside the
   checkout and redirect package caches to its `downloads` directory. Never
   install unreviewed code into the immutable rootfs.
3. **Use shared.** Run the pinned installed executable directly from
   `/usr/local/tools/<tool>/environment/` or `/static/`, using pinned executable
   bytes, the shared environment and an **agent-specific HOME, TMPDIR and cache**.
   `PYTHONDONTWRITEBYTECODE=1` prevents importer-side writes into shared source.
   Ordinary interactive Bash and unknown-effect tools still require the broad
   workspace-exclusive lease until all unbrokered writes are mediated.
4. **Modify or upgrade.** Obtain a per-tool **write** lease. Do not start while
   any agent is executing the installed checkout. Perform the mandatory clean
   operation, then clone into `/home/<agent>/projects/<tool>` **while the same
   exclusive lease remains active**. A sandboxed Git Cog executes the clone:
   `git clone --no-local --no-hardlinks --no-checkout`; it checks out the exact
   pinned commit and checks that no `.git/objects/info/alternates` is present.
   The agent can then edit its own repository and environment independently.
5. **Share improvements.** Commit in the agent's own branch, run tests and
   publish through the concurrency-aware GitHub ref/file broker, with
   expected-HEAD CAS and review. Do not mutate the shared checkout or GitHub
   `main` through normal agent edit commands. Updating the shared tool uses a
   separately authorized exclusive upgrade transaction, with a new exact pin.
6. **Clean.** Every tool registration MUST ship a reviewed list of disposable
   checkout-relative outputs and pass a real clean fixture. `clean_for_edit`
   checks the pinned Git HEAD, remote origin, lockfiles and trusted artifact
   hashes. It refuses tracked modifications, unknown untracked files, ignored
   artifacts missing from the cleanup manifest and symlinked cleanup paths.
   It deletes only declared generated files/directories using descriptor-based
   no-follow traversal; no `git reset --hard`, `git clean -fdx` or scripts taken
   from an untrusted tool are run. Verify clean source again after cleanup.
   Uncertain filesystem mutation blocks further work until reconciliation.

**Meaning of clean:** exact **installed Git commit** (recorded from a named
upstream repository), clean index and worktree, no unknown ignored residue.
Checking for a newer public upstream commit is a separate **update preflight**;
clean never silently upgrades the tool's version. Per-agent edits are NEVER
cleaned or overwritten by maintenance on shared installations.

### Mandatory installer gate

Before installation or any environment upgrade:

```
PYTHONPATH=src python scripts/check_shared_tool_install.py --probe approved-tool-probe.json
PYTHONPATH=src python scripts/check_model_cache_policy.py --tool RasterMoves --probe model-probe.json
```

The first is a strict preflight over a versioned manifest describing exact Git
source, SHA-256 lockfiles and reusable artifacts, cleanup paths and an
**operator-observed offline** install/normal-use/cleanup probe. It checks that
package caches go under the selected tool's `downloads`, immutable/reusable
outputs under `environment` or `static`, per-agent runtime data under `/home`,
and all model download destinations under `/var/ai`. It rejects writes to other
checkouts and the shared global model library. The second gate independently
checks framework-specific model download routes. An offline JSON witness alone
is NOT authorization: the operator must independently execute, hash and audit
its actual probe and tool installation before registering the tool.

## Artifact classification (examples from DanceFlow inventory)

| Component | Reusable package / static | Per-agent runtime / clean-only |
| --- | --- | --- |
| StemLab | pinned Python environment, demucs/basic-pitch package downloads, built native viewer/FFmpeg assets | separation outputs, MIDI/stem timing scratch, render/video temp, pytest/cache |
| DanceRudiments | npm package archives, compiled WASM, C/C++ assemblies, authored *released* motion manifests | generated/unreviewed motion recordings and sample renders |
| FrameRudiments | Python env, JS visualizer bundles, pinned shader/sprite binary assets | video/codec outputs, temporary frames, preset analysis, test runs |
| DanceMoves | pinned npm deps, bundles, published tagged distribution archives | Next build scratch, previews and WordPress deployments |
| KeywordMoves | Python env and NLP package archives; model weights via `/var/ai` | SQLite monitoring DB/WAL, browser sessions, research JSON, review logs |
| RasterMoves | pinned inference runtimes, ONNX backends; models through operator model store | temporary images, partially downloaded weights, output comparisons |
| LikenessForge | Python/torch env, verified pipelines; weights via operator library | training states, private likeness assets, LoRA checkpoints and outputs |
| PixelCue / ThumbMoves | pinned model runtimes and static native libs | thumbnail caches, SQLite-WAL, model manifests in `/var/ai`, analyses |
| PrismMoves | pinned Python environment, model-compatible PixelCue adapters | visual-profile runs, contact sheets, output presets |
| React Timeline Sequence / MediaSlide | npm deps and pinned built React bundles | browser state, demo scratch, local generated pages |
| WorkflowMoves | released instruction templates and reviewed installer | project-owned instruction edits, installation records, temp reviews |
| WordPress EPK / DanceVault | verified plugin zip/build, immutable release artifacts | destination-host state, credentials, encrypted exports, upload logs |
| DanceFlow for Unity | verified UPM/WASM assets and released model binaries | Library/Temp/Obj/Cache, rig edits, project-specific assets |
| Potbot | pinned tool/identity binaries, Python/Next environment, downloaded packages | leases, workspace memory, jobs, model staging, task artifacts |

The classification does **not** assert these repos already support clean
execution out of tree. Source-specific installers need to be updated when a
canonical tool hard-codes downloads under `checkout/models` or writes untracked
runtime data alongside tracked files. Such a tool fails the gate rather than
compromising the clean shared checkout.

## Explicit missing qualifications

- **Host access control:** all agents can write `/usr/local/tools`; advisory
  leases can be bypassed by a raw shell. A hash check before exec does not
  prevent subsequent shared binary replacement or edits during execution.
  Before untrusted tools can run concurrently, use a protected immutable
  executable snapshot, per-execution read-only mount/verified fd, or true
  kernel-enforced write mediation. This implementation intentionally makes no
  claim of hostile concurrent-agent isolation.
- **Cross-process/machine:** the current lease ledger is host-private SQLite;
  multiple hosts require PostgreSQL-backed distributed fencing and ownership.
- **WSL2 host testing:** real Bubblewrap mount behaviour, GID 69 mapping,
  unrelated agent UIDs, shared-tool write permission, executable tamper,
  symlink races, network/Windows-drive denial and crash/reconcile paths.
- **Git integration:** the clone callback must be implemented by a qualified
  sandboxed Git tool. The operator only constructs and verifies its plan; it
  does not run arbitrary network clones or repository scripts as host root.
- **Installer:** actual package/environment creation and content-addressed
  artifact promotion must be brokered and reconciled. The preflight is not a
  fully automated installer. Lockfiles and download sources require exact
  pinned upstream evidence, signed artifacts where available and licensing.
- **Runtime:** shared environments aren't always portable across CPU/ABI;
  qualify execution on real hardware and preserve per-agent caches and profiles.
- **Filesystem:** destructive clean on a hostile concurrently modified tree
  still requires fully mediated writes/openat2 and a hardened host filesystem.

## Offline acceptance

```
PYTHONPATH=src python -m pytest -q \
    tests/unit/test_shared_tool_store.py \
    tests/unit/test_shared_tool_install_gate.py \
    tests/unit/test_shared_workspace_isolation.py \
    tests/unit/test_agent_skeleton.py
```

Run `scripts/qualify_shared_workspace.py` with `--shared-tools` on the actual
Linux/WSL2 qualified host before enabling shared shell access. CI rootfs image
publication does not imply successful sandbox qualification.

The live sandbox qualification now requires proof that **every** positive and
negative shell test was actually dispatched, the process was reaped, and the
supervisor returned an isolation identity. It also compares the host and guest
kernel network namespace identities; merely observing an outbound connection
failure is not proof of a network sandbox. Run the smoke only with a signed,
verified, extracted system image and disjoint controller-owned mounts. A passed
smoke does not replace the remaining host/WSL2 adversarial qualifications.

## Agent Bash banner — skeleton v4

Each newly provisioned agent home receives a copy of the upstream Kieran Simkin
MOTD scripts and a host-provisioned `.config/potbot/workspace-name`. For an
unnamed agent (home directory is its 64-character SHA-256 ID), the interactive
FIGlet greeting displays the workspace name, not the anonymous ID. When the
agent obtains a name and its home is renamed by `AgentHomeRegistry`, its default
FIGlet title becomes that name without changing custom scripts or re-copying
the skeleton. A named agent can customize `banner.title`, `banner.font`
(`standard`, `slant`, `small`, `big`, `shadow`, `banner`) and `banner.color`
(`rainbow`, `plain`). Noninteractive shells remain silent, and the renderer does
not evaluate guest-supplied shell expressions. The shipped, byte-pinned
`10-figbanner` original is preserved as a reference; `potbot-welcome` implements
the dynamic FIGlet/lolcat variation. Existing v1–v3 customized homes are not
automatically overwritten: their owners require a reviewed opt-in migration.

The provisioner normalizes workspace display names to a bounded ASCII FIGlet
label, so international symbols and long workspace names cannot block home
creation. The original workspace identity remains authoritative in the host
registry; this label is cosmetic only.
