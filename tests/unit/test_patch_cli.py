from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from potbot.domain.errors import (
    RevisionConflictError,
    ScopeDeniedError,
    SourceUnavailableError,
)
from potbot.tools import patch_cli
from potbot.workspace_sandbox import WorkspaceSandbox


def prefix(root: Path) -> list[str]:
    return [
        "--dev-auth",
        "--workspace",
        str(uuid4()),
        "--principal",
        str(uuid4()),
        "--artifacts",
        str(root / "artifacts"),
        "--candidates",
        str(root / "candidates"),
        "--receipts",
        str(root / "receipts"),
    ]


@pytest.mark.parametrize("arguments", [["--help"], ["demo", "--help"], ["resume", "--help"]])
def test_help_needs_no_credentials_or_backends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    monkeypatch.delenv("POTBOT_DSN", raising=False)
    credential_lookup = Mock(side_effect=AssertionError("Help resolved a credential"))
    backend = Mock(side_effect=AssertionError("Help allocated storage"))
    monkeypatch.setattr("potbot.credentials.resolve_postgres_dsn", credential_lookup)
    monkeypatch.setattr("potbot.persistence.artifacts.LocalArtifactBackend", backend)
    with pytest.raises(SystemExit) as result:
        patch_cli.main(arguments)
    assert result.value.code == 0
    assert "usage:" in capsys.readouterr().out
    credential_lookup.assert_not_called()
    backend.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "arguments",
    [
        ["unknown"],
        ["resume", "not-a-uuid"],
        ["demo", "--key", "key"],
        ["demo", "--fixtures", "fixtures", "--key", " "],
        ["demo", "--fixtures", "fixtures", "--key", "x" * 129],
        ["demo", "--fixtures", "fixtures", "--key", "key", "--stop-after", "unknown"],
        ["--schema", "bad;schema", "inspect", str(uuid4())],
        ["export", str(uuid4())],
    ],
)
def test_malformed_arguments_do_not_connect_prompt_or_allocate_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
) -> None:
    credential_lookup = Mock(side_effect=AssertionError("Invalid input resolved credentials"))
    backend = Mock(side_effect=AssertionError("Invalid input allocated storage"))
    monkeypatch.setattr("potbot.credentials.resolve_postgres_dsn", credential_lookup)
    monkeypatch.setattr("potbot.persistence.artifacts.LocalArtifactBackend", backend)
    with pytest.raises(SystemExit) as result:
        patch_cli.main([*prefix(tmp_path), *arguments])
    assert result.value.code == 2
    credential_lookup.assert_not_called()
    backend.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    pipeline_id, export_id = uuid4(), uuid4()
    sandbox = WorkspaceSandbox.prepare(tmp_path / "workspace")
    monkeypatch.setattr(
        "potbot.workspace_sandbox.load_workspace_sandbox", Mock(return_value=sandbox)
    )
    pipeline = SimpleNamespace(
        resume=Mock(
            return_value={
                "pipeline_id": str(pipeline_id),
                "outcome": "verified",
                "export": {"id": str(export_id)},
            }
        ),
        inspect=Mock(return_value={"pipeline_id": str(pipeline_id), "outcome": "partial"}),
        exports=SimpleNamespace(
            write_bundle=Mock(return_value={"patch": "change.patch", "manifest": "manifest.json"})
        ),
    )
    credential_lookup = Mock(return_value="postgresql://fixture:private@localhost/potbot_test")
    monkeypatch.setattr("potbot.credentials.resolve_postgres_dsn", credential_lookup)
    for target, value in (
        ("potbot.persistence.database.Database", object()),
        ("potbot.persistence.artifacts.LocalArtifactBackend", object()),
        ("potbot.persistence.artifacts.ArtifactStore", object()),
        ("potbot.tools.repository.CandidateWorkspaceManager", object()),
        ("potbot.tools.receipts.RunReceiptStore", object()),
        ("potbot.tools.gateway.ToolGateway", object()),
        ("potbot.controller.patch_pipeline.PatchPipeline", pipeline),
        ("potbot.tools.patch_application.patch_runtime_roots", (tmp_path / "patch_worker.py",)),
    ):
        monkeypatch.setattr(target, Mock(return_value=value))
    demo = Mock(return_value={"id": str(pipeline_id)})
    monkeypatch.setattr(patch_cli, "_demo_pipeline", demo)
    return SimpleNamespace(
        prefix=prefix(sandbox.root),
        sandbox=sandbox,
        pipeline=pipeline,
        pipeline_id=pipeline_id,
        export_id=export_id,
        demo=demo,
        credentials=credential_lookup,
    )


