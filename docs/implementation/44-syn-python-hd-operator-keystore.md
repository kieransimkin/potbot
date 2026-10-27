# Phase 44 — SYN Python HD operator identity and OS-keystore custody

**Boundary:** SYN remains a standalone **Python** service. There is no new Rust component, Potbot identity server, or independent private-key process. This development increment adds a SYN-local operator identity provider and leaves the existing Phase 43 legacy native-signature adapter available for explicitly configured v1 installations.

## Required identity hierarchy

- Generate **32 fresh bytes** using the OS cryptographically secure random source when creating a new operator installation. Do not derive entropy from MACs or host serials.
- Derive with **CIP-3 Icarus** (empty spending passphrase, PBKDF2-HMAC-SHA-512) and **Ed25519-BIP32 V2**, not ordinary seed-based Ed25519 or SLIP-0010.
- Root identity derivation: `m/6969'/420'`.
- Permanent base identity derivation: `m/6969'/420'/0'/0/0`.
- SYN installation ID: `SHA256(raw 32-byte base public key)`. The root key's SHA-256 is separately retained for diagnostics, **never substituted** for that installation ID.
- Given an authorized 24-bit inheritance `I`, SYN computes eight **public** candidate hashes under `m/6969'/420'/I'/1/0` through `/8/0`. The temporary agent signing key plays no role in inheritance; the Phase 40 hardware-ID formula is unchanged.
- Both root and base public keys, extended private keys and chain codes live in the platform's **native OS credential service**. The original installation entropy is also stored in the same backend for deterministic recovery and independently checked derivation. No secrets are included in public CLI output, daemon JSON or Potbot receipts.

## Python implementation

`src/syn_node/hd_keys.py` is the SYN-owned authority. It delegates all HD arithmetic and extended signing to [PyCardano](https://pypi.org/project/pycardano/0.19.2/) **version 0.19.2 exactly** (`syn-node[hd]` optional extra). The package refuses missing or mismatched versions. A provider capable only of constructing a standard 32-byte Ed25519 seed is **not** a compatible substitute.

An explicit `new-operator` command creates a fresh key hierarchy **only if every keystore slot is empty**. A SYN-private, Linux `flock`-protected directory serializes initialization. The operator stores the random entropy plus root/base private/public/chain records, checking readback for each slot. The manifest is written **last**. If an OS credential write fails after any earlier write, the installation is **incomplete**: subsequent calls refuse to generate new entropy and do not overwrite it. Recovery of partial installations requires a separate operator-authenticated workflow. This is not a general multi-key transaction across OS keyrings.

On each public read, candidate derivation or signature, SYN re-derives the expected root/base from the stored entropy, compares all records and the commit marker and verifies its pinned base hash. It verifies each Ed25519 signature against the derived base public key before returning it. This intentionally prefers consistency over performance and does **not** claim Python secret memory is non-pageable or hardware non-extractable.

## Installation and use (Linux/qualified WSL2 host)

```bash
python -m pip install -e './syn-node[hd]'
# The OS Secret Service must be installed, available and unlocked.
# Create and chmod a SYN-owned 0700 lock directory outside Potbot sandboxes.
syn-node new-operator \
  --keyring-service syn.operator \
  --keyring-account my-operator \
  --lock-dir /absolute/owner-private/syn-locks
```

The returned JSON is **public only**: both derivation paths, two raw public keys, their full SHA-256 hashes and the profile revision. The `base_public_key_hash` must be pinned independently by the installation registry.

Configure Phase 43 daemon **version 2** (operator-private `0600` JSON) and use the **existing** `preflight` and `serve` commands:

```json
{"schema_version":2,"type_ref":"synny://type/syn-local-service-config-v2","mode":"operator","socket":"/absolute/syn/operator.sock","registry":"/absolute/syn/installations.json","receipt_db":"/absolute/syn/adoptions.sqlite3","expected_base_public_key_hash":"<64 lowercase hex from new-operator>","keyring_service":"syn.operator","keyring_account":"my-operator"}
```

Version 2 also supports `mode=agent` using the pre-existing OS-keystore **temporary agent key**, with the expected hash set to that temporary public key. A separate agent-mode installation is still required for each newly provisioned agent; temporary-key custody is not proof of completed inherited-key adoption. The v1 native-signer configuration remains explicitly supported for already provisioned deployments, but SYN's preferred new operator profile is Python v2.

## Qualification and non-goals

- This increment does not yet implement user-visible 24-word BIP-39 recovery and independently verified restoration; the original 256-bit entropy stays in SYN custody.
- This increment **does not** implement encrypted SYN P2P discovery, operator-wide trust/attestation, confidential selected-key adoption, agent private-key handoff, HSM keys, hardware attestation or Windows atomic installation locks.
- The OS keyring is a software credentials store. Private key values are temporarily represented as Python bytes and strings during operations. Keyring-backed at-rest storage is not TPM/HSM non-exportability, guaranteed secure erasure or defense against compromise of the SYN process/user.
- SQLite Phase 41 and Phase 42 public ledgers remain unchanged, and no Potbot module imports SYN's private API.
- An independent cross-language fixed-entropy test vector for both addresses and Ed25519-BIP32 V2 must be established before **production** signing qualification. Phase 44's synthetic tests use a deliberately marked fake wallet, not a working cryptographic engine. The optional real PyCardano test runs only when the provider is actually installed at the pinned version.
- The lock and local socket model is currently Linux-specific; no Windows or macOS security qualification is implied by supported Python/keyring APIs.

## Test profiles

```bash
PYTHONPATH=syn-node/src python -m pytest -q syn-node/tests/test_hd_keys.py
# With pinned optional dependencies actually installed, run the real crypto gate:
python -m pip install -e './syn-node[hd]'
PYTHONPATH=syn-node/src python -m pytest -q syn-node/tests/test_hd_keys.py -k fixed_entropy_real_pycardano
```

A skipped real-provider test **must never** be recorded as passing production qualification. Separately qualify OS credential backend ownership, restart/recovery, actual HD vector compatibility, Python memory exposure, and each supported host platform.
