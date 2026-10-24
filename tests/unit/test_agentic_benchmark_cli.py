"""Offline CLI, expense/admission and side-by-side output checks."""

from __future__ import annotations

import importlib.util
import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.evaluation.agentic_model_bench import Generation, ModelTarget, RoleCase, RoleSuite
from potbot.models.hosted_policy import HostedPolicy, RateRule
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts/benchmark_agentic_models.py"
_SPEC = importlib.util.spec_from_file_location("benchmark_agentic_models", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
bench = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = bench
_SPEC.loader.exec_module(bench)


def _args(**overrides):
    data = {
        "repeat": 1, "confirm_prompt_disclosure": True, "allow_paid": False,
        "max_paid_calls": 0, "max_cost_microunits": 0,
        "shuffle_seed": 42, "judge_derived": False, "max_judge_pairs": 4,
        "min_samples": 12, "store_text": False,
    }
    return Namespace(**{**data, **overrides})


def _suite():
    return RoleSuite("trial", (RoleCase("triage", "triage", "Categorise safe work",
                                         "json_subset", {"route": "local"}),))


def _local():
    return ModelTarget("local", "tiny", "loopback_openai", ("triage",),
                       "unknown", base_url="http://localhost:11434/v1")


def test_preflight_no_models_are_called(monkeypatch, capsys) -> None:
    suite = Path(__file__).resolve().parents[2] / "examples/agentic-benchmark/role-suite.json"
    models = suite.parent / "local-models.json"
    assert bench.main(["preflight", "--suite", str(suite), "--models", str(models)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["possible_hosted_calls"] == 0
    assert output["network_started"] is False


def test_no_local_output_disclosure_without_explicit_flag(monkeypatch) -> None:
    class FakeTransport:
        def __init__(self, **_kwargs):
            pass

        def request(self, _model, _prompt):
            return Generation('{"route":"local","secret":"sensitive fictional answer"}', 5)

    monkeypatch.setattr(bench, "BenchmarkTransport", FakeTransport)
    report = bench._execute(_args(), _suite(), [_local()], ["generic"], None)
    assert report["results"][0]["score"] == 1
    assert report["results"][0]["response"] is None
    assert "sensitive fictional answer" not in json.dumps(report)
    assert report["qualified"] is False
    assert report["summary"]["recommendations"]["triage"]["decision"] == "insufficient_evidence"
    disclosed = bench._execute(_args(store_text=True), _suite(), [_local()], ["generic"], None)
    assert "sensitive fictional answer" in disclosed["results"][0]["response"]
    assert "Side-by-side attempts" in bench._report_html(disclosed)


def test_paid_model_never_dispatched_without_explicit_budget(monkeypatch) -> None:
    class FakeTransport:
        def __init__(self, **_kwargs):
            pass

        def request(self, *_args):
            raise AssertionError("network must not start")

    monkeypatch.setattr(bench, "BenchmarkTransport", FakeTransport)
    target = ModelTarget("cloud", "test-model", "hosted", ("triage",),
                         "unknown", provider="openai")
    policy = HostedPolicy(allow_prompt_disclosure=True,
                          rules=(RateRule("openai", "test-model", 1000000, 2000000),))
    with pytest.raises(InvalidInputError):
        bench._execute(_args(), _suite(), [target], ["generic"], policy)
    with pytest.raises(InvalidInputError):
        bench._execute(_args(allow_paid=True, max_paid_calls=0,
                             max_cost_microunits=5000), _suite(), [target], ["generic"], policy)


def test_private_report_never_overwrites(tmp_path) -> None:
    path = tmp_path / "report.json"
    bench._private_write(path, "first")
    assert path.read_text() == "first"
    with pytest.raises(FileExistsError):
        bench._private_write(path, "second")
    assert path.read_text() == "first"


def test_derived_judge_calibrates_two_positions_without_self_judging(monkeypatch) -> None:
    models = [
        ModelTarget("candidate-a", "a", "loopback_openai", ("triage",),
                    "unknown", base_url="http://localhost:11434/v1"),
        ModelTarget("candidate-b", "b", "loopback_openai", ("triage",),
                    "unknown", base_url="http://localhost:11434/v1"),
        ModelTarget("independent-judge", "c", "loopback_openai", ("judge",),
                    "unknown", base_url="http://localhost:11434/v1"),
    ]

    class FakeTransport:
        def __init__(self, **_kwargs):
            pass

        def request(self, model, prompt):
            if model.id == "candidate-a":
                return Generation('{"route":"local"}', 2)
            if model.id == "candidate-b":
                return Generation('{"route":"hosted"}', 2)
            correct_first = 'Candidate A: {\\"route\\":\\"local\\"}' in prompt.user
            return Generation('{"choice":"A"}' if correct_first else '{"choice":"B"}', 3)

    monkeypatch.setattr(bench, "BenchmarkTransport", FakeTransport)
    report = bench._execute(
        _args(judge_derived=True), _suite(), models, ["generic"], None
    )
    judge_results = [r for r in report["results"] if r["role"] == "judge"]
    assert len(judge_results) == 2
    assert all(r["model_id"] == "independent-judge" for r in judge_results)
    assert report["judges"]["pairs"][0]["n_pairs"] == 1


def test_runner_writes_private_dispatch_and_terminal_receipts(
    monkeypatch, tmp_path, capsys
) -> None:
    class FakeTransport:
        def __init__(self, **_kwargs):
            pass

        def request(self, _model, _prompt):
            return Generation('{"route":"local"}', 4)

    monkeypatch.setattr(bench, "BenchmarkTransport", FakeTransport)
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({"schema_version": 1, "id": "test", "cases": [{
        "id": "case", "role": "triage", "task": "Return route local", "grader": "json_subset",
        "expected": {"route": "local"},
    }]}))
    models = tmp_path / "models.json"
    models.write_text(json.dumps({"schema_version": 1, "models": [{
        "id": "tiny", "model": "local-small", "backend": "loopback_openai",
        "roles": ["triage"], "revision": "unknown",
        "base_url": "http://127.0.0.1:11434/v1",
    }]}))
    result = bench.main(["run", "--suite", str(suite), "--models", str(models),
                         "--variants", "generic", "--output-dir", str(tmp_path / "runs"),
                         "--confirm-prompt-disclosure"])
    assert result == 0
    output = json.loads(capsys.readouterr().out)
    folder = Path(output["report_dir"])
    events = [json.loads(line) for line in (folder / "attempts.jsonl").read_text().splitlines()]
    assert [item["phase"] for item in events] == ["possibly_dispatched", "terminal"]
    assert events[1]["result"]["score"] == 1
    assert events[1]["result"]["response"] is None
    assert "route" not in (folder / "report.json").read_text().split('"results"')[0]
