# Agent identity, public verification and inherited Ember runtime

**Implementation addendum — 22 October 2026**

This implementation extends the hosted API and durable-job foundation at
`f02c7ba` and includes the previously prepared Potbot hierarchy extension. Existing
snapshot meanings and shipped migration histories are preserved; Ember persistence
is added as migration 13 after jobs 11 and hosted 12. The owner's instruction to
publish verification keys supersedes the hash-only disclosure rule in security
architecture 1.2.0.

## 1. What is implemented

Potbot can publish an Agent's actual Ed25519 verification key, create and verify
base-signed hourly delegation certificates, sign scoped typed payload digests
through a native local broker, and durably reject repeated accepted requests.
Types now declare inherited default Embers and required/optional instance Embers.
Scoped public and owner contract views are persisted separately in PostgreSQL.

The local custody profile is **`potbot-native-password-v1`**. Its version deliberately
distinguishes the implemented single-password vault from the larger multi-factor
vault design. Hardware wallets, passkeys, remote attestation, revocation/admission
services and unattended broker sessions are not silently represented as available.

The source implements Linux and Windows memory/file protection paths. A host must
pass native preflight before private operations. Public signature verification
requires only the Python `security` extra; it needs neither a vault, database,
model download nor private key.

## 2. Agent, Credential and locking Cog

An Agent is an autonomously acting entity or group operating under **one wallet
key hierarchy**. The runtime publishes three distinct objects:

| Object | Native kind | Assigned semantic Type | Meaning |
| --- | --- | --- | --- |
| Agent | `thing` | `synny://type/wallet-agent` or `wallet-agent-group` | The acting entity or coordinated group under one wallet hierarchy. |
| Credential | `credential` | `synny://type/agent-credential` | The authentication binding between Agent identity and the locking Cog. |
| Locking Cog | `cog` | `synny://type/ed25519-locking-cog` | The configured signing/verification computation, policy and operation binding. |

The Agent links to its Credential. The Credential links to its Agent and locking
Cog. The Cog links back to the Credential. All three bind the same public identity,
raw base verification key and stable base-key fingerprint. The owner view of the
Cog additionally requires a `secret://potbot/identity/<fingerprint>/hourly-sign`
operation handle. This handle contains no private key and is never a Type default.

The broker validates its encrypted policy independently. Registering graph objects,
changing a descriptive policy, possessing an operation-shaped string, or importing
implementation code cannot unlock a vault or authorize a signature. Signing through
`syn agent-security sign` resolves the owner contract and checks the complete
inherited implementation-Ember revisions against the current installation before
invoking the native broker with the selected vault and expected fingerprint. The
source policy is described in section 4. The broker separately enforces the policy
inside the encrypted vault.

`configured-cog` is an authored specialization of native `software-system` so an
idle configured computational entity has persistent identity. The imported native
`cog` class retains its historical `computational-process` parent. Use the native
kind filter `--primitive-kind cog` to enumerate all objects stored as Cogs; that
filter is independent of a particular Type's ancestry. An invocation remains a
separate Event. The unresolved historical Enigma/trust versus question mapping is
preserved rather than silently rewritten.

The opt-in security snapshot adds **25 security Types** to the **690-Type hierarchy**,
for **715 effective Types**. The default 576-Type seed is unchanged. Potbot-authored
names use `synny://type/...`; the corresponding `syn://type/...` is the same Type
identity within a selected catalog. Instance references continue to use native
`syn://thing/...`, `syn://credential/...`, `syn://cog/...` and `syn://ember/...` forms.

## 3. Public identity and signature protocol

