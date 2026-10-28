# MODEL.md Stage 6–7 — agent context, mediated writes, skills, shared ACLs

## What this patch activates

The previous stages provided workspace-scoped PostgreSQL context journals and a
read-only `/AGENTS.md`, `/MODEL.md`, `/MEMORY.md` projection. This increment adds:

- PostgreSQL migration **30**: per-document write grants for shared objects, with
  a restrictive RLS policy that forbids restricted application logins from
  self-granting privileges. Workspace members retain read access to shared
  objects. Their private objects remain editable by their own principal.
- Admin-only `seed_shared_context()` for all registered primitive kinds, such as
  tools, skills, operators, models, connectors, workers and workflows. It creates
  each object with the three documents and an immutable initial revision/event.
- Admin-only `grant_shared_context_writer()` and
  `revoke_shared_context_writer()`; grants require an existing shared object,
  document and workspace membership. Do not expose the admin connection to an
  agent, skill, tool, or guest.
- `AgentContextLifecycle`: reconcile an agent's persistent home and PostgreSQL
  context object keyed by immutable `agent_id`. Creates read-only empty targets
  in the home through an operator-held workspace lease. Existing nonempty or
  linked targets fail closed. Documents are never imported from a writable home.
- `ContextMutationBroker`: host-only context update entrypoint. The authenticated
  host supplies `ContextIdentity`. The broker registers an existing agent,
  requires a pinned image and an exclusive named lease, restricts the agent to
  its own context or the workspace's MODEL/MEMORY documents, and uses PostgreSQL
  CAS, revision history and idempotency. Shared document edits also require
  the explicit PostgreSQL writer grant. It does not grant guest SQL access.
- `ContextProjectionManager.capture_object()` and `AgentContextProjection`:
  snapshots of agent/document triples can be bound read-only into each
  `/home/<registered-directory>/` (the host file placeholders are never
  authoritative). `--project-agent-context` opts an upgraded operator into
  capturing all currently registered homes at startup.
- `SharedSkillReleaseStore`: create-only content-addressed releases under the
  operator-controlled shared skills source. Each release contains `SKILL.md`,
  an initial context triple and a release manifest. The caller must vet the
  skill's instructions and pin a release before activation. `SKILL.md` is not
  promoted above operator or host security policy.

## Upgrading safely

1. Apply the patch on top of merged Stage 5; run `potbot admin migrate` with
   the **migration-owner** database account. Never run migrations using a guest
   principal or grant schema ownership to an application role.
2. After migration 30, previously permitted shared-document updates now require
   grants. The host admin may use `admin_connection(...)` with
   `grant_shared_context_writer()` to grant the operator's restricted login
   the exact MODEL/MEMORY roles it should update. AGENTS.md grants should be
   exceptional and explicitly reviewed. Apply revocations from the same admin
   boundary. Private document updates continue to work as before.
3. Use `AgentContextLifecycle.provision(ContextIdentity(...), idempotency_key=... )`
   in the trusted agent-home provisioning path **before** starting the operator
   with `--project-agent-context`. This reconciles existing homes and creates
   their per-agent context in PostgreSQL under the operator's authenticated
   principal. The reconciliation step should be repeated after unexpected
   termination; it never replaces existing document history.
4. Rebuild and qualify the Stage 5 sandbox image (which has root placeholders),
   then start the host operator with `--context-principal-id UUID`,
   `--context-parent /run/potbot/context-snapshots` and optionally
   `--project-agent-context` to enable the additional agent-file mounts.
5. Provision a host-owned (not guest-writable) skill source. `SharedSkillReleaseStore`
   creates immutable version paths at `<skill>/releases/<sha>/`. The operator
   must select/pin a reviewed version before the sandbox loads it. The skill
   release's embedded context triple is a creation-time snapshot; PostgreSQL
   remains authoritative for live context and audit history.

## Outstanding qualification and deployment boundaries

This patch is not proof of live Linux/WSL2 isolation or of PostgreSQL execution.
The operator's existing workspace lease ledger is host-private and uses SQLite
for short-lived execution admission, **not** for context documents; PostgreSQL
is the only authoritative context backend. Distributed multi-host worker leases
remain future work. Operator snapshot refresh currently requires a safe restart;
registered agents created after startup require another restart to acquire
file mounts. Full integration of context provisioning into every Potbot
object's existing constructor/installer needs per-object lifecycle hooks.

RLS is the database backstop, not a replacement for trusted service endpoints.
Do not expose an arbitrary `WorkspaceContextService` method to the guest. The
mutating bridge in this patch is a host-side building block; authenticated
transport, agent credential verification, replay policy, audited activation,
semantic Syn classification of MODEL/MEMORY, and a production UI all require
additional qualification. Agents must never receive a direct database DSN.

## Tests

```
uv run pytest -q tests/test_context_projection.py tests/test_context_agent_skills.py
# disposable PostgreSQL test database only:
uv run --extra postgres pytest -q tests/integration/test_workspace_context.py tests/integration/test_context_acl.py
```

The PostgreSQL integration fixture requires the explicitly configured dedicated
`POTBOT_TEST_DSN` and a local disposable test database with pgvector installed.
Never point those fixtures at a production database.
