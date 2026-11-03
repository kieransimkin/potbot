"""Run exact official packages with disposable native custody and PostgreSQL."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

OLDER_SCHEMA = "20261023_0014"
CURRENT_SCHEMA = "20261024_0015"
VERSIONS = ("0.2.1", "0.3.1", "0.4.0")
DB_URL = "postgresql+psycopg://syn_ci:syn_ci_disposable_only@127.0.0.1:5432/syn_ci"


def command(argv, *, timeout=60):
    result = subprocess.run(argv, check=False, capture_output=True, timeout=timeout)
    if result.returncode:
        raise AssertionError(f"Disposable qualification command failed: {result.returncode}")
    return json.loads(result.stdout)


def native(upstream, base, consumer, pythons):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from syn_client.operator_protocol import canonical

    fixture = runpy.run_path(str(upstream / "scripts/ci_operator_native.py"))["native_fixture"]
    root = base / "operator"
    state = root / "syn-state"
    policies = {version: base / (version + ".json") for version in VERSIONS}

    def args(version):
        return [
            "--store",
            str(base / "packages"),
            "--policy",
            str(policies[version]),
            "--operator-root",
            str(root),
        ]

    potbot = [consumer, "-I", "-B", "-m", "potbot", "syn-operator"]

    def operator(version, operation, *extra):
        return command([*potbot, operation, *args(version), *extra])

    config = ["--config", str(state / "operator.json")]

    def syn(version, operation):
        return command([pythons[version], "-I", "-B", "-m", "syn.operator", operation, *config])

    def run_and_attach(version):
        import signal
        import time

        child = subprocess.Popen(
            [*potbot, "serve", *args(version)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 55
            while child.poll() is None and time.monotonic() < deadline:
                lifecycle = root / "lifecycle.json"
                if lifecycle.exists():
                    receipt = json.loads(lifecycle.read_bytes())
                    if receipt["state"] == "ready":
                        proof = operator(version, "attach")["attestation"]["status"]
                        assert proof["pid"] == receipt["pid"]
                        assert proof["runtime"]["runtime_version"] == version
                        return proof
                time.sleep(0.2)
            raise AssertionError("Official operator did not reach authenticated readiness")
        finally:
            if child.poll() is None:
                child.send_signal(signal.SIGTERM)
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
                raise AssertionError("Disposable supervisor did not stop") from None
            assert child.returncode == 0
            assert json.loads((root / "lifecycle.json").read_bytes())["state"] == "stopped"

    def switch(old, new):
        return operator(old, "switch-release", "--target-policy", str(policies[new]))

    # This script runs under the candidate's official interpreter. A separate
    # installed baseline subprocess owns the actual reservation and replay check.
    reservation = """
import json, sys
from pathlib import Path
from syn.operator.enrollment import reserve
request = json.loads(Path(sys.argv[2]).read_bytes())
try:
    reserve(Path(sys.argv[1]), request["pulse"], request["pledge"], 42, approve=lambda _: True)
except Exception as error:
    if "already reserved" not in str(error):
        raise
    print(json.dumps({"reserved": False}))
else:
    print(json.dumps({"reserved": True}))
"""
    with fixture(base):
        first, middle, latest = VERSIONS
        initial = operator(
            first,
            "bootstrap",
            "--new-identity",
            "--keyring-service",
            "syn.r06." + secrets.token_hex(6),
            "--keyring-account",
            "operator",
            "--network-id",
            "r06-disposable",
        )
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes_raw()
        request = base / "reservation.json"
        device = secrets.token_hex(16)

        def pledge(version):
            pulse = syn(version, "enrollment-pulse")
            offer = {
                "schema_version": 2,
                "type_ref": "synny://type/syn-agent-pledge-offer-v2",
                "temporary_public_key_hash": hashlib.sha256(public).hexdigest(),
                "hardware_unique_id": device,
                "timestamp_unix_seconds": pulse["pulse"]["issued_at"],
                "target_master_public_key_hash": initial["base_public_key_hash"],
                "pulse_challenge": pulse["pulse"]["challenge"],
            }
            signature = key.sign(b"syn.agent.pledge.offer.v2\0" + canonical(offer)).hex()
            request.write_bytes(
                canonical(
                    {
                        "pulse": pulse,
                        "pledge": {
                            "offer": offer,
                            "temporary_public_key": public.hex(),
                            "signature": signature,
                        },
                    }
                )
            )
            request.chmod(0o600)

        pledge(first)
        reserve = [pythons[first], "-I", "-B", "-c", reservation, config[1], str(request)]
        assert command(reserve)["reserved"]
        original = run_and_attach(first)
        assert switch(first, middle)["restart_required"]
        # Native adoption is a separate anchored journal introduced in 0.3.x.
        syn(middle, "adoption-init")
        journal = (state / "adoptions.json").read_bytes()
        receiver = syn(middle, "agent-pulse")["body"]["encryption_public_key"]
        upgraded = run_and_attach(middle)
        authority = state / "authority.json"
        stale = authority.read_bytes()
        assert switch(middle, latest)["restart_required"]
        active = run_and_attach(latest)
        assert syn(latest, "agent-pulse")["body"]["encryption_public_key"] == receiver
        assert switch(latest, middle)["restart_required"]
        rolled_back = run_and_attach(middle)
        assert syn(middle, "agent-pulse")["body"]["encryption_public_key"] == receiver
        assert (state / "adoptions.json").read_bytes() == journal
        assert (
            hashlib.sha256((state / "operator.json").read_bytes()).hexdigest()
            == initial["configuration_sha256"]
        )
        # A separate upstream challenge at each start proves the same signing key.
        observations = [original, upgraded, active, rolled_back]
        assert len({item["base_public_key"] for item in observations}) == 1
        assert [item["boot"] for item in observations] == [1, 2, 3, 4]
        assert all(item["allocation_count"] == 1 for item in observations)
        pledge(middle)
        reserve[0] = pythons[middle]
        assert command(reserve)["reserved"] is False
        current = authority.read_bytes()
        try:
            authority.write_bytes(stale)
            refused = subprocess.run(
                [pythons[middle], "-I", "-B", "-m", "syn.operator", "preflight", *config],
                capture_output=True,
                timeout=30,
            )
            assert refused.returncode == 2
        finally:
            authority.write_bytes(current)
        syn(middle, "preflight")
        print(
            json.dumps(
                {
                    "versions": [item["runtime"]["runtime_version"] for item in observations],
                    "boot_counters": [item["boot"] for item in observations],
                    "same_operator_identity": True,
                    "enrollment_count": 1,
                    "duplicate_reservation_refused": True,
                    "adoption_journal_preserved": True,
                    "stale_authority_state_refused": True,
                }
            )
        )


def database(pythons):
    program = """