| Document | Version | Public contents |
| --- | --- | --- |
| Public identity descriptor | 2 | Profile, raw 32-byte base Ed25519 verification key as canonical padded base64, and SHA-256 of those raw bytes. |
| Hourly key delegation | 1 | Base identity, hourly public key, epoch, exact epoch interval, fixed profiles and a base-key signature. |
| Signature envelope | 2 | Base identity, workspace, audience, domain, payload Type and exact SHA-256 digest, request UUID, issuance/expiry, epoch and hourly signature. |
| Public signature bundle | 1 | The complete identity descriptor, delegation certificate and signature envelope. |
| Ember contract bundle | 1 | Selected scope, Type contracts, exact Ember revisions, instance bindings and explicit public/owner projection. |

The stable Agent identity is `SHA-256(raw_base_public_key)`. A published key is
checked against an **independently trusted expected fingerprint**. A self-described
key and its self-signature cannot establish initial trust. Verification checks the
base-to-hourly certificate before checking the hourly payload signature, then
checks the caller's expected workspace, audience, domain, allowed Type, payload
digest, validity window and clock policy.

The key profile retains 32 bytes of original OS entropy, BIP39 English 24-word
recovery, CIP-3 Icarus derivation with the empty derivation password, and
Ed25519-BIP32 derivation scheme V2. The base path is `m/6969'/420'/0'/0/0`. The hourly
leaf is the base key's hardened `1'/E'` descendant, where `E=floor(POSIX_seconds/3600)`
and `0 <= E <= 2^31-1`. The signing broker chooses the current epoch; requests cannot
supply an arbitrary derivation path, seed, key or signing epoch.

Delegation certificates sign `potbot-hourly-delegation-v1` followed by a zero byte
and canonical certificate JSON without the signature field. Envelope signatures
use the existing `potbot-signature-v2` domain prefix. The protocol restricts values
to bounded ASCII strings, integers and objects, making sorted compact JSON an
unambiguous applicable canonical-JSON subset. Duplicate members, unexpected fields,
noncanonical base64, booleans masquerading as integers, unknown profiles and
unregistered payload Types are rejected. Type aliases compare by Type identity;
received signed bytes are never rewritten to make a signature pass.

Maximum message validity is 300 seconds and permitted verifier skew is at most
30 seconds. `identity verify` checks evidence without consuming it. `identity accept`
additionally atomically inserts a scoped replay key in a durable SQLite ledger.
Applications must use the acceptance path before granting a one-time action and
preserve that ledger across restarts. It does not itself implement the downstream
business action or replace workspace authorization.

## 4. Inherited Ember contracts

Each slot declares a stable property identity, the Ember's own Type, the Type of
its represented value or referent, cardinality, `effective` or `instance` origin,
public/private visibility, and `fixed`, `replace` or `append` override behavior.
Required and optional slots use minimum cardinality one and zero respectively.

The resolver checks an exact `(workspace, namespace, snapshot, catalog hash)` scope
and the selected catalog's parent DAG. Subtypes can narrow constraints, but cannot
weaken them. Diamond inheritance deduplicates identical revisions. Incompatible
parent defaults require an explicit compatible resolution. An explicitly empty
default remains distinguishable from a parent that never supplied one. A fixed
parent cannot be weakened by combining it with an additive parent.

The Ed25519 Cog Type supplies implementation Embers containing the exact bundled
Rust source files, `Cargo.toml`, `Cargo.lock`, `rust-toolchain.toml` and Python public
verifier source, with SHA-256 for each component. These defaults are inherited by
instances and provide inspectable source plus pinned, rebuildable inputs. They do
not promise byte-for-byte identical binaries across build environments.

Before `syn agent-security sign` invokes the broker, it requires every effective
implementation Ember to have inherited provenance. It then compares the complete
set of `(Ember reference, revision)` pairs with those generated from the installed
Python verifier and selected native source/build inputs. Missing, additional or
changed revisions fail this check. Historical or imported source declarations
remain available for descriptive resolution; using changed local source with the
semantic signing command requires a new matching immutable security view.

This is a **local source-content policy**. It establishes which source declarations
the semantic signing path accepts. It does not attest that the selected executable
was built from those inputs or establish correctness, custody qualification or
execution authority. An executable selected through `--broker PATH` still requires
independent trust and host qualification. Imported Ember code is never automatically
executed.

