# S37 — Agent browser Cogs and durable evidence

**Status:** source-level patch, offline checks only. Base commit: `68d65e25f647652b4b4119495779ea8fb57e4cae`. The separate S36 log mount patch remains compatible; S37 does not relax its operator admission or health monitoring.

## Architecture

```
Authenticated workspace/principal + admitted task
  -> sealed SYN browser Cog (observe, act, pick, capture)
  -> ExecutionCogRuntime / existing CogWorkMonitor / journal
  -> operator's BrowserControlService; fixed Xpra session allowlist
  -> XpraApplicationSupervisor.browser_command
  -> private Chromium worker pipes (Playwright/Patchright, CDP)
  -> private ArtifactStore.put: JSON observation, optional JPEG, manifest
  -> returned artifact IDs and hashes in normal Cog receipt
```

The *only* browser target is an operator-admitted, already running sandboxed Chromium. No arbitrary host process, shell command, JS evaluation, externally provided CDP endpoint, replacement database or new audit ledger is introduced.

## Cogs and semantic types

| Cog ref suffix | SYN input type suffix | Effects | Allowed methods |
|---|---|---|---|
| `browser-observe-v1` | `browser-observe-request-v1` | read-only | state, accessibility, interactive controls, picker poll, inspect at, read-only CDP |
| `browser-act-v1` | `browser-act-request-v1` | non-idempotent | navigate, pointer/keyboard, role click/fill, CDP input |
| `browser-pick-v1` | `browser-pick-request-v1` | non-idempotent | DevTools inspect mode start/stop |
| `browser-capture-v1` | `browser-capture-request-v1` | read-only | screenshot `sample` with image data |

Each input contains `{ "schema_version": 1, "session_id": "<32 hex>", "command": {"op":"..."} }` and is additionally validated by the browser worker allowlist. All return the **same** explicit result type `syn://type/potbot-browser-evidence-result-v1` with digest-verified private artifact references. Large JPEGs never travel in the Cog output or public work telemetry. `allow_model_preview` defaults to **false**, and when an operator deliberately opts in, only bounded structural observations are surfaced.

**Operator configuration, not a model instruction:** after creating the ordinary `controlled-browser` Xpra application, bind `BrowserControlService(actor=authenticated_actor, apps=owned_xpra_supervisor, artifacts=existing_artifact_store, policy=existing_execution_policy, approved_sessions=(session_id,))`. Import and seal `browser_cog_snapshot()` through the existing SYN service, then pass `browser_service=service` to `ExecutionCogRuntime` with the *same* actor, policy and artifact store. The runtime rejects a mismatched scope and a simultaneous artifact-upload profile. Use the normal `BoundCogRegistry.browser().invoke(...)` or `ExecutionCall.child_cog(...)` with `CogRunContext` and independently configured admission rules. Installing source files does not auto-enable any agent Cog.

## Durable evidence

Every successful browser call stores a JSON observation and a separate JSON manifest with workspace, principal, session, invocation, policy, command hashes, time, and artifact UUIDs. An explicit screenshot also stores an `image/jpeg` artifact of at most 600 KiB. The recorder checks base64 decode, JPEG framing, reported size and SHA-256 before publication. All writes go through the **existing** `ArtifactStore` (normal workspace/principal access rules). Source-map, CSS, DOM and ARIA details can be sensitive; only a properly authorized artifact reader should retrieve them. The normal execution journal and event stream contain the causal Cog receipt.

## Indeterminate action protection

Prior worker behavior merged preflight rejection and post-click automation exceptions into the same ordinary failed response. S37 now marks any exception after validating a mutating command as **indeterminate**. The operator's `BrowserControlChannel` fences the session, and `XpraApplicationSupervisor.fence_browser` terminates it and keeps an uncertain receipt. If evidence persistence fails *after* an action, the service also fences the session. Controller cancellation similarly fences rather than retrying an unknown action. Invocation IDs are one-use within a service lifetime. **No click, submit, or navigation is automatically replayed after unknown completion.**

## Acceptance commands

```bash
PYTHONPATH=src python3 -m pytest -q tests/unit/test_browser_cogs_s37.py \
  tests/unit/test_xpra_browser_control.py tests/unit/test_xpra_browser_worker.py \
  tests/unit/test_xpra_application_sessions.py
PYTHONPATH=src python3 -m compileall -q src/potbot
```

These source-level tests do not qualify a host. Live gates still required: operator-only Xpra in qualified Bubblewrap + S36 monitoring, exact browser window/source-picker inspection, accessibility role click, screenshot readback through real PostgreSQL/ArtifactStore, cross-workspace rejection, and deterministic failure injection after a click which proves **zero automatic retries**. A content hash is not proof that a screenshot equals the Xpra's damage stream; pixel equivalence requires a real host fixture. Browser internet access remains network-denied until a separately qualified proxy exists.

Future work: controlled external network proxy, durable operator event gallery with scoped image retrieval, browser-session reservations for concurrent agents, richer DOM/source-map inspection and production-host adversarial qualification.
