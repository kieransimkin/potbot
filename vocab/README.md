# Agent Semantic Vocabulary 0.1.0

This is the machine-readable design companion to Agent_Semantic_Vocabulary_and_Semantics_v0_1.docx. It defines a bounded first version; it is not an implemented agent, evaluator or migration.

## Files

- vocabulary.json: 25 entity types, 16 directed predicates, controlled concepts/enums, guard fields/operators and limits.
- semantic-bundle.schema.json: JSON Schema 2020-12 for ground semantic frames and their evidence bindings. Procedure templates retain the existing architecture's schema; their instantiated ground views use these frames.
- feature-encoding.json: inspectable feature families, dictionary rules and an initial ranking profile.
- retrieval-profiles.json and retrieval-profiles.schema.json: seven question contracts, candidate channels, decision gates and failure semantics, including exhaustive type inventory.
- retrieval-request.schema.json and example-retrieval-request.json: a question-owned request with explicit profile selection and checked statement/entity references.
- data-types.json: primary data types, explicit subtype relationships, agent/workflow families, parser policies and exact enumeration rules.
- typed-data-item.schema.json and type-query.schema.json: checked item occurrence and deterministic type-query shapes.
- example-typed-items.json and example-type-queries.json: file, parsed-value, agent/workflow and enumeration fixtures.
- type-conformance-cases.json: expected type classification, snapshot, coverage and pagination behavior.
- example-bundle.json and fixture-entities.json: illustrative values only, not claims about a real repository.
- invalid-schema-examples.json: examples that must fail shape validation.
- semantic-validation-rules.json: service-level obligations that JSON Schema does not establish.
- conformance-cases.json: expected semantic/runtime behavior to implement and test.
- validation-report.json: actual checks performed on this design package.

## Validation boundary

Validate JSON shape first, then vocabulary/type rules, then checked database references, scope, evidence, time and authority through the existing services. A schema-valid frame cannot promote memory, claim a test passed, grant a capability or execute an action. The workspace and producer identity are supplied by the authenticated enclosing command and existing immutable revision/event records.

The design keeps memory_items, memory_revisions, memory_evidence, lifecycle, applicability and capability ownership from the existing plan. New semantic frames/features are checked views and indexes over those records. New extracted meaning follows the normal proposal/admission path. Questions retain need_revisions ownership through separate need_query_views; query embeddings key the retrieval run, need revision, exact encoding-input hash and compatible space generation. Query unknowns are not wildcards.

## Numeric and missing-value conventions

Integers and decimals use canonical strings; zero is "0" and negative zero is rejected. Versions, digests and timestamps are typed separately. UTC timestamp canonicalization and pinned version parsing are service requirements. Unknown is explicit and is neither null, absent, false nor a query wildcard.

## Hashes and exact matching

Apply declared safe guard normalization, then RFC 8785 canonical JSON and SHA-256. A proposition digest includes vocabulary release and its signed typed atom. A statement digest adds mode, force, when, scope, applicability and valid-time qualifiers. Evidence, producer and lifecycle do not enter meaning identity. Retain canonical bytes. Partial/unknown frames remain ineligible for a confirmed exact-meaning result even if their serialized bytes match.

## Integration

See the document's S01-S12 plan mapping, and the P references in conformance-cases.json. Guards preserve applicable / inapplicable / unknown_requires_check. Four support-state labels are diagnostics, not a replacement applicability engine. Learned projections remain optional under P149-P150 and do not train current facts/preferences into weights automatically.

## Deterministic types

Each catalogued occurrence has one registered primary type within its source revision and classifier generation. Type identity is different from item identity. Exact type equality is the default; subtype inclusion follows the published graph. Files, decoded/parsed representations and their contained values are linked items, not interchangeable types. Agent/workflow definitions, deployments and runs remain separate typed objects with checked relationships.

Type inventory bypasses relevance ranking and revision-view deduplication. It preserves every matching occurrence in a frozen ordered enumeration manifest. Source ingestion, classification, nested itemisation, exact selection and delivery completeness are reported separately. Missing parsing or descendant coverage prevents an unqualified all-examples claim. Current access/deletion checks govern each page; changed eligibility invalidates continuation.

## Schema resolution

All schemas use JSON Schema Draft 2020-12. Register their exact $id values with the validator: retrieval-request.schema.json references urn:agent-sem:schema:type-query:0.1.0. Shape-valid illustrative records do not prove live ownership, authority, parser coverage or database conformance. The validation report lists actual static checks and planned runtime work separately.
## Runtime named-embedding schema

`artifact-embedding-manifest.schema.json` adds the versioned runtime contract
`urn:potbot:schema:artifact-embedding-manifest:1`, with a complete example in
`example-artifact-embedding-manifest.json`. Its `named_embeddings` map contains
source-qualified references grouped by representation name. Every reference pins an
immutable view and encoding space; names do not replace exact data types or semantic
predicates. Lists preserve multiple source occurrences and space generations.

This additive runtime contract does not revise the published `0.1.0` vocabulary or
its existing schemas. The original `validation-report.json` remains the historical
design-package validation result. Current implementation tests are run by Potbot's CI.
See [named embeddings](../docs/named-embeddings.md) for the migration and API contract.

## Runtime SYN snapshot integration

[`syn-snapshot.schema.json`](syn-snapshot.schema.json) defines the additive runtime
contract `urn:potbot:schema:syn-snapshot:1`; a complete synthetic example is in
[`example-syn-snapshot.json`](example-syn-snapshot.json).
[`syn-effective-snapshot.schema.json`](syn-effective-snapshot.schema.json) describes
the canonical source-plus-catalog artifact, and
[`syn-resolved-record.schema.json`](syn-resolved-record.schema.json) describes a
scoped resolved record with its source-qualified named-vector references.
[`syn-typing-crosswalk.json`](syn-typing-crosswalk.json) records the inspected SYN
source versions and digests, 529 bundled native classes reconciled with the supplied
database, and explicit Type mappings for all 45 existing data-type definitions. Two public
SYN record definitions bring the protected Type set to 47, giving 576 combined
definitions. The original 450 Python seed classes are all retained; the database
contributes 79 generated feed-intersection classes and its authoritative parent edges.

