# Canonical Type consensus policies and public key node identity

23 October 2026. **Owner-directed architecture update; implementation pending.**
This contract amends the [24-package integration plan](13-syn-blockchain-transaction-plan.md).
It replaces that plan's single CometBFT-first experiment with consensus selected
by each canonical Syn Type, using simpler agreement within one custody root and
a BFT DAG/Hashgraph design across independent custody roots.

Every Syn node is referenced by **its own public key hash**. A credential issuer's
root-key hash proves custody provenance and constrains policy; it is never a node
address. The operator retains the base key. Existing restricted Agent vaults and
their public membership certificates remain the custody foundation.

![Canonical Type policy selects same-custody agreement or federated DAG Hashgraph consensus, with all participants addressed by node public key hash](../media/syn-type-consensus.svg)

*Generated design illustration. The resolver, agreement profiles and federated
engine shown here require implementation; this is not a running-network capture.*

## 1 Decisions established by the owner

| Requirement | Architectural consequence |
| --- | --- |
| Agents under one operator can use simpler consensus | Authenticate common credential custody, then use an operator-scoped replicated log with an explicit crash/partition fault model |
| Outside common custody, prefer BFT DAG plus Hashgraph | Use an authenticated BFT event/data DAG and Hashgraph-family virtual voting/order; qualify their complete composition |
| Every Syn data Type defines its consensus policy in its canonical Type reference | Resolve the exact canonical Type definition/release and its committed `consensusPolicy` before choosing a protocol |
| Refer to nodes by public key hash, never an operator identifier | Participant lists, senders, recipients, voters, event creators and peer references use `node_key_hash` |
| Each Agent has its own Syn; all inter-agent communication is through private Syn pub-sub | Both consensus profiles, repair, proof and checkpoint traffic use that same Syn-owned transport boundary |
| Agents receive their assigned account key and current group key | No parent/base key sharing, raw key export or unannounced extra group-key history |

An operator-scoped result does not become federated finality by being forwarded.
A node cannot select a weaker mode because the available peers happen to share
an issuer. The Type's adopted policy decides what evidence the record needs.

## 2 Canonical node reference and credential evidence

For the existing Ed25519 account profile:

```text
node_key_hash = lowercase_hex(SHA256(raw_32_byte_assigned_account_public_key))
```

This equals the current Agent account fingerprint. Do not hash a base64 spelling,
JSON descriptor, transport peer ID, account number or operator label instead.
Future algorithms require a versioned identity profile; v1 has a fixed algorithm.

All public Syn references to another node carry this hash. Internally the network
adapter resolves its authenticated transport locators; callers do not address a
node by a libp2p peer ID or an engine's numeric index. Library-internal IDs may be
necessary, but remain adapter-private: persist a bijective roster/epoch mapping
to full node hashes. Never truncate hashes into numeric IDs or expose those IDs
as an alternative Syn identity.

| Field | Meaning and permitted use |
| --- | --- |
| `node_key_hash` | Canonical node identity and routing target |
| Full account public key | Verify signatures after checking its hash equals `node_key_hash` |
| `issuer_key_hash` | Hash of the credential-signing base public key; validates custody lineage and policy constraints |
| Signed membership certificate | Binds assigned account, instance, group/epoch and issuer; public evidence, not a base secret |
| Instance UUID | Process/store binding and duplicate-instance diagnostics; not the node reference |
| Transport locator/peer ID | Connection detail authenticated against the expected node hash; not voting identity |
| Roster digest and epoch | The exact eligible participants, roles and weights for a consensus stream |

### Proving common custody

1. Resolve the full public key and membership certificate for the expected node hash.
2. Verify the certificate against the pinned issuer public key/hash and check the
   assigned account's public key hashes to that node reference.
3. Require fresh possession of the assigned account private key. A challenge
   binds both node hashes, nonce, domain, group/epoch, session and expiry; consume
   it once. The current short-lived Agent proof can sign the challenge digest
   within its existing scope; archival ledger evidence has a separate profile.
4. Compare the verified issuer-key hashes when a Type requires common custody.
   Check each account is eligible in the current explicit voter roster. Merely
   possessing a valid account certificate does not create a vote or execution grant.
