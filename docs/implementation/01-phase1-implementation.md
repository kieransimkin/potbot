# Universal Tool Platform — T01 implementation increment

**Target repository:** [`kieransimkin/potbot`](https://github.com/kieransimkin/potbot)
**Inspected `main` baseline:** `fcede56c0390fa478acc79bf416468508fc0afc1` (2026-10-22, `phase 7`)
**Programme:** [existing tool master plan](../potbot-tool-mcp-implementation-bundle/potbot-universal-tool-mcp-implementation-plan.md) — 48 incremental stages.
**Delivery:** additive `git apply` patch, rebased in documentation against the latest tool-plan commit. No existing source is removed or overwritten.
**Qualification:** new offline tests passed in a minimal local compatibility harness; full repository CI, PostgreSQL, native custody and production sandbox are **not qualified by that result**.

## Existing integration points, not new competing systems

The reviewed repository contains controller-owned `ToolDefinition`, `ToolRegistry`, `ActionProposal` and `ToolGateway` in `domain/execution.py` and `tools/gateway.py`. It also has durable controller jobs, WebSocket/REST APIs, per-workspace sandbox roots, Linux Bubblewrap/seccomp isolation, immutable receipts, an authenticated credential broker, inherited SYN Ember contracts, installed single-input/single-output Cogs, semantic hierarchy types for `tool-definition` and `mcp-tool-definition`, and preliminary Phase 6/7 investigation and benchmarking.

The Phase 7 research Cog is marked **pending semantic registry import**, with live external access requiring qualified egress policy. Do not authorize it by deserializing public metadata. Synthetic Phase 6 programming fixtures do not establish the success rate of any live LLM.

## Files delivered

| Stage | File | Implementation |
|---|---|---|
| T01.1 | `scripts/tool_platform_inventory.py` | Deterministic, read-only source-anchor scan with explicit ownership/decisions, SHA-256, commit ID and collision warnings. |
| T01.1 | `tests/unit/test_tool_platform_inventory.py` | Fingerprint change, missing anchors, unexpected adapters, symlink rejection and source integrity. |
| T01.2 | `src/potbot/tools/capability_contracts.py` | Frozen, versioned, explicitly typed public capability, implementation and installation descriptors. Uses the existing SYN parser and selected hierarchy. |
| T01.2 | `tests/unit/test_tool_capability_contracts.py` | Strict schemas, Type ancestry, aliases, malformed and unregistered Types, oversized payloads and immutable round-trips. |
| T01.3 | `src/potbot/tools/capability_contracts.py` | Controller-constructed invocation *intent* and linked result observations. Content hashes and workspace/policy bindings; no execution permission conferred. |
| T01.3 | `tests/unit/test_tool_invocation_provenance.py` | Workspaces, policies, budget tampering, indeterminate results, input/output linkage and strict serialization. |
| T01.4 | `scripts/tool_phase1_gate.py` | Explicit offline-vs-qualified stage gate. Requires existing `scripts/check.py --with-postgres --with-sandbox` for qualification; emits XML and JSON artifacts. |
| T01.4 | `tests/unit/test_tool_phase1_gate.py` | Offline success never marked qualified, missing/full-check failure cannot qualify, skipped test checks fail. |
| Programme | `docs/potbot-tool-mcp-implementation-bundle/` | Existing 48-stage plan on main, left unchanged by this patch. |

## Security and semantic constraints

- Existing `syn://`/`synny://` parsing and selected hierarchy are the only semantic authority. This increment does not fork the ontology or install new executables.
- Public capability discovery, type validation, implementation metadata, workspace installation and execution authorization remain separate operations. The immutable invocation intent **cannot dispatch a tool**.
- `ToolCapabilityDescriptor` stores canonical immutable JSON *strings* rather than mutable schema dictionaries. Its decoded properties return separate copies.
- No raw secrets, credentials, outputs or tokens are captured in these public records. A configuration digest is not a credential.
- An observation with `succeeded` is a reported outcome only. It cannot mark semantic truth, signed authorization or verified external effects.
- Every invocation is pinned to an installation fingerprint, workspace UUID, task revision, policy revision, input Type/hash, side-effect category and resource ceilings.
- `indeterminate` observations cannot pretend to contain a finished output. Non-idempotent retries require controller reconciliation in later phases.
- No MCP child process, browser or network call is launched by these contracts. Future transport handlers **must** call the existing gateway under a qualified sandbox/egress policy.

## Local demonstration after applying the patch

Run from the Potbot repository root with its locked environment already installed:

```bash
uv run --locked python scripts/tool_platform_inventory.py --assert-required --output artifacts/tool-platform-phase1/source-inventory.json
uv run --locked python -m pytest -q \
  tests/unit/test_tool_platform_inventory.py \
  tests/unit/test_tool_capability_contracts.py \
  tests/unit/test_tool_invocation_provenance.py \
  tests/unit/test_tool_phase1_gate.py
uv run --locked python scripts/tool_phase1_gate.py --mode offline
```

For full phase qualification **on a host with real PostgreSQL and qualified Linux process isolation**, first provision a disposable test database following `docs/phase-2.md` and `docs/linux-sandbox-ci.md`, and then run:

```bash
uv run --locked python scripts/tool_phase1_gate.py --mode qualified
```

The full gate invokes Potbot's `scripts/check.py --with-postgres --with-sandbox`, including Ruff, formatting, mypy, existing unit/integration suites and wheel-install checks. It writes `artifacts/tool-platform-phase1/phase1-gate.json` with `fully_qualified=false` unless all required full checks pass. A returned offline exit code 0 is never interpreted as production qualification.

## Existing HEAD blocker: README removed

The `fcede56c` tool-plan commit deletes the project-root `README.md`, while `pyproject.toml` still declares `readme = "README.md"`. This is a pre-existing build blocker, not part of this patch's new subsystem. **Restore the previous README from version control before running the clean wheel gate:**

```bash
git restore --source=bbd2ec78165bac7f402effa520d410ea222ef064 -- README.md
```

Review and commit this restoration separately, or reconcile with whatever current `main` publishes. The T01.1 source inventory will explicitly report the missing README until restored; the Phase 1 gate cannot mark the source inventory green while it is absent.

## Remaining work before declaring T01 complete

1. Apply the patch on the latest checkout. If `main` moved after the recorded SHA, re-run the source inventory and review new implementation collisions.
2. Run Ruff and mypy using the repository's pinned `uv.lock`. Fix any style or type issues in the new files without weakening existing gates.
3. Execute real PostgreSQL and Bubblewrap suites in GitHub Actions. Pin report hashes to the commit and archive artifacts.
4. Bind capability installation and invocation descriptions to the existing protected semantic-release mechanism and inherited Ember contract revision (rather than importing descriptions directly as an executable installation).
5. Add the Agent-signed authorization receipt and durable invocation-result linkage at the controller boundary. The current plain SHA-256 hashes are not signatures or attestations.
6. Only after these gates pass, proceed to T02.1 MCP stdio client using the official versioned SDK and a qualified process launcher.

**No live provider, network egress, tool execution or new database migration is claimed by this increment.**