`syn://type/<slug>` and `synny://type/<slug>` are equivalent references to one type
within the selected catalog. Native classes use preferred `syn://` references;
Potbot representation types use preferred `synny://type/potbot-data-...` references.
The `potbot-data-` slug prefix is protected under both schemes. Existing `dt:*` IDs
remain compatibility identifiers, and the crosswalk registers their exact mappings.
These scheme aliases do not double the catalog or create distinct artifacts/vectors.
Instance identifiers remain native `syn://` references. Source spellings are retained;
effective catalog definitions and resolved records use the preferred references.

[`syn-database-types.snapshot.json`](syn-database-types.snapshot.json) is the complete
extracted 529-type database registry, with 859 parent edges, zero direct property
declarations, and an explicitly empty object inventory. It can be imported without
loading the original SQL dump or an encoder. These counts describe the supplied
snapshot; future complete exports can include further classes and local edits.
[`syn-database-types.provenance.json`](syn-database-types.provenance.json) records
the exact source digest and extraction coverage. The full SQL dump and its instance
and credential contents are not part of this package.

Native semantic class, storage kind, checked representation type, and embedding
name remain separate. All agent/workflow definition, deployment, run, role, and kind
distinctions remain intact. Imported instance records have derived primary type
`dt:syn.object_record`; Type definitions have `dt:syn.type_definition`. Resolved output
keeps those compatibility IDs in `primary_data_type`, exposes the preferred URL in
`primary_data_type_ref`, and records both the caller's `requested_ref` and the stored
canonical `ref`. A source class
assertion cannot create an agent or workflow, grant a capability, or establish a
verified fact.

SYN public JSON is retained as exact text, preserving its persisted numeric lexemes;
the canonical numeric-string rules above still apply to the original semantic-frame
contract. Runtime parsing additionally checks duplicate keys and duplicate Type aliases,
recursive private fields, payload identities, byte/count limits, and the merged type
graph. Payload object identity must match exactly; outer and embedded Type references
may use equivalent schemes while retaining both original strings. Schema validation
alone does not establish these properties.

The [SYN integration guide](../docs/syn-semantic-integration.md) explains migration 6,
read-only export coverage, immutable workspace/namespace/snapshot scope, exact
type enumeration, and named-vector ranking. These additions leave every published
0.1.0 JSON and DOCX contract unchanged.

## Expanded hierarchy and native primitive placement

The opt-in [`potbot-hierarchy.snapshot.json`](potbot-hierarchy.snapshot.json)
contains 114 authored type declarations and no instances: 80 unchanged APS 0.2
declarations plus 34 additions for processes, events, questions, evidence,
assessments and definition records. Combined with the 529 native and 47 protected
legacy definitions, it gives 690 unique type identities. It does not replace the
default bundled catalog or migrate existing objects.

The versioned source is
[`hierarchy_profile.json`](../src/potbot/semantics/hierarchy_profile.json), with
[`potbot-hierarchy-profile.schema.json`](potbot-hierarchy-profile.schema.json).
Its recommendations explain a preferred native primitive kind, or an explicit
abstract no-default decision, for each authored class and selected native classes.
They do not enforce primitive compatibility
on imported source assertions or grant APS payload-profile support.

[`ai-primitives-0.2.0.snapshot.json`](ai-primitives-0.2.0.snapshot.json) preserves
the exact 80-class source extension. The corresponding
[explicit URL migration evidence](ai-primitives-type-migration.json) remains a
separate design mapping; changed slugs are not automatic aliases.

See the [placement guide](../docs/type-hierarchy-and-syn-primitives.md) and
[complete authored hierarchy](../docs/type-hierarchy-reference.md). The new
offline `potbot syn hierarchy` and `potbot syn placement` commands require no
database or encoder. Stored instance inventory and search use `--primitive-kind`
as an additional exact filter within their selected immutable snapshot.

The [generated provenance](potbot-hierarchy.provenance.json) pins the source hashes,
catalog composition, and reproducible export. The
[synthetic object example](example-potbot-primitive-views.snapshot.json) and
[illustrative links](example-potbot-primitive-links.json) show distinct native
identities for the record, process, occurrence, question, and assessment views.
The link file is descriptive and is not imported by the snapshot service.
Regenerate these assets with `uv run --locked python scripts/generate_type_hierarchy.py`;
the regular check runner verifies them with `--check`.

## Agent security and Ember contracts

`agent-security.snapshot.json` is an opt-in snapshot extending the preserved Potbot
hierarchy with 25 security Types (715 effective Types). Potbot-authored names prefer
`synny://type/`; matching `syn://type/` spellings are the same Type identities.

`ember-contract-bundle.schema.json` defines scoped Type defaults, slot declarations,
instance bindings, exact Ember revisions, and explicit public/owner projections.
`security-public/` contains the public identity, delegation and signature schemas.
Implementation Embers publish exact source content and pinned build inputs. Semantic
signing requires their complete inherited revision set to match the current local
implementation; that content policy does not attest a binary or authorize imported
code execution. Public and owner projections retain independent content identities,
and private operation handles are available only through the authorized owner view.
See [the runtime addendum](../docs/agent-identity-and-ember-runtime.md) for validated
semantics, deterministic Ember Type inventory, private storage boundaries, custody
limits and command examples.