5. Run the selected agreement algorithm. Key possession establishes authenticated
   membership; it does not choose between conflicting valid proposals.

The existing account derivation is hardened. Its public key does not independently
reveal a mathematical derivation path from the base public key. The accepted
evidence is the trusted issuer's signed derivation/assignment attestation plus
live possession, backed by native operator-side derivation and its test vectors.
Do not publish parent secrets or change hardened derivation to manufacture a
public lineage check. This also does not attest that the node runs unmodified code.

Group-key rotation preserves the node hash when its account key is unchanged.
Account-key replacement creates a new node hash and requires an authorised
roster/identity transition; historical records keep their original author hash.

## 3 Policy belongs to the canonical Type definition

Add a first-class validated **`consensusPolicy`** attribute to the canonical Type
release/export contract. The URI identifies the Type; the selected release and
definition digest identify its immutable contents. A mutable local Type row,
arbitrary `knowledge` JSON or an application configuration file cannot substitute
for that adopted definition.

The current Syn `Type` model and Potbot `SynType` metadata can carry general
information, but do not yet implement this policy contract or enforcement.
Implementation must extend the schema/API, canonical export, sealed snapshot,
parser, resolver and admission checks together. Preserve the existing URI/alias
rules: `syn://type/...` and `synny://type/...` alone do not establish source scope.

Every new replicated record binds:

```text
canonical_type_ref
semantic_release_digest
type_definition_digest
consensus_policy_digest
consensus_stream_id
membership_roster_digest
policy_epoch
author_node_key_hash
```

The receiver resolves these against its accepted canonical release, checks the
actual payload/subject Type, and derives the allowed profile. A caller-supplied
`mode`, backend name, issuer label or proof cannot override that result.

### Proposed policy shape

These Type names and fields are proposed schema examples, not registered runtime
Types or executable configuration. Digests below are placeholders.

```json
{
  "type_ref": "synny://type/operator-task-assignment",
  "type_revision": "1",
  "consensusPolicy": {
    "profile": "syn.consensus-policy/1",
    "policy_revision": "1",
    "protocol": "syn.operator-log/1",
    "custody_constraint": "same-verified-issuer",
    "membership_anchor_digest": "<governed-roster-digest>",
    "participant_reference": "node-public-key-sha256",
    "quorum_rule": "strict-majority",
    "fault_model": "crash-and-partition",
    "proof_profile": "syn.operator-commit/1",
    "conflict_namespace": "operator-task-assignments",
    "change_rule": "current-policy-and-type-governance"
  }
}
```

A Type requiring independent-custody agreement uses its own explicit policy:

```json
{
  "type_ref": "synny://type/federated-task-assignment",
  "type_revision": "1",
  "consensusPolicy": {
    "profile": "syn.consensus-policy/1",
    "policy_revision": "1",
    "protocol": "syn.bft-dag-hashgraph/1",
    "custody_constraint": "independent-issuer-weight-caps",
    "membership_anchor_digest": "<governed-federation-roster-digest>",
    "participant_reference": "node-public-key-sha256",
    "quorum_rule": "bft-supermajority",
    "fault_model": "byzantine-less-than-one-third-weight",
    "proof_profile": "syn.dag-hashgraph-finality/1",
    "conflict_namespace": "federated-task-assignments",
    "change_rule": "current-policy-and-type-governance"
  }
}
```

The profile schemas must fix exact quorum arithmetic, permitted weight models,
proof contents, bounds and protocol versions. Free-form policy code does not run
inside the resolver. Group/disclosure constraints are separately explicit and
must also pass before any record is sent.

### Inheritance and legacy Types

For v1, every admitted Type release contains an explicit policy. Type ancestry
does not silently select a parent's backend, merge quorums or choose a weaker
policy. Authoring tools can propose a copied policy, but its resolved value must
be materialised and approved in the child's canonical definition. Incompatible
governance/ancestor constraints reject adoption.

Legacy Types without a policy remain readable as historical/local data. They
cannot enter new shared transaction processing until a governed release supplies
the policy. Unknown or unsupported profiles hold the affected Type; there is no
automatic downgrade to local signing or a default global engine.

### Policy change and bootstrap

