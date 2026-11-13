"""Qualify the real Python profiler inside the existing Linux process boundary."""

import hashlib
import sys
from pathlib import Path
from typing import Any

import pytest

from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.tools.python_profile import compare_python_profile
from potbot.tools.supervisor import ProcessSupervisor

pytestmark = pytest.mark.sandbox


def test_python_profile_finds_hotspot_and_measures_paired_overhead(tmp_path: Path) -> None:
    secret = "fixture-value-that-must-not-enter-the-profile"
    canary = tmp_path / "outside-capture"
    canary.write_text(secret, encoding="utf-8")
    source = tmp_path / "workload.py"
    source.write_text(
        f"""from pathlib import Path
import socket

def hotspot():
    total = 0
    for value in range(250_000):
        total += (value * value) % 101
    return total

def workload():
    assert not Path({str(canary)!r}).exists()
    try:
        socket.socket()
    except PermissionError:
        pass
    else:
        raise AssertionError("socket unexpectedly available")
    print({secret!r})
    return hotspot()

RESULT = {{"total": workload()}}
""",
        encoding="utf-8",
    )
    interpreter = Path(sys.executable).resolve()
    with interpreter.open("rb") as executable:
        digest = hashlib.file_digest(executable, "sha256").hexdigest()
    scope: dict[str, Any] = {
        "workspace": tmp_path,
        "source": source,
        "interpreter": interpreter,
        "interpreter_sha256": digest,
        "repetitions": 3,
    }
    with pytest.raises(ScopeDeniedError):
        compare_python_profile(ProcessSupervisor(), source_sha256="0" * 64, **scope)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    result = compare_python_profile(ProcessSupervisor(), source_sha256=source_hash, **scope)
    assert result["source_sha256"] == source_hash
    assert result["warmup_pairs"] == 1
    assert result["repetitions"] == 3
    assert all(not row["functions"] for row in result["samples"]["baseline"])
    for row in result["samples"]["profile"]:
        top = row["functions"][0]
        assert (top["function"], top["line"], top["calls"]) == ("hotspot", 4, 1)
        assert top["cumulative_ns"] >= top["self_ns"] > 0
        assert row["result_sha256"] == result["result_sha256"]
        assert len(row["isolation_identity"]) == 64
    for metric in ("cpu_ns", "wall_ns"):
        measured = result["median_ns"]
        assert (
            result["overhead_ns"][metric]
            == measured["profile"][metric] - measured["baseline"][metric]
        )
    assert secret not in str(result)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["outside-capture", "workload.py"]

    source.write_text("raise SystemExit(7)\n", encoding="utf-8")
    with pytest.raises(StateMismatchError, match="nonzero_exit"):
        compare_python_profile(
            ProcessSupervisor(),
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            **scope,
        )
    assert sorted(path.name for path in tmp_path.iterdir()) == ["outside-capture", "workload.py"]
