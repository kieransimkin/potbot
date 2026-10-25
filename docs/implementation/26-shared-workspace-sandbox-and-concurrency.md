# Shared-workspace execution — stage S1 (Linux, WSL2 + Bubblewrap)

**Source baseline:** Potbot public `main` at `82845732f93b977f1d60a8d17c258fe4bdd38621` (25 October 2026). This increment adds a controller-side admission **foundation** and an image build workflow. It does **not** silently replace existing per-candidate Bubblewrap execution or assert that WSL isolation has passed a live test.

## Target execution architecture

Each workspace has **one** persistent `/home` tree. Every chat, agent, Cog and tool in that workspace receives a stable workspace identity, but a separate agent identity and an admitted capability. Tools access that shared home under a host-side concurrency lease. Other workspaces get other home trees and sandbox processes. The guest shell is `/bin/bash`; its initial CWD and `$HOME` are `/home`.

Everything except `/home` is recreated from a versioned, signed system image. The Linux image is mounted read-only, with `/home` as the only persistent read-write host mount. `/tmp` and `/var/tmp` are links to `/home/.tmp`. `/proc` and `/dev` are kernel-provided pseudo-filesystems, **not persistent writable application volumes**. No Windows drive, host secrets, Docker socket, SSH agent, host home, network namespace, or ambient executable root is bound into the guest. Outbound GitHub/network interactions must use separately authorized host-side brokers.

- Linux: namespace/seccomp/prlimit through the existing Bubblewrap policy.
- Windows: the controller's **WSL2 guest** uses that same Linux Bubblewrap/seccomp policy. No dedicated Hyper-V VM managed by Potbot.
- macOS: not qualified; a separate Linux guest backend remains a future adapter, never an insecure native fallback.

WSL is not a security boundary from its Windows user. The operator must use a least-privileged Windows account, control the WSL distribution and restrict Windows filesystem access. `/etc/wsl.conf` must disable automount, fstab automount and interop; see `sandbox/wsl.conf.example`. Restart the distribution after applying it. The preflight rejects WSL1, missing/malformed settings and observed DrvFs/9p mounts. These checks are defense in depth, **not proof of isolation**. The actual guest Bubblewrap/seccomp/escape suite is a release gate. A qualified host with elevated privileges and a Windows threat model still needs a separate security review.

## Initial implementation

| Component | Delivered S1 | Required follow-up |
| --- | --- | --- |
| Root filesystem | `sandbox/compose.build.yaml`, minimal Debian Bash Dockerfile, CI build/push/sign | Host verified signature, secure OCI unpack into immutable directory, actual image/guest conformance tests |
| Image identity | Source SHA, immutable image digest and platform in `sandbox_image_manifest.py` | Host qualification receipt, persistence and rollback UI, cache GC with active-image pins |
| Image pins | SQLite CAS by workspace revision; refuses changes while active/uncertain tools exist | PostgreSQL lease/history service, atomic mount handover and running-shell quiescence |
| Shared access | Read/write/unknown-effect declarations, subtree conflict locking, fencing tokens, cross-process SQLite transaction | Descriptor-based TOCTOU-safe path handles, wire into every Cog, PTY and tools; multi-host PostgreSQL locks |
| GitHub | Agent-scoped branches, repository/ref scope, exact HEAD CAS/readback, host-only `GitHubGraphQLClient` with explicit repo allowlist and vault token supplier | Verified commit construction, PR/review policy, GitHub fixture acceptance and merge gate |
| WSL2 | Host configuration preflight and Bubblewrap image launch builder | WSL distribution provisioning, real namespace/seccomp support probe, Windows adversarial host-boundary tests |

`WorkspaceLeaseLedger` is **one trusted host only**, and its SQLite file must live outside every workspace's `/home`. Its cross-process transactions serialize conflicting claims. Two different workspaces may modify their own `/home/foo` concurrently. **GitHub branches and named external resources are global**: a conflicting writer in another workspace must be serialized too. Unknown tool effects obtain an exclusive workspace lease. Reads of the same resource run concurrently; any read/write overlap conflicts. All leases bind a workspace, agent, image digest, operation, idempotency key and resources. Expired/dispatched operations become indeterminate and BLOCK further overlapping writes until a trusted operator reconciles the outcome. In-process locks or agent-authored read-only hints never grant privileges.

This is an admission broker, not filesystem enforcement: before production, each legacy ToolGateway/ProcessSupervisor path must be bound to admitted leases, and filesystem opens must use race-resistant `openat2`/fd-relative resolution. Rejecting lexical traversal and symlinks is necessary but not sufficient for a hostile concurrent agent. An unbrokered shell in the same writable home could still bypass this ledger and modify files; enforce per-command mediation or isolated per-agent working views with serialized promotion before claiming robust concurrency safety.

