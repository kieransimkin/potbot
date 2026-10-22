# Potbot hierarchy reference

This reference accompanies the [hierarchy and primitive-placement guide](type-hierarchy-and-syn-primitives.md)
for `urn:potbot:semantic-placement:0.1.0`. The authoritative declarations and advisory
cards are in [`hierarchy_profile.json`](../src/potbot/semantics/hierarchy_profile.json).
The [types-only snapshot](../vocab/potbot-hierarchy.snapshot.json) is the existing
import format; it does not contain instance objects or an APS payload validator.

## Catalog boundaries

| Layer | Classes | Source |
| --- | ---: | --- |
| Native SYN | 529 | Existing catalog reconciled to the supplied PostgreSQL dump. |
| Protected legacy Potbot representations | 47 | Existing `potbot-data-` identities and exact crosswalk. |
| Preserved APS 0.2.0 authored declarations | 80 | Exact original extension `SynType` dictionaries. |
| New Potbot placement declarations | 34 | Explicit process, occurrence, question, record, assessment, and definition specializations. |
| Full effective hierarchy | **690** | One identity per scoped type slug. |

The export contains 114 declarations under `extend_builtin`, with `objects: []`.
The profile has its own `profile_hash`; the composed graph has a `catalog_hash`.
Neither changes the original APS 0.2.0 payload schemas or their bindings.

## Reading the tables

Full Type URLs are public identities. The `syn://type/` and `synny://type/` spellings
of one slug identify the same class in the selected catalog. Authored declarations
prefer `synny://`; effective lookup currently normalizes new nonprotected slugs to
`syn://`. The table must count those aliases once. Different slugs remain different
identities; migration requires an explicit mapping.

Direct parents describe the actual declared graph, including multiple inheritance.
Ancestry traversal includes the selected class itself and each reachable class once.
A representation-to-referent annotation such as `defines`, `records`, or `represents`
is not a parent edge.

A recommendation names a **native primitive kind**, not a semantic parent, validator,
or authorization. An absent recommendation means the profile has not chosen one.
**Profile abstraction** is the local policy for whether a placement card selects a
concrete default. The 80 APS classes retain the exact abstraction flags in their
source metadata. For native classes, this policy does not assert formal native
abstractness: the source Type declarations have no such field. Existing native
records may use a different kind from the recommendation, and the importer preserves
that kind.

## Inspect and select

```bash
uv run --locked potbot syn hierarchy --summary
uv run --locked potbot syn hierarchy --limit 1000
uv run --locked potbot syn hierarchy \
  --type-ref synny://type/agent-record --include-subtypes --limit 1000
uv run --locked potbot syn hierarchy --primitive-kind pondle
uv run --locked potbot syn placement \
  --type-ref synny://type/verification-assessment --check-kind pondle
```

The offline inspector defaults to 100 rows, accepts `--offset` and `--limit` up to
1,000, and reports `selected_count`, `returned_count`, `has_more`, and `next_offset`.
Keep the reported `profile_hash` and `catalog_hash` fixed across collected pages.
Its primitive filter selects explicit placement recommendations. Instance
`syn list/search --primitive-kind` instead selects the native kind actually stored
in the explicitly selected imported snapshot. Both operations deduplicate identities.

## New concepts by role

These concise slugs all have preferred authored references under `synny://type/`.
Consult each declaration for its exact direct parents and each placement card for
the recommendation and rationale.

| Role | New class slugs |
| --- | --- |
| Computational processes | `agent-process`, `workflow-process`, `inference-process`, `tool-process`, `retrieval-process`, `embedding-process`, `verification-process` |
| Occurrences | `agent-execution`, `workflow-execution`, `inference-execution`, `verification-execution`, `tool-invocation`, `memory-change` |
| Questions | `information-need`, `clarification-question`, `diagnostic-question`, `verification-question` |
| Information records | `operation-outcome`, `execution-trace`, `retrieval-result`, `claim-record`, `decision-record`, `memory-state-record`, `preference-record`, `resource-budget-record`, `evidence-manifest`, `relation-binding-record` |
| Objective assessment records | `verification-assessment`, `benchmark-assessment`, `retrieval-assessment`, `policy-assessment` |
| Definition records | `evaluation-criterion-definition`, `embedding-space-definition`, `retrieval-profile-definition` |

The existing 80 APS classes already cover the representation, file/document/value,
agent, workflow, skill, tool-interface, specification, schema, registry, manifest,
and type-definition branches. Their exact definitions and parents remain in the
profile. Role and workflow variants remain explicit; composite facets do not imply
membership in every single-role subclass.

