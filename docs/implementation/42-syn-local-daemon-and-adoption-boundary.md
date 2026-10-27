# Phase 42 — SYN daemon discovery, authenticated local bridge, and public agent adoption receipts

**Implementation status:** Host-side Potbot integration **implemented and tested with a disposable synthetic SYN peer**. **No independently published SYN daemon is currently linked, installed or qualified.** Agent-mode P2P discovery, hardware attestation, private-key derivation, sealed key transfer and genuine OS-keystore custody remain SYN-owned prerequisites. No application starts a fake SYN implementation or falls back to Potbot's legacy password-encrypted vault.

## Architecture, authority and source of truth

The merged Phase 41 flow goes `signed pulse -> signed pledge -> selected inheritance/independence`. Phase 42 extends the public/read-only boundary to `discovered operator SYN -> authenticated local challenge -> verified external adoption receipt -> durable local receipt projection`. Phase 41's `selected` state remains the source of truth for *which* candidate was offered and chosen. The new receipt projection is *not* authorization to create or activate an agent or use an OS keystore.

The operator's root is **`m/6969'/420'`**, and installation base is **`m/6969'/420'/0'/0/0`**. SYN identifies its installation using `SHA256(raw 32-byte base public key)` only. An agent's 24-bit inheritance remains `uint24_be(SHA256(raw claimed hardware ID || operator_byte)[:3])`; the temporary key's hash is used for signed-offer verification only. Eight choices remain under `m/6969'/420'/inheritance'/{1..8}/0`. Phase 42 does not generate any of these keys.

## Local SYN discovery and connection

- A **trusted host operator** supplies an authoritative inventory file, not an untrusted mDNS advertisement, PATH scan, environment variable or user-controlled workspace. The registry is canonical JSON in a regular private owner-only 0600 file under a 0700 directory, never within an agent-writable sandbox. The entries pin *exact* Unix socket paths, mode (`operator` or `agent`), expected raw-base-public-key hash and host UID. The registry can contain up to sixteen entries; ambiguous operators and duplicate pins fail closed.
- On Linux/WSL2 the client checks the Unix socket inode and private directory, connects under a 3-second default bound, validates `SO_PEERCRED` against the expected UID, sends 32 **OS-CSPRNG** challenge bytes and an independent request ID. SYN must sign a versioned response with its base Ed25519 private key. Potbot verifies the response against the **previously pinned** 32-byte base public-key hash and echoes of the challenge/request ID. Merely connecting to a socket or seeing a self-consistent public-key/hash pair is insufficient.
- The client uses length-prefixed canonical JSON frames, maximum 64 KiB, bounded reads/timeouts, duplicate-key rejection, exact fields, signature domain separation and nonce/request binding. A single authenticated socket session supports **read-only** `adoption_receipt.get`. It does not accept signing, unlocking, credential export, P2P initiation, hardware IDs or generated code from the model.
- Native Windows named-pipe transport, SID/DACL qualification and macOS launchd/XPC custody require separate SYN-supported adapters; the POSIX socket must **fail closed** rather than masquerade as a Windows fallback.
- A trusted **empty** registry is a request to install a new operator SYN externally. A missing, corrupt, locked, inaccessible, ambiguous or mismatched registration **never** triggers silent installation or new entropy generation. Agent provisioning always requests a separate `agent`-mode SYN installation, even when an operator already lives on the same host. The planner cannot perform installation or pledge effects.

The **proposed** public local transport contract lives in `vocab/syn-local-daemon.schema.json`. Its exact signatures must be accepted and implemented by the external SYN maintainers before production use. This is not an assertion of compatibility with an existing SYN daemon.

## Selected-key proof and signed adoption