import json, sys
from sqlalchemy import create_engine, inspect, text
from syn.migrations import currentRevision, upgradeDatabase
engine = create_engine(sys.argv[1])
if sys.argv[2] == "seed":
    upgradeDatabase(engine, sys.argv[3])
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO syn_policy_workspaces(workspace_id) VALUES ('r06-retained')"))
else:
    upgradeDatabase(engine)
with engine.connect() as conn:
    witness = conn.scalar(text("SELECT workspace_id FROM syn_policy_workspaces"))
    tables = set(inspect(conn).get_table_names())
print(json.dumps({"revision": currentRevision(engine), "witness": witness,
                  "governance": "syn_governed_records" in tables}))
engine.dispose()
"""
    output = [
        command([pythons[version], "-I", "-B", "-c", program, DB_URL, action, OLDER_SCHEMA])
        for version, action in [("0.3.1", "seed"), ("0.4.0", "migrate"), ("0.3.1", "reopen")]
    ]
    assert [item["revision"] for item in output] == [OLDER_SCHEMA, CURRENT_SCHEMA, CURRENT_SCHEMA]
    assert [item["governance"] for item in output] == [False, True, True]
    assert all(item["witness"] == "r06-retained" for item in output)
    return output


def main(upstream):
    from potbot.syn_release.contracts import Policy, canonical
    from potbot.syn_release.source import GitHubSource
    from potbot.syn_release.store import SynReleaseStore

    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Requires the dedicated disposable Linux workflow")
    seed = json.loads((upstream / "release/install-policies/v0.2.1-operator.json").read_bytes())
    source = GitHubSource(os.environ.pop("POTBOT_SYN_GITHUB_TOKEN"))
    with tempfile.TemporaryDirectory(prefix="r06-", dir="/tmp") as temporary:
        base = Path(temporary)
        store = SynReleaseStore(base / "packages")
        uv = base / "uv"
        shutil.copy2(Path(shutil.which("uv") or "missing").resolve(strict=True), uv)
        uv.chmod(0o500)
        previous = None
        pythons = {}
        manifests = []
        for version in VERSIONS:
            print(f"Verifying and installing official Syn {version}", flush=True)
            pin = json.loads((upstream / f"release/pins/v{version}.json").read_bytes())
            floor = "0.2.1" if version == "0.2.1" else "0.3.1"
            value = {**seed, **pin, "minimum_version": floor}
            policy = Policy.parse(canonical(value))
            path = base / (version + ".json")
            path.write_bytes(canonical(value))
            path.chmod(0o600)
            installed = store.install(
                policy,
                python=Path("/usr/bin/python3.12"),
                uv=uv,
                upgrade_from=previous,
                source=source,
            )
            previous = policy
            manifests.append(installed["manifest_sha256"])
            pythons[version] = str(
                store.root / "installs" / installed["directory"] / "venv/bin/python"
            )
        completed = subprocess.run(
            [
                "/usr/bin/dbus-run-session",
                "--",
                pythons["0.4.0"],
                "-I",
                "-B",
                __file__,
                "--native",
                str(upstream),
                str(base),
                sys.executable,
                json.dumps(pythons),
            ],
            timeout=440,
            check=True,
            stdout=subprocess.PIPE,
        )
        result = json.loads(completed.stdout)
        result["migration"] = database(pythons)
        result["manifest_sha256"] = manifests
        evidence = Path("artifacts/syn-r06/official.json")
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_bytes(canonical(result) + b"\n")
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    if sys.argv[1] == "--native":
        native(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], json.loads(sys.argv[5]))
    else:
        main(Path(sys.argv[1]))
