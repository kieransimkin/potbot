# S11 — Durable tested-tool approval UI, host worker and runtime repin

**Baseline:** S9 + S10; registered, operator-owned native credential vault from S8 and S7 `/etc/ai` history watch. Migration **28** is additive: two workspace-RLS tables (`ui_tool_upgrade_approvals`, `ui_tool_upgrade_audit`), a single active approval, CAS revisions, state fencing, and append-only audit evidence. Original migration checksums are unchanged.

## Non-secret public workflow

| State | Trigger | Effect |
| --- | --- | --- |
| awaiting_approval | `scripts/tool_approval_s11.py publish` reads real S9 `inspect` in state `ready` | Immutable source/test hashes, safe test names, categories and elapsed milliseconds published; no shell commands, paths or secrets |
| approved / rejected | Operator frontend `/setup/tools` via Next BFF, workspace bearer + **separate setup capability**, exact Origin + revision/evidence CAS | A confirmed human decision only; **no physical activation** |
| executing | Separate host `run-once` worker claims one agent-owned approved operation (PostgreSQL `FOR UPDATE SKIP LOCKED`) | Produces a worker-private token and fences concurrent claims |
| awaiting_repin | Worker revalidates S9 journal against signed approval, then S9 atomically exchanges the tested installation | Prior binaries are no longer admitted until independent new executable SHA-256 pins are installed |
| active | Host `confirm-pins` independently verifies the S9 active record and exact pins from an operator-reloaded manifest | Safe to expose the new executable under the existing dispatcher contracts |
| indeterminate | Process crash, failed/unknown outcome, evidence mismatch or missing postflight | Never automatically retried or promoted; manual host reconciliation required |

A malicious workspace SQL client cannot simply set `state='approved'` and get code executed. The backend creates a versioned **HMAC-SHA-256 approval MAC** keyed by the existing native-vault `api.setup_token`, bound to workspace, operation, agent, slug, old/new commits, test evidence, suite, approver and revision. The host worker verifies that MAC using its **own native-vault readback**, not any value from PostgreSQL or the browser. Token rotation intentionally invalidates outstanding approvals until the operator reauthorizes them.

### API / frontend

- `GET /v1/ui/tools/upgrades` and `GET /v1/ui/tools/upgrades/{uuid}` — workspace-scoped safe projections, requiring the ordinary workspace bearer.
- `POST /v1/ui/tools/upgrades/{uuid}/approve` or `/reject` — also needs exact approved browser Origin, the server-only `X-Potbot-Setup-Token`, `{ "revision":0, "evidenceSha256":"...", "confirm":true }` and a still-unmodified S9 test record. A browser **cannot** publish tests, approve arbitrary shell argv, change tool versions or activate the installation.
- Next.js `/api/ui/tools/upgrades` and `/api/ui/tools/upgrades/{id}/{action}` forward through the existing loopback-scoped server backend. The setup token is inserted by Next server code only, never hydrated into JavaScript. Invalid backend fields and private metadata are discarded by the frontend projector.
- `/setup/tools` displays test names, categories, measured run times, hashes, current phase and explicit approval/rejection. The Potbot control menu and preferences link to it.

**Provider key management remains S8 `/setup/apis`; S11 never exposes vault values, tool stdin, checkout paths, raw test logs, process commands or developer credentials.**

### Operator use (maintenance mode)

Run migration 28 with Potbot's ordinary admin migration tooling first. Use the same `POTBOT_CREDENTIAL_PROFILE` for the API process and host worker. Only the **operator** has the manifest/ledger and can publish a tested record or execute approved operations.

```bash
PYTHONPATH=src python scripts/tool_approval_s11.py \
  --manifest /srv/potbot/controller/manifest.json \
  --workspace-name danceflow --principal-id PRINCIPAL_UUID \
  --agent-id AGENT_UUID --operation-id OP_UUID \
  --proposal-dir /srv/potbot/controller/pin-proposals publish

# After operator approves in /setup/tools, quiesce long-lived watcher and worker:
PYTHONPATH=src python scripts/tool_approval_s11.py \
  --manifest /srv/potbot/controller/manifest.json \
  --workspace-name danceflow --principal-id PRINCIPAL_UUID \
  --agent-id AGENT_UUID --proposal-dir /srv/potbot/controller/pin-proposals run-once

# After independently verifying/applying the proposed executable pins and
# restarting the operator using the new approved manifest:
PYTHONPATH=src python scripts/tool_approval_s11.py \
  --manifest /srv/potbot/controller/manifest.json \
  --workspace-name danceflow --principal-id PRINCIPAL_UUID \
  --agent-id AGENT_UUID --operation-id OP_UUID \
  --proposal-dir /srv/potbot/controller/pin-proposals confirm-pins
```

Both the normal operator and these maintenance commands own a **singleton S7 inotify watcher**. They cannot run concurrently. `run-once` performs **one** approved operation, not an unattended loop. A crash leaves the PostgreSQL record `executing` and requires manual operator investigation. The signature is an authorization check, not a substitute for the filesystem lease, approved source/lockfile hashes, S9 evidence, or secure runtime pinning.

## Qualification

- Offline: real SQLite SQL-shape fixture for state/CAS transitions and MAC verification, FastAPI admission/auth tests, host worker receipt and failure tests, Node projectors and route bridge contract stand-ins.
- Live PostgreSQL RLS/migration-28 acceptance, real native-vault roundtrip, full Next.js build, WSL2/bubblewrap staging + activate + repin, and multi-process crash recovery remain **not independently qualified** until run in Potbot CI and a trusted operator host.

```bash
uv run --locked python -m pytest -q \
  tests/unit/test_ui_tool_upgrades_s11.py \
  tests/unit/test_tool_upgrade_approval_store_s11.py \
  tests/unit/test_tool_approval_worker_s11.py
cd frontend && npm test && npm run typecheck && npm run build
```
