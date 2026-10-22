# Cog-based, explicitly typed transformations

## Architectural rule

A **Cog** is a concrete computation-capable SYN object that transforms a value
of **one registered input Type** into a value of **one registered output Type**.
It is not an untyped callback, a model instruction, an embedding-neighbour
relationship, or a blanket permission to execute tools. A Cog may be idle;
only an explicitly selected, installed, authorized instance is executable.

Each installed Cog definition has the following required instance fields,
represented as public, typed declarations (and intended to be inherited as
required Embers on its Cog Type): `inputTypeRef`, `outputTypeRef` and
`implementationBindingHash`. A versioned handler reference and revision
complete its executable binding. Concrete Types extend `syn://type/cog`.

All Type identifiers use full `syn://type/...` or `synny://type/...` URLs.
Cog *instances* use `syn://cog/...`. The two Type URL schemes can express
one Type identity in a pinned catalog; legacy shorthand notation is not
accepted by the Cog layer. Each Cog instance has exactly one registered
concrete Type; input and output are registered concrete data Types.

## Two formerly separate model paths

```
Retained completed provider text
  (syn://type/potbot-provider-completion-text)
       |  syn://cog/potbot-completion-json-v1
       v
Bounded, strict JSON document
  (synny://type/potbot-data-document-json)
       |  syn://cog/potbot-json-model-coding-v1
       v
Schema-checked but untrusted coding response
  (syn://type/potbot-model-coding-response)
       |  syn://cog/potbot-scope-check-coding-v1
       v
Scope-checked coding response
  (syn://type/potbot-scope-checked-coding-response)
       |  EXISTING controller's invocation, contract, settled-budget,
       |  patch, sandbox and test verification gates (NOT a Cog shortcut)
       v
Verified candidate patch
```

The same strict JSON Type is the input to the separate
`syn://cog/potbot-json-model-action-v1`, which produces
`syn://type/potbot-model-action-frame`. The receiving controller must still
check the action inventory, facts, identity echoes, evidence, scope and tool
capabilities before scheduling an action. A Type label alone never makes the
model output authoritative.

The Cog registry can **plan** an exact Type-to-Type path using its installed
Cog instances. It never invents converters from compatible-looking strings,
Type ancestry, vector similarity, model prose or untrusted supplied code.
Unknown routes fail; equally short alternatives require an explicit choice.
A controller-scoped Cog requires `CodingScope` loaded by trusted controller
code, not produced by a model or read from a public HTTP request.

## SYN import and pinned implementation

The complete public snapshot is included at:

`vocab/potbot-cog-transforms.snapshot.json`

It declares **nine concrete Types** (including the transformation-Cog Type
hierarchy and response data Types) and **four concrete Cog instances**. The
Types and instance Embers can be imported through the existing
`SynService.import_snapshot` for a specific repository, then pinned to an
immutable release. The import operation does **not** run code, grant execution
permissions, or promote any model output. It is a data declaration.

The running adapter matches the complete pinned snapshot against the known
installed definitions and handler names. An unrecognized or modified Cog,
changed input/output Type, unpinned handler binding, or wrong scope fails
closed. The `implementationBindingHash` is a canonical **binding digest** of
signature and handler revision. It is *not* a cryptographic attestation of
transitive Python source or dependency versions. Such code attestation must be
checked independently before granting a production Cog capability.

Each successful step returns a Cog receipt with the exact Cog and concrete
Type identities, pinned snapshot hash, original model invocation and task/base
identities, input/output value digests, optional input-artifact identity,
controller scope hash where applicable, and prior receipt hash. These are
**transformation observations**, not assertions that a downstream action was
authorized, paid for, executed, or verified. Completion status, provider
usage settlement and original artifact integrity remain owned by the
existing controller/database boundary.

### Example

```python
from potbot.cogs import BoundCogRegistry, CogRunContext, TypedCogValue
from potbot.cogs.builtin import COMPLETION_TEXT, MODEL_ACTION_FRAME

registry = BoundCogRegistry.builtins()
context = CogRunContext(
    cog_snapshot_hash=registry.snapshot.fingerprint,
    task_revision_id=task_contract_id,
    repository_state_id=repository_state_id,
    invocation_id=verified_completed_invocation_id,
)
result = registry.convert(
    TypedCogValue(COMPLETION_TEXT, retained_completed_text, source_artifact_id),
    MODEL_ACTION_FRAME,
    context,
)
# result[-1].value is a typed, schema-checked action frame.
# result[-1].receipt is traceable to every prior Cog in the chain.
# NO tool is executed by these transformations.
```

The caller must establish from trusted evidence that `retained_completed_text`
is the complete output of the stated invocation and that the source artifact
matches the declared bytes. This pure Cog module does not make database claims.

## Phase 6 graph/persistence integration

The controller's durable graph should treat a Cog transformation as a typed
edge **from input value instance to output value instance**, carrying the
Cog reference, binding digest and receipt reference. Branch knowledge views
pin the Cog/SYN snapshot alongside the task and repository state. Cog
planning can answer **which transformations are required** to connect data
sources and consumers; the action planner then checks permissions and budgets
before any potentially mutating Cog is scheduled.

Next implementation steps for the ongoing graph migration:

1. Register a sealed Cog snapshot under the intended workspace and repository.
2. Retain the completed, redacted, origin-verifiable provider output as an
   immutable artifact *before* running a parsing Cog.
3. Append transformation receipts and output-artifact references to the
   evidence ledger using the existing idempotence and outbox mechanisms.
4. Bind a Cog receipt to its graph edge, with scope and generation fencing.
5. Reuse the existing `ModelCodingService.record` / patch-verification gate
   for final admission; do not bypass invocation, budget or task checks.
6. Add controlled Cog-discovery results to the reuse/adapt/build assessment.

This first delivery installs the **pure typed transformation kernel and import
snapshot**. The durable graph writer, hosted adapter call-site integration and
real PostgreSQL integration remain separately testable work. The local unit
suite exercises both coding and action routes but does not claim a live
provider qualification or a production Cog execution service.

Cog Types in unrelated domains—such as Ed25519 signing and verification—must
still declare specific input and output Types, inherit required Ember
contracts, and use the appropriate credential/approval policies. Their
private signing handles must never appear in public Cog objects or receipts.
