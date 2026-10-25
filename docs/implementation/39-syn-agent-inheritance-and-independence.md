# SYN agent-mode provisioning — inheritance and independence (historical v1 contract)

**Status:** Historical reference. **Superseded for new enrollment by [the hardware-identifier v2 contract](40-syn-agent-hardware-inheritance.md).** Do not implement the temporary-key-hash inheritance formula below for newly provisioned agents. New owner-specified requirements plus a public calculation/verifier library; **not a running SYN node or an activated enrollment API**. Supersedes the earlier provisional `m/6969/420'` root path (the first index **is hardened**). This document extends the SYN-ownership specification in Phase 38; it does not authorize Potbot to derive, store, unwrap, transmit, or invent private keys.

## 1. Installation identities and custody

SYN alone owns identity generation, signing, recovery and **OS-protected key custody**. A conventional Potbot installer searches for a trusted **local operator-mode SYN installation** and pledges to it, creating a fresh operator-mode SYN installation only after positively establishing that none exists. Locked or ambiguous discovery fails closed.

Agent provisioning is distinct: **every new agent has its own agent-mode SYN installation**, with its own fresh local temporary key and P2P node lifecycle; it discovers an existing operator over a verified, signed pulse protocol before it can be endowed with an inherited operator key. No existing SYN installation is silently reused as an unrelated new agent identity.

An initial, independently created operator SYN installation obtains **256 bits of entropy from the OS CSPRNG** and derives these separate extended Ed25519-BIP32 V2 keypairs using the existing CIP-3 Icarus profile:

| Role | Exact path | Use |
| --- | --- | --- |
| Operator/SYN installation **base address** | `m/6969'/420'/0'/0/0` | Installation's permanent public identity: lowercase hex `SHA-256(raw 32-byte base public key)` |
| Operator/SYN **root address** | `m/6969'/420'` | Parent for inherited agent keys; private material is operator-only |

The root path is **`m/6969'/420'`, not `m/6969/420'`**. Its first and second indices are both hardened. The Icarus master `m` is **not** the root *address* key. SYN persists both derived keypairs and required chain codes inside the OS-protected keystore; neither Potbot nor an agent obtains the operator root private key. Existing Potbot file-vault identities require separately authorized, fingerprint-preserving migration.

## 2. Agent-mode P2P discovery and signed pledge offer