Public and owner views have independent canonical content identities. A public
view omits private values, private Ember identifiers, private binding revisions and
the owner bundle's digest. It reports `public_only` conformance; hidden private
requirements are not reported as missing owner values. Raw private keys, entropy,
recovery phrases and recognized secret fields are rejected by the contract view.
Arbitrary mislabeled prose cannot be proven nonsecret by a generic JSON validator;
producers remain responsible for classifying their own data correctly.

## 5. Database and deterministic inventory

**Migration 13** creates `syn_ember_public_bundles` and `syn_ember_owner_bundles`.
Both are immutable and refer to sealed SYN snapshots. Owner rows additionally bind
the current principal to the actual PostgreSQL login through restrictive row-level
security. The public projection has its own stored digest. Owner records are not
artifacts, embedding sources, vector inputs or public SYN payloads.

Imports validate the catalog fingerprint, every instance's direct Type, and the
existence and compatible Type of object-valued references in the selected snapshot.
Concurrent identical imports serialize on an owner-scoped advisory lock and reuse
the same result. New code or binding revisions create new immutable views.

Public Agent, Credential, Cog and Ember objects use the existing exhaustive
`syn list --type-ref ... [--include-subtypes]` inventory. `syn embers list` additionally
enumerates every matching Ember revision in a selected bundle, including private
operation Embers when explicitly requesting an authorized owner view. Its filter
is the **Ember's own Type**, distinct from `value_type_ref`. Ordering is deterministic
by reference and revision, with total counts and bounded limit/offset pagination.
Coverage is explicit: a selected sealed snapshot or a selected immutable bundle.
Similarity vectors do not determine Type membership or enumeration completeness.

## 6. Local use

The platform launchers already install all Python extras. For a development setup:

```bash
uv sync --locked --extra postgres --extra semantics --extra credentials --extra security --dev
uv run --locked potbot identity build
uv run --locked potbot identity preflight
```

Building needs Rust/rustup and the pinned toolchain in `native/potbot-identity/`.
`identity build` uses the pinned version, `Cargo.lock` and release mode, and reports
both source and executable digests. It builds outside installed package directories.
These inputs support rebuilding and inspecting a selected build; the digest report
is not binary attestation. An explicit trusted executable can be selected with
`--broker PATH`.

Create a public policy file using your existing workspace UUID:

```json
{
  "workspace_id": "00000000-0000-4000-8000-000000000001",
  "allowed_audiences": ["potbot-local"],
  "allowed_payload_types": ["syn://type/ember"],
  "allowed_domains": ["potbot.event.v2"]
}
```

```bash
potbot identity init --vault /absolute/private/identity.json --policy policy.json --output identity.public.json
potbot identity public --vault /absolute/private/identity.json --output identity.public.json
```

The native process prompts directly on its terminal. Python receives only the
public descriptor. Keep the resulting fingerprint through a trusted channel.
Use the corresponding absolute Windows path on Windows.

```bash
potbot identity sign --vault /absolute/private/identity.json --payload message.json --expected-signer-hash FINGERPRINT --workspace-id WORKSPACE --audience potbot-local --domain potbot.event.v2 --payload-type-ref syn://type/ember --output message.signature.json
potbot identity verify --bundle message.signature.json --payload message.json --expected-signer-hash FINGERPRINT --workspace-id WORKSPACE --audience potbot-local --domain potbot.event.v2 --payload-type-ref synny://type/ember
potbot identity accept --bundle message.signature.json --payload message.json --expected-signer-hash FINGERPRINT --workspace-id WORKSPACE --audience potbot-local --domain potbot.event.v2 --payload-type-ref syn://type/ember --replay-db accepted-requests.sqlite
```

`--catalog vocab/agent-security.snapshot.json` selects the trusted extended catalog
when signing or verifying payloads whose Types are outside the default catalog.
The expected payload Type is a caller policy choice, not a value blindly adopted
from an incoming signature.