Genesis pins the initial canonical Type-governance policy and its trust anchors.
This terminates the bootstrap dependency; an untrusted Type cannot nominate the
rules by which it approves itself.

A Type-policy update requires the **current** policy's approval and the applicable
canonical Type-governance authority, then a versioned adoption/cutover record.
The new policy cannot ratify its own weaker quorum. Changing protocol, membership,
conflict ownership or trust scope requires an explicit checkpoint handover.
Existing records and running tasks keep their pinned definitions and proof profiles.

## 4 Same-custody agreement profile

`syn.operator-log/1` is the simpler profile for Types explicitly trusting one
verified credential issuer. Use a complete Raft-style replicated state machine,
with root-certified membership and signed Syn transport messages.

The proposed integration loop is:

1. Resolve the Type policy and current roster of full node hashes.
2. Authenticate each participant and its common issuer constraint.
3. The elected leader proposes a bounded entry binding stream, term, position,
   previous entry, Type/policy digests and exact operation bytes.
4. Followers validate and durably persist according to the complete protocol's
   term, vote and log rules before issuing the corresponding acknowledgements.
5. Commit only under the protocol's current-term/majority rules, then apply the
   deterministic reducer. Elections, configuration changes and restart persistence
   use the complete selected library, not this outline as a replacement specification.
6. Produce an operator-profile receipt/checkpoint and verify its exact trust claim.
   An append acknowledgement is not itself an authenticated application-state root.