## Reviewed native reuse

The profile's [`reuse_decisions`](../src/potbot/semantics/hierarchy_profile.json)
cover **51 reviewed native classes** without changing their definitions or parents.
Each decision explains whether a particular primitive is appropriate or the class
is too broad to select a concrete default. For example, the native
`information-object` and `record` classes span several possible record kinds, so
their local placement policy is abstract and recommends none. This does not add
an abstractness declaration to the native ontology.

The reviewed concepts include software agents, programs, workflows, API interfaces,
executions, observations, evidence, questions, evaluations, policies, preferences,
quantities, and the native primitive roots. The profile does not infer placement
recommendations for the other native classes. All 529 native classes and the 47
protected legacy classes remain inspectable through `syn hierarchy`; the full
unfiltered CLI result contains 690 logical classes.

## Complete authored type table

The following generated section is reserved for the **114 authored classes**: the
80 preserved APS declarations and 34 new Potbot declarations. It should be regenerated
from the inspector's validated composed graph so direct parents, profile abstraction,
and placement recommendations cannot drift from the machine-readable profile.
Use `syn hierarchy --limit 1000` for all 690 effective classes.

<!-- BEGIN GENERATED TYPE TABLE -->

| Preferred Type reference | Source layer | Direct parents | Profile abstraction | Recommended primitive kind |
| --- | --- | --- | --- | --- |
| `synny://type/agent-definition` | APS 0.2.0 (preserved) | `synny://type/agent-record`, `synny://type/definition-record` | concrete | `ember` |
| `synny://type/agent-deployment-record` | APS 0.2.0 (preserved) | `synny://type/agent-record`, `synny://type/deployment-record` | concrete | `ember` |
| `synny://type/agent-execution` | Potbot 0.1.0 | `syn://type/execution` | concrete | `event` |
| `synny://type/agent-process` | Potbot 0.1.0 | `syn://type/cog` | concrete | `cog` |
| `synny://type/agent-record` | APS 0.2.0 (preserved) | `synny://type/checked-record` | abstract | none (abstract) |
| `synny://type/agent-run-record` | APS 0.2.0 (preserved) | `synny://type/agent-record`, `synny://type/execution-record` | concrete | `ember` |
| `synny://type/agent-specification` | APS 0.2.0 (preserved) | `synny://type/primitive-specification` | concrete | `ember` |
| `synny://type/agents-md-file` | APS 0.2.0 (preserved) | `synny://type/file-occurrence` | concrete | `thing` |
| `synny://type/array-value` | APS 0.2.0 (preserved) | `synny://type/value-occurrence` | concrete | `thing` |
| `synny://type/benchmark-assessment` | Potbot 0.1.0 | `synny://type/checked-record`, `syn://type/pondle` | concrete | `pondle` |
| `synny://type/boolean-value` | APS 0.2.0 (preserved) | `synny://type/value-occurrence` | concrete | `thing` |
| `synny://type/bundle-manifest` | APS 0.2.0 (preserved) | `synny://type/manifest` | concrete | `ember` |
| `synny://type/byte-sequence` | APS 0.2.0 (preserved) | `synny://type/data-representation` | concrete | `thing` |
| `synny://type/checked-record` | APS 0.2.0 (preserved) | `synny://type/data-representation`, `syn://type/record` | concrete | `ember` |
| `synny://type/claim-record` | Potbot 0.1.0 | `synny://type/checked-record`, `syn://type/ember` | concrete | `ember` |
| `synny://type/clarification-question` | Potbot 0.1.0 | `synny://type/information-need` | concrete | `enigma` |
| `synny://type/coding-agent-definition` | APS 0.2.0 (preserved) | `synny://type/agent-definition` | concrete | `ember` |
| `synny://type/coding-workflow-definition` | APS 0.2.0 (preserved) | `synny://type/workflow-definition` | concrete | `ember` |
| `synny://type/composite-agent-definition` | APS 0.2.0 (preserved) | `synny://type/agent-definition` | concrete | `ember` |
| `synny://type/composite-skill-definition` | APS 0.2.0 (preserved) | `synny://type/skill-definition` | concrete | `ember` |
| `synny://type/composite-workflow-definition` | APS 0.2.0 (preserved) | `synny://type/workflow-definition` | concrete | `ember` |
| `synny://type/conformance-specification` | APS 0.2.0 (preserved) | `synny://type/primitive-specification` | concrete | `ember` |
| `synny://type/coordination-agent-definition` | APS 0.2.0 (preserved) | `synny://type/agent-definition` | concrete | `ember` |
| `synny://type/data-representation` | APS 0.2.0 (preserved) | `syn://type/representation` | abstract | none (abstract) |
| `synny://type/decimal-value` | APS 0.2.0 (preserved) | `synny://type/numeric-value` | concrete | `thing` |
| `synny://type/decision-record` | Potbot 0.1.0 | `synny://type/checked-record`, `syn://type/ember` | concrete | `ember` |
| `synny://type/decoded-text` | APS 0.2.0 (preserved) | `synny://type/data-representation` | concrete | `thing` |
| `synny://type/definition-record` | APS 0.2.0 (preserved) | `synny://type/checked-record`, `syn://type/specification` | abstract | none (abstract) |
| `synny://type/deployment-record` | APS 0.2.0 (preserved) | `synny://type/checked-record` | abstract | none (abstract) |
| `synny://type/deployment-workflow-definition` | APS 0.2.0 (preserved) | `synny://type/workflow-definition` | concrete | `ember` |
| `synny://type/derived-representation` | APS 0.2.0 (preserved) | `synny://type/data-representation` | abstract | none (abstract) |
| `synny://type/diagnostic-question` | Potbot 0.1.0 | `synny://type/information-need` | concrete | `enigma` |
| `synny://type/discovery-specification` | APS 0.2.0 (preserved) | `synny://type/primitive-specification` | concrete | `ember` |
| `synny://type/embedding-process` | Potbot 0.1.0 | `syn://type/cog` | concrete | `cog` |
| `synny://type/embedding-space-definition` | Potbot 0.1.0 | `synny://type/definition-record` | concrete | `ember` |
| `synny://type/evaluation-criterion-definition` | Potbot 0.1.0 | `synny://type/definition-record` | concrete | `ember` |
| `synny://type/evaluation-workflow-definition` | APS 0.2.0 (preserved) | `synny://type/workflow-definition` | concrete | `ember` |
| `synny://type/evidence-manifest` | Potbot 0.1.0 | `synny://type/manifest`, `syn://type/ember` | concrete | `ember` |
| `synny://type/execution-record` | APS 0.2.0 (preserved) | `synny://type/checked-record` | abstract | none (abstract) |
| `synny://type/execution-trace` | Potbot 0.1.0 | `synny://type/execution-record`, `syn://type/ember` | concrete | `ember` |
| `synny://type/file-occurrence` | APS 0.2.0 (preserved) | `synny://type/data-representation`, `syn://type/file` | concrete | `thing` |
| `synny://type/inference-execution` | Potbot 0.1.0 | `syn://type/execution` | concrete | `event` |
| `synny://type/inference-process` | Potbot 0.1.0 | `syn://type/cog` | concrete | `cog` |
| `synny://type/information-need` | Potbot 0.1.0 | `syn://type/enigma` | concrete | `enigma` |
| `synny://type/instruction-set` | APS 0.2.0 (preserved) | `synny://type/data-representation` | abstract | none (abstract) |
| `synny://type/instruction-set-specification` | APS 0.2.0 (preserved) | `synny://type/primitive-specification` | concrete | `ember` |
| `synny://type/integer-value` | APS 0.2.0 (preserved) | `synny://type/numeric-value` | concrete | `thing` |
| `synny://type/json-declared-file` | APS 0.2.0 (preserved) | `synny://type/file-occurrence` | concrete | `thing` |
| `synny://type/json-document` | APS 0.2.0 (preserved) | `synny://type/parsed-document` | concrete | `thing` |
| `synny://type/json-schema` | APS 0.2.0 (preserved) | `synny://type/validation-schema` | concrete | `ember` |
| `synny://type/manifest` | APS 0.2.0 (preserved) | `synny://type/checked-record` | abstract | none (abstract) |
| `synny://type/markdown-document` | APS 0.2.0 (preserved) | `synny://type/parsed-document` | concrete | `thing` |
| `synny://type/mcp-tool-definition` | APS 0.2.0 (preserved) | `synny://type/tool-definition` | concrete | `ember` |
| `synny://type/memory-change` | Potbot 0.1.0 | `syn://type/event`, `syn://type/change` | concrete | `event` |
| `synny://type/memory-curation-agent-definition` | APS 0.2.0 (preserved) | `synny://type/agent-definition` | concrete | `ember` |
| `synny://type/memory-curation-workflow-definition` | APS 0.2.0 (preserved) | `synny://type/workflow-definition` | concrete | `ember` |
| `synny://type/memory-state-record` | Potbot 0.1.0 | `synny://type/checked-record`, `syn://type/ember` | concrete | `ember` |
| `synny://type/null-value` | APS 0.2.0 (preserved) | `synny://type/value-occurrence` | concrete | `thing` |
| `synny://type/numeric-value` | APS 0.2.0 (preserved) | `synny://type/value-occurrence` | abstract | none (abstract) |
| `synny://type/object-value` | APS 0.2.0 (preserved) | `synny://type/value-occurrence` | concrete | `thing` |
| `synny://type/opaque-bytes` | APS 0.2.0 (preserved) | `synny://type/byte-sequence` | concrete | `thing` |
| `synny://type/operation-outcome` | Potbot 0.1.0 | `synny://type/execution-record`, `syn://type/ember` | concrete | `ember` |
| `synny://type/parsed-document` | APS 0.2.0 (preserved) | `synny://type/data-representation`, `syn://type/document` | abstract | none (abstract) |
| `synny://type/planning-agent-definition` | APS 0.2.0 (preserved) | `synny://type/agent-definition` | concrete | `ember` |
| `synny://type/policy-assessment` | Potbot 0.1.0 | `synny://type/checked-record`, `syn://type/pondle` | concrete | `pondle` |
| `synny://type/preference-record` | Potbot 0.1.0 | `synny://type/checked-record`, `syn://type/ember` | concrete | `ember` |
| `synny://type/primitive-core-specification` | APS 0.2.0 (preserved) | `synny://type/primitive-specification` | concrete | `ember` |
| `synny://type/primitive-specification` | APS 0.2.0 (preserved) | `synny://type/specification-document` | abstract | none (abstract) |
| `synny://type/procedural-skill-definition` | APS 0.2.0 (preserved) | `synny://type/skill-definition` | concrete | `ember` |
| `synny://type/reference-skill-definition` | APS 0.2.0 (preserved) | `synny://type/skill-definition` | concrete | `ember` |
| `synny://type/registry` | APS 0.2.0 (preserved) | `synny://type/checked-record`, `syn://type/dataset` | abstract | none (abstract) |
| `synny://type/relation-binding-record` | Potbot 0.1.0 | `synny://type/checked-record`, `syn://type/ember` | concrete | `ember` |
| `synny://type/repository-instructions` | APS 0.2.0 (preserved) | `synny://type/instruction-set` | concrete | `ember` |
| `synny://type/research-agent-definition` | APS 0.2.0 (preserved) | `synny://type/agent-definition` | concrete | `ember` |
| `synny://type/research-workflow-definition` | APS 0.2.0 (preserved) | `synny://type/workflow-definition` | concrete | `ember` |
| `synny://type/resource-budget-record` | Potbot 0.1.0 | `synny://type/checked-record`, `syn://type/ember` | concrete | `ember` |
| `synny://type/retrieval-assessment` | Potbot 0.1.0 | `synny://type/checked-record`, `syn://type/pondle` | concrete | `pondle` |
| `synny://type/retrieval-process` | Potbot 0.1.0 | `syn://type/cog` | concrete | `cog` |
| `synny://type/retrieval-profile-definition` | Potbot 0.1.0 | `synny://type/definition-record` | concrete | `ember` |
| `synny://type/retrieval-result` | Potbot 0.1.0 | `synny://type/checked-record`, `syn://type/ember` | concrete | `ember` |
| `synny://type/review-agent-definition` | APS 0.2.0 (preserved) | `synny://type/agent-definition` | concrete | `ember` |
| `synny://type/skill-definition` | APS 0.2.0 (preserved) | `synny://type/skill-record`, `synny://type/definition-record` | concrete | `ember` |
| `synny://type/skill-record` | APS 0.2.0 (preserved) | `synny://type/checked-record` | abstract | none (abstract) |
| `synny://type/skill-specification` | APS 0.2.0 (preserved) | `synny://type/primitive-specification` | concrete | `ember` |
| `synny://type/specification-document` | APS 0.2.0 (preserved) | `synny://type/parsed-document`, `syn://type/specification` | abstract | none (abstract) |
| `synny://type/sql-declared-file` | APS 0.2.0 (preserved) | `synny://type/file-occurrence` | concrete | `thing` |
| `synny://type/sql-document` | APS 0.2.0 (preserved) | `synny://type/parsed-document` | concrete | `thing` |
| `synny://type/string-value` | APS 0.2.0 (preserved) | `synny://type/value-occurrence` | concrete | `thing` |
| `synny://type/syn-object-record` | APS 0.2.0 (preserved) | `synny://type/checked-record` | concrete | `ember` |
| `synny://type/syn-type-definition` | APS 0.2.0 (preserved) | `synny://type/type-definition` | concrete | `ember` |
| `synny://type/text-fragment` | APS 0.2.0 (preserved) | `synny://type/derived-representation`, `synny://type/decoded-text` | concrete | `thing` |
| `synny://type/tool-definition` | APS 0.2.0 (preserved) | `synny://type/tool-interface-record`, `synny://type/definition-record`, `syn://type/api` | abstract | none (abstract) |
| `synny://type/tool-interface-record` | APS 0.2.0 (preserved) | `synny://type/checked-record` | abstract | none (abstract) |
| `synny://type/tool-invocation` | Potbot 0.1.0 | `syn://type/execution` | concrete | `event` |
| `synny://type/tool-process` | Potbot 0.1.0 | `syn://type/cog` | concrete | `cog` |
| `synny://type/tool-specification` | APS 0.2.0 (preserved) | `synny://type/primitive-specification` | concrete | `ember` |
| `synny://type/type-definition` | APS 0.2.0 (preserved) | `synny://type/definition-record` | concrete | `ember` |
| `synny://type/type-registry` | APS 0.2.0 (preserved) | `synny://type/registry` | concrete | `ember` |
| `synny://type/type-system-specification` | APS 0.2.0 (preserved) | `synny://type/primitive-specification` | concrete | `ember` |
| `synny://type/unclassified-representation` | APS 0.2.0 (preserved) | `synny://type/data-representation` | concrete | `thing` |
| `synny://type/validation-schema` | APS 0.2.0 (preserved) | `synny://type/parsed-document`, `syn://type/schema` | abstract | none (abstract) |
| `synny://type/value-occurrence` | APS 0.2.0 (preserved) | `synny://type/data-representation` | abstract | none (abstract) |
| `synny://type/verification-assessment` | Potbot 0.1.0 | `synny://type/checked-record`, `syn://type/pondle` | concrete | `pondle` |
| `synny://type/verification-assistance-agent-definition` | APS 0.2.0 (preserved) | `synny://type/agent-definition` | concrete | `ember` |
| `synny://type/verification-execution` | Potbot 0.1.0 | `syn://type/execution` | concrete | `event` |
| `synny://type/verification-process` | Potbot 0.1.0 | `syn://type/cog` | concrete | `cog` |
| `synny://type/verification-question` | Potbot 0.1.0 | `synny://type/information-need` | concrete | `enigma` |
| `synny://type/verification-workflow-definition` | APS 0.2.0 (preserved) | `synny://type/workflow-definition` | concrete | `ember` |
| `synny://type/workflow-definition` | APS 0.2.0 (preserved) | `synny://type/workflow-record`, `synny://type/definition-record` | concrete | `ember` |
| `synny://type/workflow-execution` | Potbot 0.1.0 | `syn://type/execution` | concrete | `event` |
| `synny://type/workflow-process` | Potbot 0.1.0 | `syn://type/cog` | concrete | `cog` |
| `synny://type/workflow-record` | APS 0.2.0 (preserved) | `synny://type/checked-record` | abstract | none (abstract) |
| `synny://type/workflow-run-record` | APS 0.2.0 (preserved) | `synny://type/workflow-record`, `synny://type/execution-record` | concrete | `ember` |
| `synny://type/workflow-specification` | APS 0.2.0 (preserved) | `synny://type/primitive-specification` | concrete | `ember` |

<!-- END GENERATED TYPE TABLE -->

## Source and behavior references

- [Profile data and audit provenance](../src/potbot/semantics/hierarchy_profile.json)
  pin the APS source, native primitive evidence, explicit reuse decisions, and new
  definitions.
- [Existing catalog implementation](../src/potbot/semantics/catalog.py) owns type
  identity normalization, graph merge, and ancestry; the [crosswalk](../vocab/syn-typing-crosswalk.json)
  retains native and protected legacy provenance.
- [Dump extraction provenance](../vocab/syn-database-types.provenance.json) identifies
  the supplied native database registry and its exact coverage.
- [SYN wire records](../src/potbot/domain/syn.py) distinguish Type declarations from
  instance objects and preserve exact public JSON strings.
- [Snapshot service](../src/potbot/semantics/service.py) owns workspace-scoped import,
  resolution, inventory, and candidate selection for named embeddings.
- [Guide](type-hierarchy-and-syn-primitives.md) explains records versus referents,
  native primitive meanings, opt-in import, and the limits of these recommendations.
