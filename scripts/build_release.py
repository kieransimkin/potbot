#!/usr/bin/env python3
"""Build and smoke-test exact-tag source-preview distributions, without registry writes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

from potbot.tools.temporary import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def run(*command: str, cwd: Path = ROOT) -> None:
    subprocess.run(command, cwd=cwd, check=True, timeout=240)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument(
        "--allow-working-tree", action="store_true", help="Local candidate smoke only"
    )
    args = parser.parse_args()
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    if args.tag != "v" + version or not re.fullmatch(r"v\d+\.\d+\.\d+(?:\.dev\d+)?", args.tag):
        raise SystemExit("Release tag must exactly match the owned source version")
    if f'__version__ = "{version}"' not in (ROOT / "src/potbot/__init__.py").read_text():
        raise SystemExit("Runtime and project versions disagree")
    dirty = subprocess.check_output(["git", "status", "--porcelain=v1"], cwd=ROOT, text=True)
    if dirty and not args.allow_working_tree:
        raise SystemExit("A release build requires a clean exact source revision")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise SystemExit("Release output must be fresh; never overwrite distributions")
    scratch = ROOT / ".potbot/release-tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=scratch) as temporary:
        probe = Path(temporary) / "probe"
        probe.write_bytes(b"release-probe")
        if probe.read_bytes() != b"release-probe":
            raise SystemExit("Release temporary-directory preflight failed")
        moved = probe.with_name("renamed")
        probe.replace(moved)
        moved.unlink()
    run("uv", "build", "--sdist", "--wheel", "--out-dir", str(output))
    wheels, sources = list(output.glob("*.whl")), list(output.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise SystemExit("Expected one wheel and one source distribution")
    with zipfile.ZipFile(wheels[0]) as wheel:
        metadata = next(name for name in wheel.namelist() if name.endswith(".dist-info/METADATA"))
        if f"\nVersion: {version}\n" not in wheel.read(metadata).decode("utf-8"):
            raise SystemExit("Wheel version differs from release source")
        for source in (
            "cogs/debugger.py",
            "debugging/cog_runtime.py",
            "controller/cog_work.py",
            "cogs/inventory.py",
            "controller/tool_inventory.py",
            "tools/inventory.py",
            "tools/semantic_contracts.py",
            "cogs/tool_contracts.py",
            "controller/tool_contracts.py",
            "cogs/execution.py",
            "controller/execution_cogs.py",
            "controller/execution_journal.py",
            "controller/execution_receipts.py",
            "tools/execution_contracts.py",
            "tools/temporary.py",
            "tools/patch_worker.py",
            "persistence/execution_provenance_schema.py",
        ):
            if wheel.read("potbot/" + source) != (ROOT / "src/potbot" / source).read_bytes():
                raise SystemExit("Wheel debugger source differs from build input")
        names = wheel.namelist()
    with tarfile.open(sources[0]) as source_archive:
        source_names = source_archive.getnames()
    for name in [*names, *source_names]:
        parts = Path(name).parts
        if any(p in {".codex", ".potbot", ".test-runs", ".venv", ".env"} for p in parts):
            raise SystemExit("Private/cache material found in distribution")
        if name.endswith((".safetensors", ".gguf", ".ckpt", ".pth", ".onnx")):
            raise SystemExit("Model weights found in distribution")
    with TemporaryDirectory(dir=scratch) as temporary:
        environment = Path(temporary) / "smoke"
        run("uv", "venv", "--python", sys.executable, str(environment))
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run("uv", "pip", "install", "--python", str(python), "--no-deps", str(wheels[0]))
        run(
            str(python),
            "-I",
            "-c",
            "import potbot; from potbot.cogs.registry import BoundCogRegistry; "
            f"assert potbot.__version__ == {version!r}; "
            "assert len(BoundCogRegistry.debugger().installed()) == 25; "
            "assert len(BoundCogRegistry.tool_inventory().installed()) == 5; "
            "assert len(BoundCogRegistry.tool_contracts().installed()) == 7; "
            "assert len(BoundCogRegistry.provenance().installed()) == 23; "
            "print('Clean wheel version and installed SYN Cog catalog verified')",
            cwd=Path(temporary),
        )
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    assets = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (*wheels, *sources)
    }
    manifest = output / "release-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": version,
                "tag": args.tag,
                "source_commit": commit,
                "assets": assets,
                "clean_wheel_smoke": "passed",
                "source_state": "working_tree_candidate" if dirty else "clean_commit",
                "registry_acceptance": "not_established_by_build",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    assets[manifest.name] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    (output / "SHA256SUMS.txt").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(assets.items())), encoding="ascii"
    )
    print(json.dumps({"version": version, "source_commit": commit, "assets": assets}, indent=2))


if __name__ == "__main__":
    main()