To publish the Agent security graph and retain its owner contract:

```bash
potbot admin migrate
potbot syn agent-security register --workspace-id WORKSPACE --principal-id PRINCIPAL --repository-id REPOSITORY --namespace agent-security --identity-file identity.public.json --expect-signer FINGERPRINT --policy-file policy.json
```

Use `--group` for one Agent composed of cooperating actors under the same wallet.
The result includes snapshot and bundle IDs and the Agent/Credential/Cog references.
Registration reuses an identical public snapshot on retry and imports the scoped
owner bundle. It does not change native policy or create a key. If the second step
fails, the already-public immutable snapshot can safely be reused on retry.

```bash
potbot syn embers resolve --workspace-id WORKSPACE --principal-id PRINCIPAL --bundle-id BUNDLE --ref COG_REF
potbot syn embers list --workspace-id WORKSPACE --principal-id PRINCIPAL --bundle-id BUNDLE --type-ref synny://type/property-ember --include-subtypes
potbot syn agent-security sign --workspace-id WORKSPACE --principal-id PRINCIPAL --bundle-id BUNDLE --cog-ref COG_REF --vault /absolute/private/identity.json --payload message.json --payload-type-ref syn://type/ember --audience potbot-local --domain potbot.event.v2
```

Read commands default to the public projection. `--projection owner` explicitly
requests the principal-protected view. Offline contract validation is available as
`syn embers validate --file bundle.json --catalog-file snapshot.json`. For offline
Agent export, `syn agent-security export` requires an explicit workspace, namespace
and snapshot UUID and defaults to a public bundle.

Native `recovery`, `recover`, `change-password` and `backup` commands are exposed
through `identity`. Recovery words are handled only by native terminal input/output;
they never pass through Python or stdout JSON. Backups copy encrypted vault data;
this operation checks the public format but does not unlock or authenticate the
private contents. Likewise, `identity public` checks the public descriptor without
decrypting the vault. Recovery requires the expected original fingerprint, so an
incorrect phrase cannot silently create a different identity.

## 7. Custody and qualification boundary

The vault uses fixed Argon2id settings (64 MiB, three passes, four lanes), HKDF,
independent vault/data keys, AES-256 key wrapping and AES-256-GCM authenticated
encryption. It checks its encrypted identity against freshly derived keys on unlock.
Private operations run in a one-shot native process with guarded locked allocations,
a protected stack and checked OS file protections. There is no production switch
that substitutes pageable secret memory or weakens the password derivation.

The broker reserves **at least 96 MiB of locked-memory capacity**, allowing room for
the fixed 64 MiB Argon2 arena and other protected allocations. This implementation
session's Linux host exposes only an 8 MiB hard limit. The real release executable
therefore returns `ready: false` and exit 78 before private operations. Cryptographic
library tests use synthetic inputs and a test allocator; they are not evidence that
this host can safely unlock a production vault.

The retained production-boundary test invokes the actual native executable and
must be run explicitly on a qualified Linux host. It fails if preflight is not ready.
CI raises the test shell's locked-memory allowance and runs that gate. Windows code
must also pass native runtime qualification on the intended deployment, beyond a
successful cross-compilation check. Release/build/provider changes require renewed
qualification. This code does not claim formal verification or protection against a
compromised kernel, administrator, or malicious build toolchain.

This profile implements one password slot and one operation per native process.
Private operations unlock in that process, and decrypted wallet state is not retained
between operations. The resident vault-handle APIs and idle-memory AES sealing from
the broader design are not implemented by this one-shot tier. Multi-factor,
hardware-custody and unattended resident sessions require separate profiles.

