import hashlib
import json
import tarfile
import zipfile

import pytest

from potbot.cli import main
from potbot.wsl_kernel import OPTIONS, PROFILE, SOURCE_REPOSITORY
from scripts.prepare_wsl_kernel_release import main as package_kernel


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

    return tmp_path, payloads, seal, signer


def test_wsl_kernel_cli_verifies_only_selected_bundle_bytes(signed_bundle, capsys):
    root, _, seal, _ = signed_bundle
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
    _, payloads, seal, _ = signed_bundle
    payloads["kernel.config"] += b"# CONFIG_TASKSTATS is not set\n"
    assert main(seal()) == 2
    assert "must enable CONFIG_TASKSTATS" in capsys.readouterr().out


def test_kernel_packager_preserves_signed_bytes_and_exports_reproducible_assets(
    signed_bundle, capsys
):
    root, _, seal, signer = signed_bundle
    verify = seal()
    source_commit = "a" * 40
    source = root / "microsoft.tar.gz"
    with tarfile.open(source, "w:gz") as archive:
        directory = tarfile.TarInfo(f"WSL2-Linux-Kernel-{source_commit}")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    prepared = root / "prepared"
    prepare = [
        "prepare",
        "--components",
        str(root),
        "--source-archive",
        str(source),
        "--source-commit",
        source_commit,
        "--source-sha256",
        source_sha,
        "--kernel-release",
        "6.18.54.1-test-wsl",
        "--output",
        str(prepared),
    ]
    assert package_kernel(prepare) == 0
    unsigned = json.loads(capsys.readouterr().out)
    raw = (prepared / "bundle" / "manifest.json").read_bytes()
    message = (prepared / "manifest.to-sign").read_bytes()
    assert message == b"potbot.wsl-kernel-bundle/1\0" + raw
    assert unsigned["manifest_sha256"] == hashlib.sha256(raw).hexdigest()
    review = [
        "review",
        "--prepared",
        str(prepared),
        "--manifest-sha256",
        unsigned["manifest_sha256"],
    ]
    assert package_kernel(review) == 0
    preflight = json.loads(capsys.readouterr().out)
    assert preflight["state"] == "ready_for_external_signing"
    assert preflight["signing_input_sha256"] == hashlib.sha256(message).hexdigest()
    assert preflight["signing_input_size"] == len(message)
    assert package_kernel([*review[:-1], "0" * 64]) == 2
    assert "independent selection" in capsys.readouterr().out
    (prepared / "manifest.to-sign").write_bytes(b"x" + message[1:])
    assert package_kernel(review) == 2
    assert "Signing input differs" in capsys.readouterr().out
    (prepared / "manifest.to-sign").write_bytes(message)

    signature = root / "external.ed25519"
    signature.write_bytes(signer.sign(message).hex().encode())
    finalize = [
        "finalize",
        "--prepared",
        str(prepared),
        "--signature",
        str(signature),
        "--manifest-sha256",
        unsigned["manifest_sha256"],
        "--public-key",
        verify[-1],
        "--output",
    ]
    outputs = [root / "release-one", root / "release-two"]
    first, second = outputs
    assert package_kernel([*finalize, str(first)]) == 0
    capsys.readouterr()
    signature.write_bytes(signer.sign(message))
    assert package_kernel([*finalize, str(second), "--signature-format", "raw"]) == 0
    capsys.readouterr()
    signature.write_bytes(signer.sign(message).hex().encode())
    assets = json.loads((first / "release.json").read_text())["assets"]
    for name, record in assets.items():
        payload = (first / name).read_bytes()
        assert payload == (second / name).read_bytes()
        assert record == {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    (exported_source,) = first.glob("*.source.tar.gz")
    assert exported_source.read_bytes() == source.read_bytes()
    unpacked = root / "unpacked"
    unpacked.mkdir()
    (zip_path,) = first.glob("*.zip")
    with zipfile.ZipFile(zip_path) as archive:
        for name in archive.namelist():
            (unpacked / name).write_bytes(archive.read(name))
    assert (
        main(
            [
                "wsl-kernel",
                "verify",
                "--bundle",
                str(unpacked),
                "--manifest-sha256",
                unsigned["manifest_sha256"],
                "--public-key",
                verify[-1],
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert package_kernel([*finalize, str(first)]) == 2
    assert (first / "release.json").is_file()
    capsys.readouterr()
    (prepared / "source.tar.gz").write_bytes(b"unrelated archive")
    failed_output = root / "refused"
    assert package_kernel([*finalize, str(failed_output)]) == 2
    assert "Source archive differs" in capsys.readouterr().out
    assert not failed_output.exists()