1. **SYN**, after privately importing the selected key to the new agent's protected OS keystore, presents a fresh 32-byte adoption challenge in an authenticated enrollment context. The new **agent-mode SYN** uses the *selected private key* to sign a public `SelectedKeyProof` binding the original pledge digest, pinned master, inheritance, independence, exact 32-byte selected public key, challenge and 1–300-second validity window.
2. **Operator-mode SYN** verifies the agent's proof and its own private authority/custody state, then signs an `AdoptionReceipt` binding the selected full public-key hash, proof digest, receipt time, `agent` mode and `syn-os-keystore` custody claim. Potbot verifies **both signatures**, digest matching and freshness; a three-byte abbreviated hash is never accepted as the final identity.
3. Potbot's `SynAdoptionLedger` atomically correlates that receipt to the **existing Phase 41 `selected` row** (matching master, offer SHA-256, 24-bit inheritance, index 1–8 and full 32-byte public-key hash). It stores public data in a separate SQLite table, rejects divergent repeats, and retains a replay-safe record after process restart. It does **not** change the Phase 41 `selected` state to a fictional `active` state.
4. Any agent home creation, worker credential binding or service permission **must additionally** verify the actual agent SYN instance's live base identity and external OS-keystore qualification. A signed operator *claim* is not independent evidence that the key resides in an OS keystore or was delivered confidentially. Potbot must never derive/import/export adopted private keys or treat the operator's ability to sign a proof as evidence of exclusive agent custody.

The operator possesses derivation authority under `m/6969'/420'` and could itself produce the selected descendant's signature. Therefore the cryptographic public proof alone **does not prove exclusive custody by an independently running agent**. Require real SYN agent-mode attestation and a SYN-qualified secure transfer/keystore process before activation. Agent hardware IDs are never present in this local read-only bridge or the receipt journal; the private P2P pledge route remains SYN-owned and encrypted end to end.

## SYN integration readiness and recovery

`SynStartupPlanner.potbot_first_run()` resolves the protected inventory and a live signed operator attestation. Its results mean `external_syn_pledge_required` (operator exists) or `external_syn_operator_install_required` (verified empty registry). `new_agent()` always means `external_syn_agent_install_required`. These are *intents*, not successful effects. A checked-in Python script prints a safe JSON summary:

```bash
PYTHONPATH=src python scripts/syn_phase42_preflight.py \
  --registry /var/lib/potbot/operators/syn-installations.json \
  --expected-master-hash <64-lowercase-hex>
```

It returns nonzero when setup/adoption is unavailable, and never emits a seed, hardware identifier or a private key. The registry is populated and maintained by the trusted native SYN installer, not by Potbot or an agent.

After a restart, an authenticated `adoption_receipt.get` lookup can complete a Phase 41 selected pledge. Receipt absence leaves the pledge selected/pending external SYN confirmation. Failure of authentication, signature, replay, matching or private journal storage **blocks** completion rather than initializing a new installation.

## Validation and required production gates

CI runs Phase 39–42 public contract tests on Linux with real ephemeral **test-only** Ed25519 signatures, private on-disk SQLite, concurrency, a real Unix socket test server, peer credential checks, missing-key/nonce/replay/receipt tampering, direct in-process adoption signature verification and signed receipt resumption. It also validates JSON Schema and compiles the affected modules. The disposable test server **is not SYN** and tests never qualify OS-key custody, secure P2P, live kernel attestation or private child-key migration.

Remaining:

1. Obtain or create SYN's independently installable daemon with **operator** and **agent** modes, OS-CSPRNG, OS-keystore protected base+root key pairs, signed local discovery inventory, secure P2P pulse/pledge transport and versioned API conformance.
2. Qualify actual SYN keystore custody, root `m/6969'/420'`, base `m/6969'/420'/0'/0/0`, adopted child derivation, confidential selected-key import and agent key possession after a separate process restart.
3. Adopt the actual native SYN local protocol (or update this proposed adapter with a versioned migration), including Windows named-pipe/Mac-specific secure channels and hardware-ID attestation methods.
4. Bind operator/agent SYN identities to `AgentHomeRegistry`, worker SYN heartbeat signing and workspace authorization **only after** cryptographically and operationally qualified adoption. Do not infer authority from an unsigned 24-bit inheritance value, abbreviated candidate hash or an unreviewed SQL row.
5. Add operator-mode backpressure, event journaling/retention/replay and coordinated restoration when the SYN daemon crashes mid-import, receives the selection but not its acknowledgement, or loses OS-keystore access. These effects require independent SYN custody reconciliation; do not automatically retry a possibly completed transfer.
6. Run whole-repository Ruff, mypy, PostgreSQL/RLS tests and full CI on the pinned merged commit. Offline tests are not a production qualification.