New passwords must contain 12–1,024 UTF-8 bytes, contain no control characters and
be confirmed. Existing-password input is nonempty, rejects control characters and
is limited to 1,024 UTF-8 bytes.
The native process reads the controlling terminal directly. On Linux, native `init`
and `sign` also accept `--password-fd N` for an already-open private descriptor with
`N >= 3`, intended for a protected native supervisor or the disposable acceptance
test. The Python adapter exposes neither this input option nor password, recovery
phrase or raw-key APIs. Password rotation collects its new-password input before
unlocking the existing wallet.

The encrypted vault retains at most **1,024 live request-ID reservations**. On a
signing attempt, after policy and clock checks, the broker prunes a reservation only
when the current time is strictly later than its signed expiration plus 30 seconds.
A reservation remains live at that boundary. The broker then rejects a duplicate
live ID or a new request when 1,024 live reservations remain. It durably stores the
new encrypted reservation before releasing a signature. This is a bounded replay
window, so an expired and pruned request ID is not permanently reserved.

Encrypted local epoch/replay state catches local duplicate requests and clock
rollback relative to the state being used. It is not hardware-anchored anti-rollback
protection against restoring an older vault backup. Preserve the independent verifier
replay ledger. Changing a password does not revoke copies of an older encrypted
backup or change the wallet identity; recovery and revocation are separate concerns.

## 8. Source and validation map

| Concern | Implementation / contract |
| --- | --- |
| Public verification and replay acceptance | `src/potbot/security/public.py` and `vocab/security-public/` |
| Public-only native adapter and CLI | `src/potbot/security/broker.py`, `security/cli.py` |
| Native derivation, vault and OS boundary | `native/potbot-identity/` |
| Inherited contracts and projections | `src/potbot/semantics/embers.py`, `vocab/ember-contract-bundle.schema.json` |
| Scoped persistence and migration 13 | `semantics/ember_service.py`, `persistence/ember_schema.py` |
| Agent graph, code defaults and signing integration | `semantics/agent_security.py`, `agent_security_cli.py` |
| Extended Type registry | `vocab/agent-security.snapshot.json` |

Tests cover independent published Ed25519/Icarus vectors, Rust-to-Python signature
interoperability, tampering, scope and Type mismatches, boundary times and replay,
vault authentication and mutation, inherited contracts and conflicts, redaction,
exact inventory, immutable persistence, concurrency and genuine PostgreSQL role
isolation. `scripts/check.py` verifies Python source, installed-wheel resources,
the registry assets and the selected application integration suites. Native CI
adds both platform builds and the explicit production-boundary gate.

### Recorded verification results

The completed checks below apply to the integrated `f02c7ba` checkout with Ember
migration 13:

| Gate | Observed result |
| --- | --- |
| Full `scripts/check.py` gate | Unit/Git selection: **1,514 passed, 315 deselected**, in **48.76 seconds**. Lint, formatting of 247 files, mypy over 127 source files, generated assets, and clean wheel/install/help/resource/catalog checks passed. The installed `0.5.0.dev2` wheel passed the `serve`, `jobs` and `identity` command checks. |
| Full PostgreSQL selection, `pytest -m postgres` | **309 passed, 1,520 deselected**, exit 0, in **339.26 seconds**. The fixture used real restricted PostgreSQL logins and row-level security, including the hosted API and durable-job integration suites. |
| Strengthened `tests/integration/test_phase4_upgrade.py` | **3 passed within the full PostgreSQL run**. Both populated main and phase3-preview histories reach revision 13, retain prior data and migration records, and grant the existing application login access to both new Ember tables. The tests explicitly retain jobs at 11 and hosted at 12; modified historical checksums are still rejected. |

The Python gates each emitted one upstream Starlette TestClient deprecation warning.
The database run retained its sanitized output and JUnit report as
`artifacts/postgres-integration.log` and `artifacts/postgres-integration.xml`.
The update archive includes copies in its `verification/` directory. The
[verification record](agent-identity-and-ember-verification.json) records these checks
and their provenance. Deployment also requires the native production-boundary
qualification described in section 7. In this implementation environment, the actual
native private-operation path refused to proceed with an insufficient memory-lock
allowance.
