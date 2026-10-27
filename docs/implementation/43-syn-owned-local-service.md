# Phase 43 — Independently installable SYN local service

**Status: working standalone Linux read-only daemon protocol and agent temporary-key bootstrap; NOT a qualified production identity issuer.** This patch builds on Phase 42's public SYN daemon client, which is **not yet on GitHub main** as of 26 October 2026. Apply Phase 42 first. Potbot remains a client of SYN, and no Potbot service gets private/extended keys.

## Delivered

A distinct `syn-node/` Python distribution (`syn-node-service`, `syn-node` CLI) can run without importing Potbot modules. It implements:

1. Exact framed canonical JSON on a private Unix socket, bounded at 64 KiB, with `SO_PEERCRED` same-UID checks, challenged Ed25519 local attestation, pinned base public-key hash and read-only `adoption_receipt.get`. This wire API is interoperable with the Phase 42 `SynLocalDaemonClient`. The server cannot derive keys or accept enrollments through its public socket.
2. A private, locked installation registry, compatible with `SynInstallationRegistry` and updated via atomic rename + fsync. A different operator cannot replace an existing operator entry. Absent, altered, inaccessible, locked or conflicting discoveries do not create new identities.
3. An immutable public-adoption-receipt journal that independently verifies **both** selected-agent and operator Ed25519 signatures, exact master/agent identity bindings, timestamps and digest commitments. A signed operator *claim* of custody is recorded; it is **not** independent proof that an agent owns an OS keystore key. Journals do not contain raw hardware identifiers, entropy, private keys or chain codes.
4. A SYN-only helper to create a temporary agent Ed25519 keypair from OS CSPRNG and place its private seed in a selected *native* platform credential backend, with readback verification and an operator-private file lock to prevent competing first-time creations. No temporary key is derived from MAC, machine serial, hardware hash, time or predictable entropy. The public hash verifies signature identity; the original signed hardware identifier plus the operator's one-byte choice remains the inheritance input.
5. An **external native SYN signer** integration point. The binary must be privately installed with exact SHA-256 pinning, return a pinned 32-byte public key and valid Ed25519 signatures, and avoid exporting any private material. This is a *proposed v1 protocol* for the future real SYN HD-key broker; test-only signers do not establish actual OS custody.
6. Explicit `preflight`, `serve`, `publish-receipt` and `new-agent-temporary-key` CLI operations. Only an independently authorized operator may run them. Invalid/mismatched signer, insecure paths or missing keyring cause refusal. A stopped or locked daemon leaves an existing pinned registration unresolved, never silently replaced.

## Derivation and adoption remain SYN-owned

| Key | Exact path / identity |
| --- | --- |
| Operator root address | `m/6969'/420'` |
| Operator base address | `m/6969'/420'/0'/0/0` |
| Installation identifier | `SHA256(raw 32-byte base public key)` |
| Agent inheritance | `uint24_be(SHA256(raw signed hardware unique ID || operator byte)[:3])` |
| Agent choices | `m/6969'/420'/inheritance'/{1..8}/0` |
| Independence | one selected choice index `1..8`; the offered 3-byte abbreviations are **not** authoritative identities |

An Ed25519-BIP32 **extended key** is not an ordinary Ed25519 seed. Consequently, the supplied Python OS-keyring helper must **never** be used as the implementation of operator root/base derivation or adopted descendant signing. Production operator mode requires a separately qualified native SYN CIP-3 Icarus/Ed25519-BIP32 V2 signer with true OS-keystore custody. `NativeOperatorSigner` enforces public identity and binary pinning, **not** this missing qualification. It does not provide a generalized signing RPC to Potbot.

Root key, original entropy, derivation chain codes, rejected candidate private keys and any selected agent private key must remain under SYN control. Before an adopted key is installed, implement authenticated encrypted key transfer or independently reviewed cryptographic delegation, ownership/custody receipts, proof of possession on the **independently running agent installation**, revocation and crash reconciliation. An operator can itself sign with a derived child, so a child public signature is not standalone evidence of exclusive agent control.

## Security and live qualification

- Public `adoption_receipt.get` requires a previously pinned SYN daemon, a local authenticated user and a challenge-specific reply. The API exposes no `sign`, `derive`, `unwrap`, `publish`, `pledge.accept` or credential-export method.
- `new-agent-temporary-key` requires an actual native OS credential store: Linux Secret Service, macOS Keychain or Windows Credential Manager, and a private singleton lock. The test mock is not a qualified keyring. Ordinary Python process memory is not automatically locked; the production native SYN signer must prove its memory and OS custody boundary. Windows local daemon and interprocess locking need a native transport/lock backend; Linux Unix sockets are not a Windows fallback.
- Manual `publish-receipt` verifies a signed public packet; it is not an agent activation grant. The journal intentionally persists the same original signed receipt and rejects divergent replacements or tampering.
- Qualified SYN P2P pulse broadcast, encrypted operator-directed hardware offer, real hardware attestation, genuine CIP-3 root/base derivation, OS-keystore key import, signed candidate generation, agent-selected-key delivery, rollback-resistant state and external adoption remain **outstanding**.
- Upgrade/migration from legacy Potbot identity vaults must never invent new identities, derive agent keys within Potbot, or import secrets via a model-visible channel.

## Commands and evidence

```bash
PYTHONPATH=syn-node/src:src python -m pytest -q syn-node/tests \
  tests/unit/test_syn_local_daemon.py tests/unit/test_syn_adoption.py
PYTHONPATH=syn-node/src python -m syn_node --help
```

`standalone new-agent-temporary-key` must run with the real OS `keyring` package and selected native backend. The tests use disposable Ed25519 signing keys, a deliberately synthetic executable signer with no OS custody, private local SQLite and a real Unix-socket service. Passing them proves **wire interoperability and replay-safe public record handling**, not P2P confidentiality, hardware trust or native signer qualification. The root/base operator identity feature must remain blocked until a real native SYN package can pass the separate key derivation and keystore host gates.