def test_demo_returns_json_and_retains_optional_bundle_arguments(
    rig: SimpleNamespace,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = rig.sandbox.path("artifacts") / "review bundle"
    args = [
        *rig.prefix,
        "demo",
        "--fixtures",
        str(rig.sandbox.path("tests") / "fixtures"),
        "--key",
        "cli-demo",
        "--preexisting-failure",
        "--output",
        str(output),
    ]
    assert patch_cli.main(args) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["schema_version"] == 1
    assert result["data"]["outcome"] == "verified"
    assert result["data"]["bundle"]["patch"] == "change.patch"
    assert str(rig.pipeline_id) in captured.err
    assert rig.demo.call_args.args[-2:] == (True, "cli-demo")
    rig.pipeline.exports.write_bundle.assert_called_once()
    assert rig.pipeline.exports.write_bundle.call_args.args[-2:] == (rig.export_id, output)


@pytest.mark.parametrize("outcome", ["partial", "blocked"])
def test_incomplete_resume_returns_nonzero_status_and_structured_result(
    rig: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
) -> None:
    rig.pipeline.resume.return_value = {"outcome": outcome}
    assert (
        patch_cli.main(
            [*rig.prefix, "resume", str(rig.pipeline_id), "--stop-after", "verification"]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["data"]["outcome"] == outcome
    assert rig.pipeline.resume.call_args.kwargs["stop_after"] == "verification"
    rig.demo.assert_not_called()


def test_read_only_inspection_does_not_resume_and_succeeds_for_partial_pipeline(
    rig: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert patch_cli.main([*rig.prefix, "inspect", str(rig.pipeline_id)]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["outcome"] == "partial"
    rig.pipeline.resume.assert_not_called()
    rig.demo.assert_not_called()


@pytest.mark.parametrize(
    "failure,code",
    [
        (RevisionConflictError("credential=private"), "revision_conflict"),
        (SourceUnavailableError("credential=private"), "source_unavailable"),
        (RuntimeError("credential=private"), "internal_error"),
    ],
)
def test_runtime_failures_return_sanitized_json_without_exception_details(
    rig: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
    code: str,
) -> None:
    rig.pipeline.resume.side_effect = failure
    assert patch_cli.main([*rig.prefix, "resume", str(rig.pipeline_id)]) == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["error"]["code"] == code
    assert result["data"] is None
    assert "private" not in captured.out + captured.err


def test_missing_registered_sandbox_blocks_pipeline_dispatch(rig, monkeypatch, capsys):
    monkeypatch.setattr(
        "potbot.workspace_sandbox.load_workspace_sandbox",
        Mock(side_effect=ScopeDeniedError("No registered workspace sandbox")),
    )
    assert patch_cli.main([*rig.prefix, "inspect", str(rig.pipeline_id)]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "scope_denied"
    rig.pipeline.inspect.assert_not_called()


def test_export_destination_outside_sandbox_is_rejected(rig, tmp_path, capsys):
    assert (
        patch_cli.main(
            [*rig.prefix, "export", str(rig.export_id), "--output", str(tmp_path / "escape")]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "scope_denied"
    rig.pipeline.exports.write_bundle.assert_not_called()
