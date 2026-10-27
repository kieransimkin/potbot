# MODEL.md Stage 2 — durable context store

Stage 2 adds `context_contract.durable.DurableContextStore` without changing the Stage 1 API. It is a transactional operator-side storage building block, **not** an exposed agent API or a sandbox mount.

## API and invariants

- `read(kind, name, role, actor=...) -> (content, sha256, revision)`; missing documents return revision zero.
- `write(kind, name, role, content, expected_revision, actor=..., reason=...) -> (sha256, revision)`; stale revisions raise `Conflict`.
- `provision(kind, name, actor=...)` creates only missing triplet documents.
- `history(...)` lists immutable revision metadata; `restore(...)` writes a new revision containing an older revision's content.
- Every successful update commits current content and an audit revision in one SQLite transaction. `BEGIN IMMEDIATE` coordinates writers in separate processes.
- `actor` is required but **not authenticated by this library**. The operator must derive actor identity from authenticated sessions and enforce its access policy. The optional authorizer is only an additional guard.

## Integration work remaining

1. Wire an authenticated operator RPC endpoint to the durable store; reject untrusted actor strings and enforce sandbox/object ACLs.
2. Keep the SQLite database outside agent-writable mounts and use a local filesystem with working SQLite locks; configure backup, retention and recovery.
3. Implement multi-document transactions and operator-mediated materialization into `/AGENTS.md`, `/MODEL.md`, `/MEMORY.md` with safe file-descriptor-relative access and no symlink traversal.
4. Add semantic vocabulary types for MODEL.md and MEMORY.md, plus object-level context links.
5. Add content patch/merge operations, conflict UI, notifications, audit export, observability and operator-only provisioning.
6. Integrate `/usr/local/skills` and per-agent context with authorization and lifecycle hooks.

## Run tests

`python -m unittest discover -s tests -p 'test_durable_context.py'`

SQLite transaction durability does not make this an authoritative policy engine, distributed database, or safe filesystem sandbox boundary.
