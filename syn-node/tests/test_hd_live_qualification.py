"""Real optional PyCardano qualification of SYN's exact inherited HD paths.

These checks exercise a deterministic PUBLIC test entropy and real curve
signatures. They do not create an OS keystore, transfer keys or qualify an
operator host. The dedicated hd CI environment installs the pinned provider
and runs this test as a mandatory gate, never accepting a mock as proof.
"""

from __future__ import annotations

import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from syn_node.hd_keys import BASE_PATH, ROOT_PATH, _derive, _pycardano


@pytest.fixture(scope="module")
def real_hdwallet():
    pytest.importorskip("pycardano")
    HDWallet, ExtendedSigningKey = _pycardano()
    return HDWallet.from_entropy(bytes(range(32)).hex(), passphrase=""), ExtendedSigningKey


def test_operator_root_and_base_exact_distinct_paths(real_hdwallet):
    wallet, signing_cls = real_hdwallet
    values = _derive(bytes(range(32)))
    root = wallet.derive_from_path("m/6969'/420'")
    base = wallet.derive_from_path("m/6969'/420'/0'/0/0")
    assert ROOT_PATH == "m/6969'/420'"
    assert BASE_PATH == "m/6969'/420'/0'/0/0"
    assert bytes(root.public_key) == values["root_public"]
    assert bytes(base.public_key) == values["base_public"]
    assert root.public_key != base.public_key
    assert hashlib.sha256(bytes(base.public_key)).digest() != hashlib.sha256(bytes(root.public_key)).digest()
    for node, tag in ((root, b"root"), (base, b"base")):
        signer = signing_cls(bytes(node.xprivate_key) + bytes(node.public_key) + bytes(node.chain_code))
        message = b"SYN v2 derived-key path qualification/" + tag
        Ed25519PublicKey.from_public_bytes(bytes(node.public_key)).verify(
            signer.sign(message), message
        )


@pytest.mark.parametrize("inheritance", [0, 42, (1 << 24) - 1])
def test_eight_independent_hd_leaf_signatures(real_hdwallet, inheritance):
    wallet, signing_cls = real_hdwallet
    hashes: list[bytes] = []
    for independence in range(1, 9):
        path = f"m/6969'/420'/{inheritance}'/{independence}/0"
        node = wallet.derive_from_path(path)
        public = bytes(node.public_key)
        assert len(public) == 32
        signature = signing_cls(
            bytes(node.xprivate_key) + public + bytes(node.chain_code)
        ).sign(b"SYN inherited-key path/public-only qualification")
        Ed25519PublicKey.from_public_bytes(public).verify(
            signature, b"SYN inherited-key path/public-only qualification"
        )
        hashes.append(hashlib.sha256(public).digest())
    assert len(hashes) == len(set(hashes)) == 8
    assert len({digest[:3] for digest in hashes}) == 8
