import hashlib
import json

import pytest

from potbot.cli import main
from potbot.wsl_kernel import OPTIONS, PROFILE, SOURCE_REPOSITORY


@pytest.fixture
def signed_bundle(tmp_path):
    ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    signer = ed25519.Ed25519PrivateKey.generate()
    release = "6.18.54.1-test-wsl"
    image = bytearray(1024)
    image[0x1FE:0x200] = b"\x55\xaa"
    image[0x202:0x206] = b"HdrS"
    image[0x211] = image[0x236] = 1
    image[0x20E:0x210] = (0x80).to_bytes(2, "little")
    label = release.encode() + b"\0"
    image[0x280 : 0x280 + len(label)] = label
    payloads = {
        "bzImage": image,
        "modules.vhdx": b"vhdxfile-test",
        "kernel.config": "".join(f"{option}=y\n" for option in OPTIONS).encode(),
        "wsl-taskstats-pidns.patch": b"test patch",
    }

    def seal():
        manifest = {
            "profile": PROFILE,
            "architecture": "x86_64",
            "kernel_release": release,
            "source": {
                "repository": SOURCE_REPOSITORY,
                "commit": "a" * 40,
                "archive_sha256": "b" * 64,
            },
            "artifacts": {
                name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for name, data in payloads.items()
            },
        }
        for name, data in payloads.items():
            (tmp_path / name).write_bytes(data)
        raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        (tmp_path / "manifest.json").write_bytes(raw)
        signature = signer.sign(b"potbot.wsl-kernel-bundle/1\0" + raw)
        (tmp_path / "manifest.ed25519").write_bytes(signature.hex().encode())
        return [
            "wsl-kernel",
            "verify",
            "--bundle",
            str(tmp_path),
            "--manifest-sha256",
            hashlib.sha256(raw).hexdigest(),
            "--public-key",
            signer.public_key().public_bytes_raw().hex(),
        ]

    return tmp_path, payloads, seal


def test_wsl_kernel_cli_verifies_only_selected_bundle_bytes(signed_bundle, capsys):
    root, _, seal = signed_bundle
    args = seal()
    assert main(args) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["bundle_verified"] and receipt["activation"] == "not_attempted"
    assert receipt["manifest_sha256"] == args[-3]

    assert main([*args[:-1], "00" * 32]) == 2
    assert "signature verification failed" in capsys.readouterr().out
    assert main([*args[:-3], "00" * 32, *args[-2:]]) == 2
    assert "independent selection" in capsys.readouterr().out

    (root / "modules.vhdx").write_bytes(b"vhdxfile-tESt")
    assert main(args) == 2
    assert "digest differs for modules.vhdx" in capsys.readouterr().out


def test_signed_bundle_with_conflicting_taskstats_settings_is_refused(signed_bundle, capsys):
    _, payloads, seal = signed_bundle
    payloads["kernel.config"] += b"# CONFIG_TASKSTATS is not set\n"
    assert main(seal()) == 2
    assert "must enable CONFIG_TASKSTATS" in capsys.readouterr().out
