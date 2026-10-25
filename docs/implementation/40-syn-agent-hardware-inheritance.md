# SYN agent pledge v2 — CSPRNG temporary identity, hardware-based inheritance

**Status:** updated owner specification and *public-only* protocol contract. This supersedes [v1](39-syn-agent-inheritance-and-independence.md) for new enrollment. It **does not** implement a running SYN node, claim hardware attestation, make key material available to Potbot, or complete P2P pledging. Historical v1 signed transcripts must retain their original interpretation and never be silently reinterpreted as v2.

## Identity custody and key generation

SYN is the sole authority for key generation, identity, secure signing, OS-protected keystore, operator acceptance, and key adoption. The independently created operator SYN derives its **root address** from `m/6969'/420'` and its distinct, stable **base address** from `m/6969'/420'/0'/0/0`. The installation is identified by `SHA-256(raw 32-byte base public key)`; the operator root private key never leaves SYN.

Every newly provisioned agent starts a separate **agent-mode SYN installation** and generates a fresh **temporary Ed25519 signing keypair using genuine OS cryptographically secure random entropy** (`getrandom`/OS CSPRNG or an equivalently qualified hardware RNG), with private key protected by the operating system's keystore. It must not derive temporary key entropy from MAC addresses, machine IDs, hardware serials, timestamps, hostnames, process IDs or their hashes. Identical hardware IDs on different machines must not produce identical temporary keys; regenerating the temporary key must not change inheritance for the same hardware identifier and operator byte.

The agent independently reads its **hardware unique identifier** from SYN's qualified host integration. Its *canonical raw bytes*, rather than a textual formatting or temporary-key hash, are the new inheritance input. A six-byte MAC address is representable, but MACs may be randomised, duplicated or spoofed. SYN must define trusted collection, stability, permitted sources, multiple-adapter selection, replacement policy and device attestation before using hardware identity as authentication. The public Potbot contract validates 6–256 bytes only: it does **not** establish unique physical-device ownership. Missing/unqualified hardware ID **blocks** pledging until an authorized policy is specified; do not fall back to a temporary key hash.

## V2 signed offer and operator verification

1. Agent-mode SYN discovers an authenticated operator pulse with target operator base-public-key SHA-256 and fresh 32-byte challenge.
2. SYN constructs a `synny://type/syn-agent-pledge-offer-v2` containing:
   - `hardware_unique_id`: **the exact hardware ID bytes encoded as lowercase hex** (not hashed, and not ASCII bytes of a MAC string);
   - `temporary_public_key_hash`: SHA-256 of the agent's 32-byte Ed25519 temporary *public* key, used **only to verify that the public key accompanying the offer matches the signing key**;
   - `timestamp_unix_seconds`, `target_master_public_key_hash`, and `pulse_challenge`.
3. The agent signs `b"syn.agent.pledge.offer.v2\x00" + canonical_json(offer)`, with sorted keys, ASCII and compact separators, using its fresh CSPRNG-generated temporary private key. The outer envelope includes the full temporary raw 32-byte public key and 64-byte Ed25519 signature, encoded as lowercase hex.
4. The operator compares target master and current authenticated pulse; validates freshness, Ed25519 signature and advertised public-key hash; verifies policy/hardware-attestation requirements and atomically reserves a replay token. **A valid signature proves control of the temporary signing key, not authenticity of the hardware ID.**

The hardware ID is sensitive linkable device metadata. It is sent **only in the encrypted, authenticated, operator-directed pledge request**, never in public pulse broadcasts, broad gossip, general telemetry, audit output, or model prompts. Signed requests do not automatically provide confidentiality. Apply explicit retention limits and authorization at the operator; even a SHA-256 digest of a stable MAC can permit linkage or dictionary attacks.

## V2 inheritance: exact calculation

On authorized acceptance, the operator chooses one octet `operator_byte` between 0 and 255 and reduces **the raw hardware identifier**, not the public-key hash:

```text
hardware_id       = exact bytes in the verified v2 signed pledge
operator_byte     = one chosen unsigned octet
inheritance_bytes = SHA256(hardware_id || operator_byte)[:3]
inheritance       = uint24_big_endian(inheritance_bytes)   // 0 ... 16,777,215
```

No string formatting, UTF-8 of hex, size-prefix, additional salt, modulo reduction or change in hashing algorithm is implicit. A separate protocol version is required to change this formula. SHA-256 truncation has uniform 24-bit outputs under normal hash assumptions, but the small space is not globally collision-resistant. The operator must atomically reserve inheritance per operator installation; after collision it may try another of its 256 operator bytes. An exhausted or unqualified enrollment **does not reuse another agent's inheritance**. Identical hardware ID and operator byte intentionally produce identical inheritance regardless of the temporary key.

For a *synthetic 32-byte hardware identifier* `000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f`, the SHA-256 prefix is `1b170d` for byte `00`, `562dac` for `a5`, and `bdb741` for `ff`. These were also the old v1 public-hash vectors because the test inputs were the same **bytes**, not because a public-key hash remains an input.

## Eight agent choices / independence

Once inheritance `I` is reserved, operator SYN derives exactly eight Ed25519-BIP32 V2 candidate keypairs beneath the **operator root**:

```text
m/6969'/420'/I'/1/0
m/6969'/420'/I'/2/0
m/6969'/420'/I'/3/0
m/6969'/420'/I'/4/0
m/6969'/420'/I'/5/0
m/6969'/420'/I'/6/0
m/6969'/420'/I'/7/0
m/6969'/420'/I'/8/0
```

For each key, SYN takes the first three bytes of SHA-256 of the raw 32-byte public verification key. The eight abbreviations must be distinct. The agent chooses one *index* from 1–8, its **independence**; full public-key identities remain authoritative. Candidate packets use `synny://type/syn-agent-identity-candidates-v2` and bind their operator-byte and inheritance to `pledge_offer_sha256 = SHA256(the domain-prefixed v2 signed-offer preimage)`. They **do not repeat either the hardware ID or temporary public-key hash**. Selection packets use `synny://type/syn-agent-independence-selection-v2` and bind the selected short hash to the signed candidates and temporary signature.

SYN still needs independent candidate key derivation/custody, operator signatures, replay/uniqueness ledger, adoption proof, revocation and safe delegation or secure selected-key delivery. The operator's root key, other candidate private keys, chain codes and entropy must never be sent to the agent. The selected key's private custody and any non-hardened public/child derivation safety need a dedicated security review.

## Code changes and release policy

- `src/potbot/domain/syn_agent_pledge.py` now validates the v2 signed offer and calculates inheritance from raw `hardware_unique_id` bytes. It **does not** provision keys or register devices.
- `vocab/syn-agent-pledge.schema.json` is now the v2 public schema; immutable historical v1 schema remains at `vocab/syn-agent-pledge-v1.schema.json`. The v2 verifier rejects v1 offers to avoid ambiguous signature/identity interpretation.
- `tests/unit/test_syn_agent_pledge.py` covers hardware-ID bounds, key-hash separation, signed hardware claim tampering, deterministic vectors, 8 choices, v1 rejection and v2 JSON Schema shapes.

Deployment of a real SYN installer, encrypted P2P transport, OS keystore and hardware attestation remains **blocked on SYN-owned runtime/API availability**. Potbot must not silently substitute its file vault, derive the temporary key from hardware bytes, or mark a v2 pledge accepted merely because the public schema or signature checks pass.
