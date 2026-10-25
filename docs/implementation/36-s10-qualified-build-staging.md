# S10 — Approved sandbox builds and independent staged-test admission

**Baseline:** `kieransimkin/potbot` S9, Git commit `4899ec6f8b1770098c80aa83eae41e9fb6270864` (and inspected subsequent `428d397a`).

This increment **does not alter a live shared-tool checkout while compiling**. The operator starts a pinned executable inside the existing S7-monitored Bubblewrap namespace, with the selected enrolled agent's `/home/<agent>/...` as its working directory. The guest sees `/usr/local/tools` read-only; it may populate an operator-preprovisioned build tree below its home. A separate host-side data mover copies that completed build into the private S9 candidate slot, then invokes the existing S9 suite and clean probe.

## Admission flow

1. The host provisions an *existing* source checkout in `/home/<agent>/builds/<operation>` using the audited Git tool. Its source, checked-out commit and dependency caches must be pinned. The tree must contain `checkout/` (including a real `.git`), `environment/`, `static/`, and `downloads/`. Source preparation/download policy remains external to this increment; arbitrary network cloning is not allowed inside the build.
2. An operator reviews an exact build argument array and stores a private JSON S10 build plan (mode 0600). The SHA-256 is computed over canonical JSON of `argv`, including the trailing newline; the new `command_digest()` method is authoritative. Command, source pin, agent/workspace, operation UUID, and deadline are immutable for this attempt.
3. **Quiesce other operator processes**, then run `scripts/tool_pipeline_s10.py` with an S8/S9 operator manifest, agent ID, S9 replacement test plan, existing build root, private S10 build plan, and a controller-only 0700 evidence directory. One tool write lease fences readers and writers during build + transfer. The same S7 watcher cancels the build if `/etc/ai` history becomes unhealthy.
4. Admission requires confirmed dispatch, process-tree termination, zero exit code, `completed` outcome, and an isolation identity. No host fallback. The operator copies files through fd-relative `O_NOFOLLOW` operations, rejects symlinks/hardlinks/devices/setuid and bounded traversal violations, and fsyncs the complete stage. Incomplete copy residue is left quarantined; no silent retry.
5. Final placement is `.potbot-s9-stage-<slug>-<uuidhex>`. A private, immutable-once S10 receipt records the tool, command digest, sandbox identity and transfer tree hash. A missing receipt after a crash must be reconciled independently. **No shared-tool registration or activation occurs at this step.**
6. The S9 `SharedToolUpgradeManager.prepare()` now reads the exact staged Git HEAD/origin, lockfile hashes, *all* reusable artifacts, runs smoke + unit (+ optional integration/regression) via the sandbox, checks cleanup and records a durable S9 test receipt. Only that S9 record can be published for approval in S11.

### Run from a real qualified Linux/WSL2 operator host

```bash
# Exact, operator-owned files; the example does not invent build arguments.
PYTHONPATH=src python scripts/tool_pipeline_s10.py \
  --manifest /srv/potbot/controller/manifest.json \
  --workspace-name danceflow --agent-id AGENT_UUID --operation-id OP_UUID \
  --build-root /srv/potbot/workspaces/NAME/home/AGENT/builds/OP_UUID \
  --evidence-dir /srv/potbot/controller/s10-receipts \
  --build-plan /srv/potbot/controller/s10-build-plan.json \
  --upgrade-plan /srv/potbot/controller/s9-upgrade-plan.json
```

The S10 plan shape is `{ "schema":"potbot-s10-build-plan/v1", "argv":[...], "sha256":"64 lowercase hex", "timeoutMs":600000 }`. To compute the correct digest, use `PYTHONPATH=src python -c 'from potbot.tools.tool_stage_s10 import command_digest; print(command_digest(("/usr/bin/python3", "--version")))'` for that specific operator-reviewed argv. A valid digest is not approval by itself; file ownership, image pin, original Git source and S9 plan gates still apply.

## Boundaries and required host qualification

- **Offline tested:** path and command-policy validation, actual fd-relative data copy, binary artifacts, symlinks/hardlinks, duplicate stage rejection and a real Git checkout roundtrip.
- **Qualified by existing S9 logic on a real host only:** smoke/unit/integration command execution, exact checkout/lock verification and cleanup after build; the unit suite uses test doubles for supervisor responses.
- **Not automatically qualified:** real WSL2 subuid/idmapped per-agent DAC, unsandboxed host races against the shared physical storage, building arbitrary third-party installers, dependency-license verification, signature attestation, power-loss persistence under all filesystems, cross-machine leases and network access. Reject or quarantine without evidence.
- The S10 stage cannot be promoted through the HTTP API. An S11 browser approval cannot bypass the S9 or S10 operator-side checks.

## Unit gate

```bash
uv run --locked python -m pytest -q tests/unit/test_tool_stage_s10.py
```