1. Provision a fresh, isolated **agent-mode SYN installation** inside that agent's workspace/UID sandbox. SYN creates a temporary **signing keypair** backed by strong randomness and protects its private key in its OS credential store. SYN may bind a salted commitment to available hardware identifiers/MAC addresses for local device continuity, but **hardware identifiers are public/spoofable, sometimes absent or unstable, and must never be the sole secret entropy**. A hardware-ID hash alone is not a signing key or a signature-verifiable public key hash. Do not broadcast unredacted MACs, hardware IDs, serial numbers or stable cross-workspace fingerprints.
2. Join the specifically authorized P2P discovery network (under the operator's sandbox/egress policy). Listen for **authenticated operator pulse** broadcasts. A pulse identifies the operator by the **base address public key hash** and supplies a fresh cryptographically random **32-byte challenge**. A bare unauthenticated advertisement is not a suitable operator.
3. Choose a suitable operator. Construct the agent's pledge offer with the three requested identity fields: (a) `temporary_public_key_hash = SHA-256(raw 32-byte temporary Ed25519 public key)`; (b) an exact Unix-seconds timestamp; and (c) the target operator's **base public key hash** (the proposed master). For replay protection, bind the fresh operator pulse challenge into the **same signed transcript**. A public temporary verification key must accompany the offer so the receiver can verify the signature; its computed hash must match (a).
4. The agent signs the domain-separated canonical offer with its *temporary* Ed25519 private key. The operator checks source identity, target master, signature, live pulse challenge, bounded timestamp skew, policy and a **durable, atomically claimed replay key** before considering acceptance. The offer is only a request to become a servant: it does **not** imply acceptance, pledge finality or key transfer.
5. Decline/rejection never yields operator-derived key material. The operator may require explicit owner approval, device attestation, capability/quotas and an audit reason before accepting an agent.

Public signing preimage for the version-one offer is UTF-8 bytes `syn.agent.pledge.offer.v1\0` followed by the JSON object of `PledgeOffer.to_dict()` serialized with sorted keys, ASCII and `(',', ':')` separators. `timestamp_unix_seconds` is a JSON integer; hashes and challenge are lowercase hex. This is a deliberately versioned proposal. SYN must implement the signer and a trusted, durable replay store; Potbot only has a **public verifier**. The optional `cryptography` security extra is required to verify an Ed25519 signature through Potbot.

## 3. Operator-chosen one-byte salt and inheritance (24 bits)

On acceptance, the operator SYN instance takes **exactly the 32 raw bytes** of the advertised temporary public key hash, appends **one operator-chosen byte** (0 through 255), computes SHA-256 of that 33-byte string, and takes **the first three digest bytes**:

```text
Htemp          = SHA256(agent_temporary_raw_public_key)  // exactly 32 bytes
operator_byte  = one octet 0x00 .. 0xff
inheritance3   = SHA256(Htemp || operator_byte)[:3]
inheritance    = uint24_big_endian(inheritance3)       // 0 .. 16,777,215
```

Reference vectors for a **synthetic public-key hash** of `000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f` (not a real device or signing key):

| Operator byte | 3-byte digest prefix | Inheritance (unsigned big-endian) |
| --- | --- | --- |
| `00` | `1b170d` | `1775373` |
| `a5` | `562dac` | `5647788` |
| `ff` | `bdb741` | `12433217` |

The unsigned 24-bit value is the agent's **inheritance**. Do **not** take a modulo of an arbitrarily sized integer, reinterpret 24-bit hex as signed, use little-endian byte order or hash the ASCII hex string. SHA-256 truncation yields a uniform 24-bit abbreviation under the standard cryptographic-hash assumption. This is a publicly reproducible identifier, **not** a signature, capability, encryption key or collision-resistant global identity.

SYN must atomically reserve `(operator_base_identity, inheritance)` in its protected durable registry before issuing choices. On collision, the operator can choose another of its 256 possible bytes; if none are available, provisioning is **rejected/indeterminate** pending a new offer/policy—not silently assigned a duplicate. Inheritance values are only 24 bits: collisions become plausible at scale, so full key hashes, signed authorization and canonical run identities remain required. The chosen byte and resulting integer must be retained in the signed operator acceptance/audit record.

## 4. Eight operator-derived candidate keypairs

For the reserved inheritance integer `I`, **operator SYN** derives eight independent extended Ed25519 keypairs under the **operator root address**, at the exact paths:

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

Only `I` is variable in the hardened third step. The **last two indices are non-hardened**. They are **not descendants of the base leaf** `m/6969'/420'/0'/0/0` despite the shared first two indices.

For each candidate public key, compute its **full** public key hash as `SHA256(raw 32-byte Ed25519 public key)` and **reduce to exactly three bytes by taking the first three bytes**. The eight 3-byte hashes are offered to the agent, in path order `1..8` and bound to the target master, inherited index, temp hash, chosen operator byte, authenticated pulse/offer and a fresh operator signature. The candidate list must contain eight **distinct** short hashes. If two collide, abort this offer and choose a different available inheritance rather than offering ambiguous identities. The operator must independently verify actual Ed25519-BIP32 derived paths; an arbitrary list of eight hashes is not derivation evidence.

The agent selects an index **1 through 8**, called its **independence** (or *chosen parameter*). SYN records both the index and the selected 3-byte public-key hash. The selected **full 32-byte hash** is the agent's canonical cryptographic identity; the 3-byte form is only a short label. Nothing in this public contract infers an unpublished agent subpath or alternative root path.

## 5. Ownership/acceptance/recovery boundaries still to be specified

**Crucial key-custody question:** the eight private candidate keys originate from the *operator's* root key. To let an agent use its chosen key independently, SYN must specify a safe operator-authorized **chosen-private-key provisioning mechanism**: e.g. mutually authenticated, ephemeral ECDH/HPKE-wrapped delivery directly into that agent's OS keystore, *or* signing delegation without exporting the key. The operator's root/other candidate keys and chain codes must never be transmitted. Because the final suffixes are non-hardened, a review must assess interactions between exposed child private keys and extended public keys; the operator's root and other agent branches must stay protected. No private key may appear in public offers, process arguments, logs, application memory dumps, untrusted model context or raw SYN telemetry.

The new agent needs a signed final acceptance certificate binding its temporary identity, operator base identity, inherited integer and salt, independence index, **full** selected public key hash, chosen key path, timestamp, scope/expiration/revocation and custody receipt. SYN must retain an atomic index and be able to reconcile partial delivery or agent crashes. A short hash, unverified pledge, or operator pulse alone never proves key possession or successful identity installation.

**Open owner decision:** This contract interprets *independence* as the selected choice **index `1..8`**, and separately records the chosen 3-byte public-key hash. If *independence* is meant to be the 24-bit selected hash itself instead, amend the versioned wire contract before enabling provisioning. Other missing production decisions: which SYN source/runtime to install, authenticated P2P pulse transport, local hardware binding/attestation, acceptance policy and revocation, secret-key delivery vs delegated signing, and how an agent proves possession of its adopted key.

## 6. What Potbot implements in this increment

- `src/potbot/domain/syn_agent_pledge.py`: **public-only** hashing, inheritance conversion, exact eight derivation-path builders, offer canonicalization and optional Ed25519 verification, and duplicate-choice rejection. No signing or private derivation.
- `vocab/syn-agent-pledge.schema.json`: draft versioned offer, operator candidate announcement and agent choice envelopes. A schema-valid packet has **no authority** absent trusted SYN signatures, replay store, source identity and fresh operator challenge.
- `tests/unit/test_syn_agent_pledge.py`: deterministic vectors, exact hardened/non-hardened hierarchy, unsigned 24-bit boundaries, 256 salt options, 8-choice validation, canonical offer signing and invalid-master/challenge/expiry/signature rejection.

Not provided: a standalone SYN runtime, P2P pulse listener, OS-keystore integration, signed operator acceptance, candidate private-key derivation or delivery, agent `Syn` instance installer, production replay/durable uniqueness registry, or new default Potbot startup behavior. All are **SYN-owned future integration**, and none may be silently simulated as an approved/successful enrollment. The existing Potbot encrypted file vault is legacy and must not be substituted for SYN OS-keystore custody.
