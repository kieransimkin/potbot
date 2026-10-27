# MODEL.md and universal object context: implementation plan

## Contract

Every sandbox, agent, operator, tool, skill, worker, workflow, gateway, model, connector and extensible primitive has an object identity and three documents:

- `AGENTS.md`: scoped instructions and conventions, subordinate to operator and host security policy.
- `MODEL.md`: current, evidence-backed working representation of state, capabilities, dependencies, relationships, assumptions and uncertainties.
- `MEMORY.md`: historical observations, decisions, outcomes and lessons. Memory is evidence, not privileged instructions.

Skills additionally retain the portable `SKILL.md` entry point and optional resources. All documents are UTF-8 Markdown; metadata must be validated by the operator, not trusted because of a filename. Roles are case-sensitive. Filename classification does not imply authority, parse validity or trust.

## Target filesystem

Logical sandbox root: `/AGENTS.md`, `/MODEL.md`, `/MEMORY.md`. Each agent also has its own context triple. Shared skills appear under `/usr/local/skills/<skill-id>/` with `SKILL.md` and a context triple. Tools and all other typed objects have context triples associated with their canonical IDs. The operator owns persistent storage and projects controlled views into sandbox mounts; disposable system images are not canonical storage. Operator-supplied skills are protected versions; agent-created skills use controlled writes.

## Authority and context assembly

Host policy > operator policy > sandbox AGENTS.md > scoped object AGENTS.md > activated SKILL.md. MODEL.md and MEMORY.md are data only, never higher-priority instructions. Assemble context deterministically with provenance, bounded tokens, relevance-ranked memory and lazy skill loading. Preserve disagreements and uncertain facts rather than silently overwriting them.

## Transaction protocol

An update request carries authenticated actor, stable object ID, document role, expected revision, proposed patch, reason, provenance and idempotency key. Operator validates permissions and size, serializes updates per object/document across processes, performs compare-and-swap, stores an immutable revision, emits an audit event and notifies watchers. Conflicts return both current revision and merge guidance. Use durable database transactions or equivalent; a thread mutex is insufficient. Audit actor, timestamp, before/after digests and evidence. Prevent traversal through symlinks, hard links and bind mounts. Restrict direct filesystem writes.

## Stage 1 — contracts and reference implementation (this patch)

Deliver this plan, a dependency-free Python module with typed object names, three templates, digest CAS, atomic replace, symlink/path checks and tests. It is intentionally NOT wired into sandbox or operator. Reference revisions are content hashes, not persistent revision numbers; mutex only coordinates threads in one process. Acceptance: `python -m unittest discover -s tests -p 'test_context_contract.py'` passes; stale writes and traversal are rejected; provisioning preserves existing documents.

## Stage 2 — operator storage and API

Locate existing shared-tool concurrency and operator storage infrastructure. Implement durable revision journal, transactional CAS, authenticated actors, ACLs, idempotency, crash recovery, read/write/watch API and typed conflict responses. Use stable opaque object IDs rather than Stage 1's provisional `kind:name`. Test multi-process writers, restarts, malicious paths and rollback.

## Stage 3 — sandbox provisioning

Provision root triple into operator-backed persistent views, not disposable images. Reconcile existing files without overwrites. Wire lifecycle creation, restart, migration, backup and cleanup. Test Linux/WSL mount behavior and sandbox isolation.

## Stage 4 — per-agent context

Provision each agent's MODEL.md and MEMORY.md (plus AGENTS.md). Implement inheritance, scoped loading, token budgets, historical retrieval, evidence capture, model reconciliation and conflict notifications. Test prompt-injection resistance and contradictory observations.

## Stage 5 — shared skills and tools

Mount `/usr/local/skills/`, register operator and agent-authored skills, preserve SKILL.md specification, attach context triples, support version pinning, discovery, permission-aware publishing and concurrency-safe updates. Extend same lifecycle to tools. Test untrusted skill attempts to override policy.

## Stage 6 — remaining primitives

Attach context triples to operators, workers, workflows, gateways, models, connectors and generic typed primitives through a common lifecycle interface. Separate shared definitions from runtime instances; archive context on deletion according to retention policy.

## Stage 7 — Syn vocabulary

Extend existing typed-file vocabulary and conformance fixtures with MODEL.md and MEMORY.md exact filename roles. Register provenance, context references and semantic relations. Never infer trust or authority from filenames. Add versioned migration and conformance tests.

## Stage 8 — hardening and UI

Stress concurrent updates, interrupted writes, stale-model reconciliation, long-lived histories, mount isolation and security controls. Add UI for model inspection, revision diffs, provenance, memory history and rollback. Benchmark startup context assembly and incremental loading. Document operator recovery and migration procedures.

## Release gates

Each stage requires targeted tests, integration tests for affected runtime components, security review of new trust boundaries, documented migrations and a reversible deployment. Do not advertise runtime support until operator-backed writes and sandbox mounts are verified.