For a configured N-node roster, strict majority is `floor(N/2)+1`: one node needs
one; two need both; three need two. A minority partition cannot create new
committed shared state. Offline nonvoters do not change the configured quorum.
Membership changes use the protocol's safe old/new configuration transition.
[Raft's paper](https://raft.github.io/raft.pdf) is the protocol reference.

**First implementation candidate:** the Rust `raft-rs` core, whose documented
design leaves storage, state machine and transport to the embedding application.
That separation fits Syn-owned private pub-sub and the native helper boundary.
Syn must implement durable storage and the message/persistence integration loop;
the crate is not a complete deployed service. [Upstream design](https://raw.githubusercontent.com/tikv/raft-rs/master/README.md).

The Go `etcd/raft` core is an alternative if its integration evidence is stronger;
it also delegates network/disk I/O to its caller. A Go helper would be another
maintained runtime boundary. [Upstream integration contract](https://raw.githubusercontent.com/etcd-io/raft/main/README.md).

This profile trusts authorised participants to follow the crash-fault protocol.
A common issuer can derive the assigned keys, and a compromised authorised node
can behave Byzantine. Authentication does not turn Raft into BFT. A Type needing
that stronger fault model must explicitly select the federated profile.

## 5 Cross-custody BFT DAG and Hashgraph profile

`syn.bft-dag-hashgraph/1` is the owner's preferred resilient direction for Types
requiring agreement outside a shared custody root.

The intended composition has **one** decision path:

```text
signed typed transaction batches
    -> authenticated DAG and required data availability
    -> Hashgraph-family gossip-about-gossip and virtual voting
    -> one final order of eligible batch/transaction identities
    -> deterministic Syn validation and state transition
    -> policy-bound finality and outcome evidence
```

Hashgraph is itself a DAG-based BFT design. The combination should preserve those
properties, adding a clearly specified availability/dissemination layer where
needed rather than running two unrelated ordering systems and choosing a winner.
The original paper describes signed gossip history and virtual voting;
[Hashgraph protocol source](https://www.swirlds.com/downloads/SWIRLDS-TR-2016-01.pdf).

### Participants and independent fault weight

Roster entries and DAG creators are identified by node public-key hash. Their
verified issuer hashes describe correlated custody, not another node identity.
The first federation profile permits one voting delegate per independent issuer
per epoch, or an equivalently explicit aggregate weight cap. Other accounts can
author, relay and replicate without creating additional independent weight.

Use the conventional minimum pilot of four independent custody roots/voting
delegates, equal weight, for one Byzantine fault and a three-vote supermajority.
Seven independent roots can model two faults with five votes. Four child keys of
one base do not supply four independent fault domains. Unknown roots are not
automatically admitted; permissioned genesis/governance prevents that Sybil path.

Independent custody is an explicit admission assumption, supported by the
federation's governance and operational evidence. Distinct public keys or issuer
hashes alone cannot prove that their controllers are independent people or hosts.
For a qualified weighted profile, Byzantine weight must remain below one third
of the total and the relevant supermajority exceeds two thirds, using exact
integer arithmetic. These thresholds belong to the complete protocol's voting
rules; collecting that many arbitrary signatures is not an implementation.

### Required DAG and ordering rules

1. Each signed event binds its creator node hash, stream, roster/epoch, sequence,
   self-parent, allowed other-parent references, batch/availability commitment
   and exact Type/policy context. Freeze the pinned engine's actual event grammar.
2. Verify signatures, parent availability, acyclicity, causal limits and creator
   eligibility. Retain both signed branches as equivocation evidence; do not
   accept an attacker-selected convenient branch as uniquely authoritative.
3. Define which bytes an availability certificate covers, who durably retains
   them, retention/checkpoint obligations and how another admitted node retrieves
   them. A digest or gossip receipt alone does not establish data availability.
4. Apply the complete Hashgraph-family round/visibility/virtual-voting/finality
   rules to the qualified event history. Pin assumptions, randomness/coin rules,
   membership transitions and deterministic tie handling in the selected profile.
5. Require the availability and ordering layers to use the same admitted roster,
   weight, epoch and batch identities. The interface must preserve valid events
   and their causal evidence; an adapter cannot discard information needed by
   the ordering algorithm's safety argument.
6. Execute finalised operations using the same Type-pinned reducer on every node.
   Independently authenticate/replay the resulting outcome root; ordering input
   is not automatically proof of the custom application's resulting state.

The [Narwhal and Tusk paper](https://arxiv.org/abs/2105.11827) is useful for the
separation of reliable dissemination from ordering. Its algorithms and performance
results do not prove a new Narwhal/Hashgraph composition. The original
[MystenLabs/narwhal repository](https://github.com/MystenLabs/narwhal) is archived
and read-only; it is research material, not the default maintained dependency.

### Concrete implementation investigation

| Candidate | Intended use | Required spike and decision |
| --- | --- | --- |
| Hiero consensus/platform modules | First upstream investigation for actual Hashgraph-family ordering | Establish a supported embedding/application boundary, event/proof export, Syn-only transport injection, protected signing and exact versioned recovery; do not assume a drop-in Python library |
| Self-hosted Hiero service boundary | Possible isolation boundary if embedding is unsuitable | Prove all cross-node traffic still uses Syn and custom Syn outcome evidence is independently verified; ordinary public HCS use would not satisfy the private transport requirement |
| A reviewed native Hashgraph implementation | Fallback only if no supported upstream integration satisfies the contract | Separate correctness model, differential tests, cryptographic/consensus review and maintenance commitment; a simulator/pseudocode port is not production acceptance |
| Other DAG BFT engines | Test comparator for availability/recovery or a separately proposed policy profile | Do not substitute a non-Hashgraph finality algorithm silently for the owner's chosen profile |

[Hiero's architecture document](https://raw.githubusercontent.com/hiero-ledger/hiero-consensus-node/main/hedera-node/docs/design/design.md)
describes separate platform/application responsibilities and private deployments.
It does not establish that the exact Syn embedding, protected signer or pub-sub
adapter already exists. Pin actual source/release/API at spike start and record
unsupported interfaces as blockers, not capabilities inferred from a module name.

If a separate certified dissemination DAG is required, its composition with the
Hashgraph event DAG is a protocol-design deliverable with an explicit validity,
safety, liveness and recovery argument. Reusing papers' names does not inherit
their proofs. Bounded eventual communication assumptions do not promise finite
progress during an indefinite partition or unavailable quorum.

## 6 Runtime components and end to end dispatch

| Proposed component | Interface and responsibility |
| --- | --- |
| `NodeIdentityRegistry` | Resolve full keys/certificates by expected node hash; bind authenticated locators and current instance state |
| `CanonicalTypePolicyResolver` | Resolve adopted Type/release, validate `consensusPolicy`, roster, custody constraints and supported profile; return immutable `PolicyContext` |
| `ConsensusRouter` | Route by domain/genesis, policy digest, conflict namespace and epoch; reject caller-selected downgrade |
| `OperatorLogAdapter` | Embed the qualified crash-fault engine, persist protocol state and exchange its messages over Syn |
| `FederatedDagAdapter` | Persist/validate certified event history, integrate Hashgraph-family order and retain Byzantine evidence |
| `DeterministicStateReducer` | Validate/apply exact typed operations in the committed stream; no model/network side effects |
| `ConsensusProofVerifier` | Verify the profile required by the exact Type and authenticate the outcome claim |
| `TypePolicyGovernance` | Adopt canonical definitions and checkpointed policy/roster transitions under existing authority |
| `SynTransportGateway` | Frame, sign/encrypt, repair and rate-limit all consensus traffic; targets remain node hashes |

Several Types with identical effective policy, membership and conflict ownership
can share a physical engine. Each record still binds its exact canonical Type.
The system need not run a process per Type, but it must not collapse differing
policy contexts into one generic unqualified chain.

A finalised position is scoped by **stream and epoch**, not a universal height
across every Type. Preserve Potbot event cursors, local Syn journal cursors,
author/event sequences and each consensus stream position separately.

### Conflict domains and atomic operations

One mutable conflict key/resource has one authoritative consensus policy context.
A weaker Type must not mutate the same reservation, grant or balance through
another stream. Validate declared write sets/conflict ownership before admission.
In v1, reject an atomic operation spanning incompatible policies. Split it only
when the business operation permits a saga, or define a governed common transaction
Type whose policy explicitly authorises all affected state. Do not choose whichever
quorum is easiest or assume a larger numeric quorum satisfies every trust model.

### Proposed persistence

- Immutable canonical Type-policy releases and their adoption proofs.
- Node public-key/hash bindings and issuer certificates; private keys remain native.
- Versioned participant rosters and adapter-private engine-ID mappings.
- Operator log/vote/term/snapshot persistence and applied-state positions.
- Signed DAG events, availability batches, missing-parent quarantine and equivocation evidence.
- Per-stream finality checkpoints, deterministic outcomes and projection cursors.
- Old-policy/new-policy transition records and exact cutoff checkpoints.

Use new migrations under the appropriate owner. Preserve existing Type/property,
receipt and private-vault profiles. Current metadata storage is not an enforcement
implementation, and no live migration is performed by this design update.

### Admission and execution flow

1. Potbot seals a typed intent against an adopted semantic release and its local
   disclosure/effect rules. Syn authenticates the local caller and account binding.
2. Syn resolves the Type using its scoped canonical catalog and exact definition
   digest. A matching URI/slug in another installation is not the same authority.
   Validate payload Type, proposed operation, subject and declared write set.
   Resolve the policies of affected subject/state Types as well as the operation
   envelope. A transaction Type cannot override the Type protecting a resource;
   the complete write set must belong to one authorised compatible context.
3. Build immutable `PolicyContext`: domain/genesis, Type/release/definition digest,
   policy digest, conflict namespace, stream/epoch, roster digest and proof profile.
   A read-only preview can display this context; it cannot approve a mutation.
4. Check author permissions and group visibility, then persist/sign exact bytes
   through native custody. Any derived signing role needs an explicit account
   delegation and purpose binding; its public key is not a replacement node address.
5. Route to the qualified adapter. Engine-internal messages are encrypted Syn
   frames, authenticated to full node hashes and bounded by stream quotas.
6. On committed order, resolve the pinned context and authorisation at that
   position again. Atomically store reducer outcome, state root and applied cursor.
   A prior mempool admission does not guarantee the transaction will be accepted.
7. Assemble and independently verify the requested profile's evidence. Potbot
   admits the verified outcome through its own inbox/projection transaction, then
   rechecks local permission before any external action.

### Proof and checkpoint contracts to freeze

| Claim | Required evidence and limits |
| --- | --- |
| Same-custody committed entry | Selected Raft protocol's durable commit evidence plus an authenticated checkpoint/receipt under the explicit trusted-member policy; one signed leader response alone proves only that leader's assertion |
| Federated final order | Selected Hashgraph implementation's verifiable event history/checkpoint and roster/weight transitions, or a separately specified certified checkpoint profile; a generic quorum-signature blob is insufficient |
| Accepted application outcome | Exact intent, committed position, reducer/version and authenticated result/state commitment, or verified deterministic replay from a trusted snapshot through that position |
| Current state for a new write | Expected revision plus the protocol's qualified current-read mechanism; an old valid checkpoint proves history, not that no later revocation or assignment exists |
| Historical Type authority | Canonical definition/policy bytes and adoption chain back to pinned governance, including the old-policy-approved cutoff |

SBT11 must choose an exact operator-checkpoint authentication rule and test it
against full Raft commit/replay before issuing that proof profile. Followers must
not sign arbitrary proposed roots as though committed. Likewise the Hiero spike
must establish the actual verifiable exports and their relationship to Syn's
custom application. Where a maintained succinct verifier does not exist, retain
the required history for full replay and declare its cost; do not invent a light
proof format from an API response. Neither mode needs every Agent to be a voter.

### Failure behaviour

| Condition | Required action |
| --- | --- |
| Unknown Type, unsupported policy or missing adopted definition | Hold/quarantine the affected operation; no engine fallback |
| Operator minority or federation quorum unavailable | Stop new shared commits in that context; bounded repair and explicitly local work may continue |
| Missing event parent or certified payload bytes | Fetch through Syn under quotas; do not apply an unavailable transaction |
| Conflicting creator events or account/instance duplication | Retain evidence, stop unsafe signing and apply the profile's governed isolation/recovery rules |
| Type policy/roster changes while an intent waits | Preserve the immutable intent; execute only under its specified transition rules or return an explicit stale-policy rejection |
| Group epoch/custody state disagrees with the checkpoint | Pause affected networking/signing until current-key and counter safety is established |
| Another stream claims an owned conflict key | Reject before state mutation, even if its own proof is otherwise valid |
| Syn commits but Potbot loses the acknowledgement | Query/retry the same identity and exact bytes; do not repeat external work |

### Bounded implementation investigations

These experiments produce decision evidence before protocol selection; none is
permission to weaken the transport, custody or Type-policy contract.

| Investigation | Concrete deliverables | Selection gate |
| --- | --- | --- |
| Canonical Type authority | Proposed schema/validation, genesis/adoption vectors, resolver and downgrade corpus | Same exact Type/policy resolves independently in Syn and Potbot; governance and conflict bypasses fail |
| `raft-rs` embedding | Pin release/source/licence/toolchain; integrate three stores, message loop, Syn transport and protected signing | Crash at every persistence/message boundary; 2–1 and 1–1 partitions, safe configuration changes and deterministic replay |
| Hiero integration feasibility | Pin supported release/modules/APIs/licence; minimal application, event/order/proof export, transport and signer capability matrix | Actual private Syn frames and protected native signing, usable recovery and independently verified custom outcome; mark unavailable hooks as blockers |
| Federation transport/key policy | Define authorised key provisioning or reviewed agreement, root admission, current-key cutover and disclosure test cases | Different operators exchange through one admitted private group without sharing base keys or adding undeclared Agent secrets |
| DAG/Hashgraph composition | Exact event/batch grammar, availability obligations, round/vote rules, membership transition and deterministic simulator | Explicit safety/liveness assumptions and review; differential comparison with the selected upstream where possible, then real Byzantine fault tests |
| Profile-aware proof verifier | Golden valid/invalid bundles, checkpoint trust and historical policy/roster fixtures | Independent offline replay rejects a wrong Type/profile/root/outcome and unsafe restore; measured verification/retention cost |

First implement contract/resolver work under SBT01–SBT04, then durable replication
and the simpler operator-profile experiment. Keep federation Types disabled while
its feasibility and correctness gates are unresolved. A failed Hiero embedding
spike leads to a named upstream-adapter task or reviewed native implementation
proposal, with its engineering/security cost recorded. It does not make a
different consensus family equivalent to the requested Hashgraph direction.

## 7 Privacy and key scope across custody boundaries

The existing within-custody group key remains derived from the operator's secret
base material and delivered only as the current epoch key. Its public issuer hash
cannot derive that secret.

Cross-custody traffic needs a separately defined federation-group provisioning/
agreement policy; it cannot pretend that different base roots derive the same key.
No participant receives another issuer's base key. Resolve the actual supported
group-key protocol and governance in the transport spike before enabling federation.
Retain the current-key-only vault limit; do not silently provision multiple active
group keys or historical key rings to make a prototype work.

For a profile with one active shared federation group, all admitted holders of that
current key can decrypt its group traffic. Same-custody consensus within that group
does not make its payload private from other members. Operator-confidential Types
must refuse publication into the wrong group. A requirement for simultaneous
private operator/federation channels needs an explicitly reviewed transport/custody
profile, not an implicit expansion of these agents' assigned secrets.

Transport group membership and consensus voter membership are separate. A removed
voter cannot regain votes through a new node hash; a removed reader requires key
cutover as well as policy revocation. Key hashes remain correlatable identifiers,
not anonymisation, so public export still needs disclosure review.

## 8 Implementation and research acceptance

Update SBT01–SBT04 first: exact node-reference profile, canonical policy schema,
governed Type release, policy resolver and custody evidence. SBT09 adds partitioned
deterministic state. SBT10 has separately qualified operator and federation
adapters; SBT11 verifies both profiles. Operator-policy Types can qualify before
the federation implementation, while federation-policy Types remain held.

Required acceptance cases:

1. Reject sender/recipient/voter references supplied as operator labels, account
   numbers or bare transport IDs; reject a full key not matching its node hash.
2. Verify two different account hashes share an authenticated issuer without
   exposing the base key. Reject forged certificates and replayed challenges.
3. Commit/conflict/recover with three same-custody Raft voters; a two-node voter
   roster cannot claim majority availability with just one node remaining.
4. With four independent federation voters, inject one Byzantine creator, missing
   parents, conflicting events, withheld batches and partitions; retain evidence
   and prove no conflicting finality under the specified assumptions.
5. Show that many derived accounts from one issuer do not gain independent
   federation voting weight or count as a four-root fault-tolerance test.
6. Reject missing/unknown Type policies, stale Type definitions, changed policy
   digests, unsupported profiles and an operator proof substituted for a required
   federated proof. No automatic downgrade after a backend failure.
7. Reject a Type-policy update authorised only by its proposed new policy, and
   reject same-conflict writes submitted through an unrelated weaker Type.
8. Rebuild identical per-stream outcomes from trusted snapshots and retained
   inputs; preserve old policy proofs, node hashes and checkpoint lineage.
9. Verify native/SQL/transport crash boundaries and current-group-key-only restore
   safety. Packet capture finds no separate consensus network bypass.
10. Inspect canonical Type resolution, chosen profile, eligible node hashes,
    custody constraints, roster/epoch and outcome evidence in a sanitised real demo.

No throughput, resilience or production interoperability result is claimed for
this proposed implementation. The source-backed component investigation narrows
the experiments; actual acceptance requires running the selected versions.

## 9 Research record and compatibility boundary

Primary sources consulted on 23 October 2026 are linked at the relevant design
decisions above: the Raft paper, TiKV `raft-rs` and etcd/raft integration contracts,
the original Hashgraph technical report, the Narwhal/Tusk paper and original
repository, and Hiero's architecture document. Upstream default-branch documents
are research snapshots, not pinned deployment dependencies. No installed consensus
engine or supported Syn embedding API was verified by reading those documents.

Implementation facts were checked against Syn `57da1be8ee69e00375bb2756bb09839bf0d4bc06`
and Potbot's provisioning baseline `058612c66c2569750bb661f29092f745caba1ac4`;
the preceding plan-only head is `e59da9008af4e5639ff6e1f9441f6ad15a5e00aa`.
The existing Syn Type model has generic knowledge metadata; the Potbot Type DTO
has `metadata_json`. Neither implements this proposed first-class policy,
governed resolution or consensus enforcement. Existing receipt chains, hardened
derivation, membership and short-lived proof profiles remain unchanged.

The diagram is a generated vector illustration, rendered and visually inspected
for this design update. A screenshot or video of the proposed consensus network
would misrepresent its implementation state; actual synthetic demonstrations
remain acceptance deliverables, with relevant recording and transcript.
