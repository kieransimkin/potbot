# Phase 41 — SYN operator enrollment: signed pulses, candidates and durable replay fencing

**Status:** implemented **public-only protocol verification and operator-private
transaction journal**; pending **real SYN daemon**, encrypted P2P, key custody,
hardware attestation, candidate private-key adoption and live-host qualification.
This is a bridge for an independently installed SYN authority, **not** a Potbot
identity provider and **not** a complete agent activation workflow.

## Identity rules retained unchanged

- Operator SYN installation identity: `SHA256(raw 32-byte base public key)`.
- Operator root: `m/6969'/420'` (both indices hardened).
- Operator base: `m/6969'/420'/0'/0/0`.
- Temporary agent Ed25519 key: CSPRNG entropy, private key owned by its **separate
  agent-mode SYN installation** in its OS-protected keystore.
- Inheritance: `uint24_be(SHA256(exact signed raw hardware ID || chosen operator octet)[:3])`.
  Never use the temporary public-key hash for inheritance.
- Eight public choices: `m/6969'/420'/inheritance'/1/0` through `/8/0`.
  The **independence** is the chosen index from 1 to 8; the 3-byte public-hash
  prefix is only an abbreviation, **not** a secure identity.
- SYN alone derives/signs/holds private installation and adopted agent keys. Potbot
  receives no entropy, mnemonic, chain code, keystore password or private key.

## Authorized transport and wire sequence

1. **SYN operator broadcasts a signed pulse**
   (`synny://type/syn-operator-pulse-v1`), binding its stable base public-key
   hash, network ID, a fresh cryptographically random 32-byte challenge,
   issuance and expiration (at most 300 seconds). Its signature is verified
   against the *operator's independently pinned base public key*, not solely an
   arbitrary key carried in the frame. The same genuine broadcast pulse **may
   be used by many distinct agents**. Its challenge is NOT a one-use token.
2. **Agent SYN selects an operator**, establishes a mutually authenticated,
   confidential, operator-directed channel and submits the **v2 signed pledge**
   with its claimed hardware ID in the encrypted message. A valid signature
   proves control of the disposable signing key but does **not** prove hardware
   ownership. The hardware ID is never gossiped or logged.
3. **SYN operator attests and privately blinds the hardware identity**. A
   trusted SYN API returns an opaque HMAC/PRF-backed 32-byte device handle; the
   operator-local journal never writes the raw identifier or plain hash of a
   low-entropy MAC. Failure to attest/qualify an identity **denies enrollment**.
   The V40 candidate receipt does contain SHA-256 of the entire signed pledge
   preimage, which commits to its hardware ID. That commitment does **not**
   guarantee hardware privacy against offline guessing of low-entropy IDs;
   operator replies must stay on the confidential agent-directed channel.
   An operator SQLite file is protected by a private `0700` operator directory,
   regular `0600` journal and disjoint sandbox boundaries. Windows-native
   fallback is NOT silently provided without a qualified SYN-owned store.
4. **First-use ledger transaction** atomically reserves the full signed-offer
   SHA-256 and a unique opaque device handle per master. It retains the agent's
   public verification key, pulse challenge, target master and expiry. Signed
   offers, not the broadcast challenge, are single use. A crash/retry must not
   mint a second admission, even when the temporary signing key is rotated.
5. **External owner approval** via SYN is required before operator-byte
   selection and derived-key operations. The operator chooses its one byte;
   collision with an already reserved 24-bit inheritance fails closed and can
   be retried with **another authorized byte**. No automatic reuse of an
   expired or rejected inheritance; reconciliation requires an explicit SYN
   lifecycle decision.
6. **SYN derives and signs eight candidate public hashes**; this bridge
   verifies the operator's base-key signature over
   `b'syn.agent.candidates.v2\x00' + canonical_json(candidates)` before
   atomically publishing a candidate offer. Candidate public messages expose
   no raw hardware identifier or temporary signing-key hash. The signed
   candidate digest is `SHA256(signing preimage)` and includes the original
   signed-pledge preimage hash, master, operator octet and inheritance.
