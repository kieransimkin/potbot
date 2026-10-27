# Phase 46 — SYN standalone CI isolation and real-path qualification

**Target baseline:** Potbot `main` at `d33031d01c8eb8be60492a9bd4444d3e3bc3f60a`.
**Scope:** fix the failing, isolated Python SYN HD qualification workflow;
exercise all eight selected root-derived public paths with the real pinned
PyCardano library. This is **not** confidential inherited-key delivery or
hardware-backed identity adoption.

## Incident and correction

On GitHub Actions run `40557585986` the real HD smoke executed successfully,
including `pycardano==0.19.2`, `cbor2==5.8.0`, `cbor2pure==5.8.0` and the
operator base Ed25519-BIP32 signature verification. The subsequent isolated
pytest invocation failed *during conftest collection*, before any pytest case:

```text
ImportError while loading conftest '.../potbot/conftest.py'
ModuleNotFoundError: No module named 'potbot'
```

The standalone `syn-node[hd]` environment deliberately does not install
Potbot. It should not have to do so merely to test cryptography. The workflow
now uses pytest `--confcutdir=syn-node/tests` for **each** invocation, so the
SYN-only `syn-node/tests/conftest.py` is still loaded but Potbot's root
`conftest.py` is not. The workflow also supplies a short, disposable private
`/tmp` base to avoid the separate 107-byte Unix socket pathname limit.

The new `syn-node/tests/test_hd_live_qualification.py` exercises actual
PyCardano derivation for the exact operator root `m/6969'/420'`, base
`m/6969'/420'/0'/0/0`, and all eight inherited paths for three boundary
inheritance values (0, 42 and 16,777,215). Every child signs a test challenge
using the *extended* Ed25519-BIP32 private key; public Ed25519 verification
must pass. It also checks distinct full and three-byte public-key hashes per
set. The hardware-derived inheritance equation is unchanged:
`uint24_be(SHA256(hardware_id_bytes || operator_byte)[:3])`.

The standalone offline suite skips these particular tests when the optional
HD dependency is absent. The dedicated GitHub workflow instead **installs and
checks the exact real dependency versions**, runs a non-skippable Python signing
smoke before pytest, and explicitly invokes a real-provider leaf test.

## Verify

```bash
python -m pytest -q --confcutdir=syn-node/tests \
  syn-node/tests/test_hd_keys.py \
  syn-node/tests/test_operator_recovery.py \
  syn-node/tests/test_hd_live_qualification.py
```

Use the dedicated `.github/workflows/syn-python-hd.yml` for the mandatory
real-library acceptance gate. Do not mark it passing until GitHub Actions
actually runs successfully on the merged commit. No Potbot identity import or
private-key IPC is added.

## Remaining separate SYN installation work

SYN still needs independently qualified OS-keystore custody on each platform,
confidential P2P identity-key delegation, device attestation, exact selected
extended-private-key handoff, replay-fenced receipt issuance and agent proof of
exclusive custody. The currently available repository does not identify a
separate upstream Python SYN checkout, so this patch modifies the standalone
SYN service maintained at `potbot/syn-node/`. It does not modify SynFlint or
claim that an external SYN deployment was updated.