### GitHub source updates

Keep a **per-agent Git worktree/branch** under `/home` for speculative changes; shared canonical refs are read-only to agent shells. Before publishing, the controller verifies the patch and prepared commit against its exact base tree, and admits a global GitHub ref write lease. `ConcurrentGitHubTool` then checks the expected tip, delegates an **atomic** GraphQL `updateRefs(refUpdates:[{name,beforeOid,afterOid,force:false}])` to an authenticated host adapter, rereads the ref, and emits a receipt. Its general helper `graphql_update_refs_variables()` constructs the strict precondition input. GitHub REST's `force:false` is NOT an exact-old-SHA CAS. Direct updates of `main` or other branches through this tool are forbidden: only the scoped agent branch is allowed. Pull requests and merging are separate explicit operations with branch protection/approvals. A timeout after dispatch stays indeterminate, not silently retried. Host credential never enters the sandbox.

### System images: build, publish, select, run

1. CI validates the build contract, obtains a digest-pinned upstream Debian base and uses `docker compose -f sandbox/compose.build.yaml build` (never `up`) to produce linux/amd64 and linux/arm64 variants.
2. On `main` or an explicitly authorized release, CI pushes those version-tagged images to GHCR and signs **the published OCI digest** with keyless cosign OIDC. The output manifests record source commit, platform, digest and read-only requirements; they do not pretend to certify runtime qualification. PRs build without publication.
3. A *trusted host provisioner*, not an untrusted shell, verifies the OCI signature against the expected repository/workflow identity, pins exact digest/platform, copies the image with an OCI-aware tool such as `skopeo`, and unpacks it with `umoci` into a controller-owned protected directory. Never `tar -xf` an untrusted image directly into the host filesystem. Verify extracted ownership/mount destinations, kernel ABI and source image digest before admitting it.
4. The host provisions `/home/.tmp`, records a separate *actual* sandbox qualification receipt and atomically pins the image revision. Any active/indeterminate tool, terminal, browser or agent blocks a version swap. No automatic writable rootfs overlay.
5. WSL2 Linux `SharedWorkspaceBubblewrapBackend.prepare` constructs `--ro-bind <qualified-rootfs> / --bind <workspace-home> /home`, `--unshare-all`, seccomp, prlimit and a Bash-available guest runtime. It does not mount host `/usr`, `/mnt/c`, provider credentials or the network. Linux native uses the same builder. A new launch never silently falls back to the old host-root-based mounting strategy.
6. On upgrade, keep old immutable rootfs revision/digest and rollback only through explicit CAS after quiescing the workspace and reconciling uncertain operations. Persist and display actual current image digest, source commit, qualification digest and revision. Garbage collect only unreferenced images.

**Do not confuse an OCI digest with a rootfs directory checksum.** The digest identifies the signed OCI artifact; unpacked runtime content must be separately validated/qualified. The CI build uses package repository snapshots resolved at build time: image digests pin a shipped result but do not yet guarantee byte-for-byte reproducible rebuilds. Fully reproducible builds require a pinned apt repository snapshot and package version lock.

## Tests and acceptance before production

Run offline deterministic tests:

```bash
uv run --locked python -m pytest -q tests/unit/test_workspace_concurrency.py tests/unit/test_shared_workspace_isolation.py
```

On a real Linux/WSL2 worker, **also** test actual bwrap rootfs execution, source commit, seccomp enforcement, prevention of writes outside `/home`, symlink/rename races, fork-bombs, network denial, WSL Windows executable interoperability, DrvFs visibility, failure/termination and resource caps. On Windows, launch under the nonprivileged Potbot WSL distribution (not bare Windows Python). An unsupported kernel or tool fails closed. The shared filesystem/multihost/PR integration tests must run on a real PostgreSQL test system and against a controlled GitHub fixture repository with protected-branch policies. The unit tests do not establish those qualifications.

## References

- [Microsoft WSL security model](https://wsl.dev/technical-documentation/security/)
- [Microsoft WSL distribution settings](https://learn.microsoft.com/windows/wsl/wsl-config)
- [Bubblewrap read-only mounts and namespaces](https://github.com/containers/bubblewrap)
- [GitHub GraphQL `updateRefs` exact beforeOid](https://docs.github.com/en/graphql/reference/git)
- [GitHub GraphQL signed commit with expectedHeadOid](https://docs.github.com/en/graphql/reference/commits)