7. **Agent SYN signs independence selection** over
   `b'syn.agent.independence.v2\x00' + canonical_json(selection)`. Selection
   binds the exact signed candidate digest, master, inheritance, choice index
   and chosen 3-byte hash. The ledger verifies both signatures, retrieves the
   full 32-byte chosen public-key hash from its private mapping and commits
   `selected` exactly once.
8. **Stop at `selected`, not `active`.** Actual custody transfer, the agent's
   proof of possession of the adopted private key, secure keystore import,
   durable attestation and revocation remain a separate SYN-owned protocol.
   A selected receipt is not approval to execute as the inherited identity.

### Signature domains and encoding

All signatures cover their specific domain-prefixed **canonical ASCII JSON**,
with sorted keys, compact separators and exact typed fields. See
`src/potbot/domain/syn_enrollment_protocol.py` and
`vocab/syn-agent-operator-enrollment.schema.json`. Reinterpreting a historical
v1 pledge as v2, signing an unsalted/generic message, accepting a noncanonical
hash or trusting an unsigned candidate list is forbidden. Timestamp checking
uses trusted operator time and explicit monotonic/audit policy.

The public protocol is an intentionally **bounded v2-compatible supplement**
to `vocab/syn-agent-pledge.schema.json`; it does not replace the existing V40
wire model. The operator journal is not a new P2P listener, HTTP enrollment
endpoint or keystore.

## Runtime integration contract

`SynEnrollmentLedger` needs an exact operator `master_hash`, network ID,
private file path and a **host-configured** `SynOperatorAuthority` implementing:

- `attest_and_blind_device(hardware_id) -> bytes[32]`: enforce qualified
  hardware/owner policy and return SYN-secret-keyed handle;
- `authorize_offer(offer_sha256) -> bool`: actual user/operator approval;
- `derive_candidate_public_hashes(inheritance) -> tuple[bytes[32],...x8]`:
  SYN derivation under `m/6969'/420'` without releasing secrets;
- `operator_public_key() -> bytes[32]`: independently pinned operator base
  verification key, and `sign_candidates(preimage) -> bytes[64]`:
  SYN-owned OS keystore signing operation.

This interface must be implemented only through an authenticated local SYN
installation, not a model-selected Python callback, HTTP-supplied code or a
synthetic key generator. Public Ed25519 verification is included; it does not
make an untrusted implementation of the authority interface acceptable.

**No automatic Potbot startup behavior changes in this patch.** Before wiring
production startup, SYN must provide discovery of existing installations,
pledge versus new-install selection, conflict handling for locked/multiple
installations and a verified OS-keystore-backed identity interface. Until then
an unavailable SYN service is a blocked dependency, never permission for
Potbot to create and own an unrelated identity.

## Qualification and remaining work

Focused offline tests verify real disposable Ed25519 signatures; strict
pulse/operator/network/timestamp checking; real SQLite rollback, restart and
concurrent first-use fencing; hardware non-disclosure; collision and short-hash
ambiguity; signed candidate/selection tampering; and an intentionally mocked
SYN authority. They do **not** prove device authenticity, real SYN key
custody, Linux/WSL isolation, transport confidentiality or interoperability.

Production remains blocked on:

1. An independently installable SYN service with agent/operator mode, verified
   local discovery, OS CSPRNG and **OS-keystore** custody (never legacy Potbot
   encrypted file vault as a hidden substitute).
2. Signed pulse P2P transport, owner-approved inbound encrypted pledge channel,
   challenge freshness, connection-bound admission and scoped rate limiting.
3. Hardware-ID collection and attestation policy (MACs may be randomized,
   spoofed or absent), secure HMAC key rotation and privacy/retention controls.
4. Full candidate derivation and safe selected-private-key provisioning, proof
   of possession and revocation under a reviewed hardened/non-hardened policy.
5. A qualified host-backed persistent store (including Windows), schema
   migrations, adversarial concurrent enrollments and backup/restore/rollback
   tests. SQLite's local transaction journal is not globally replicated and
   requires qualified protection and recovery.
6. Explicit agent-home provisioning only *after* SYN confirms adopted-key
   custody. Wire the resulting verified SYN identity into worker heartbeat
   signing and monitoring without exposing operator root credentials.

Use `PYTHONPATH=src python -m pytest -q tests/unit/test_syn_agent_pledge.py
 tests/unit/test_syn_enrollment_ledger.py` for the offline contract gate.
