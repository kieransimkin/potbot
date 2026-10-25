"""Actual copied Kieran Simkin MOTD skeleton; no network, no sandbox claims."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.domain.errors import StateMismatchError
from potbot.tools.agent_homes import AgentHomeRegistry
from potbot.tools.agent_skeleton import (
    SKELETON_SOURCE,
    SKELETON_SOURCE_COMMIT,
    SKELETON_VERSION,
    skeleton_digest,
)
from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger

DIGEST = "e" * 64
QUALIFICATION = "a" * 64

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Linux/WSL agent home POSIX descriptors")


@pytest.fixture
def registry(tmp_path: Path):
    home = tmp_path / "workspace" / "home"
    home.mkdir(parents=True)
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    ledger = WorkspaceLeaseLedger(private / "controller.sqlite", workspace_home=home)
    workspace = uuid4()
    ledger.pin_image(workspace, digest=DIGEST,
                     image_ref="ghcr.io/test/rootfs@sha256:" + DIGEST,
                     build_commit="f" * 40, platform="linux/amd64",
                     rootfs_path=rootfs, qualification_sha256=QUALIFICATION)
    return AgentHomeRegistry(ledger, workspace_name="danceflow"), workspace, home


def _git_blob_sha(content: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()


def test_source_pinned_exact_upstream_motd_blobs(registry):
    owner, workspace, home = registry
    agent = owner.provision(workspace, uuid4())
    motd = home / agent.directory / ".local/share/potbot/motd.d"
    upstream = {
        "10-figbanner": "1c8ec80cb1f6e23f31545303296d86cdd47d1540",
        "20-fortune": "afd270e021bc3905a678c31ed220f3c371fa9766",
        "99-insult": "97f27ca76757794327e3b803a4a2108bfa506172",
    }
    for name, sha in upstream.items():
        assert _git_blob_sha((motd / name).read_bytes()) == sha
        assert (motd / name).stat().st_mode & 0o777 == 0o755
    assert SKELETON_SOURCE == "kieransimkin/update-motd.d-scripts"
    assert SKELETON_SOURCE_COMMIT == "2485c90143dae3dd336769e7ab68ebcca781fa8a"


def test_copy_once_preserves_customizations_and_rename(registry):
    owner, workspace, home = registry
    actor = uuid4()
    first = owner.provision(workspace, actor)
    path = home / first.directory
    bashrc = path / ".bashrc"
    assert path.stat().st_mode & 0o777 == 0o775
    assert bashrc.stat().st_mode & 0o777 == 0o644
    assert (path / ".local/bin/potbot-welcome").stat().st_mode & 0o777 == 0o755
    assert (path / ".config/potbot").stat().st_mode & 0o777 == 0o775
    metadata = json.loads((path / ".config/potbot/skeleton.json").read_text())
    assert metadata["schema"] == SKELETON_VERSION
    assert metadata["template_sha256"] == skeleton_digest()
    assert (path / "projects").is_dir()
    assert (path / ".tmp").is_dir()
    assert (path / "Tools").is_symlink()
    assert os.readlink(path / "Tools") == "/usr/local/tools"
    bashrc.write_text("# Owned by agent, do not overwrite\n", encoding="utf-8")
    assert owner.provision(workspace, actor) == first
    assert bashrc.read_text() == "# Owned by agent, do not overwrite\n"
    renamed = owner.rename(workspace, actor, "my-creative-name", expected_revision=1)
    assert renamed.guest_path == "/home/my-creative-name"
    assert (home / renamed.directory / ".bashrc").read_text() == "# Owned by agent, do not overwrite\n"
    assert not path.exists()


def test_second_agent_gets_private_copy_and_identity(registry):
    owner, workspace, home = registry
    a = owner.provision(workspace, uuid4())
    b = owner.provision(workspace, uuid4())
    assert (a.uid, b.uid) == (420, 421)
    assert (a.gid, b.gid) == (69, 69)
    left = home / a.directory / ".local/share/potbot/motd.d/99-insult"
    right = home / b.directory / ".local/share/potbot/motd.d/99-insult"
    left.write_text("agent customization\n")
    assert right.read_text() != "agent customization\n"


def test_tampered_template_denied_before_copy(registry, monkeypatch, tmp_path: Path):
    import potbot.tools.agent_skeleton as skeleton

    owner, workspace, home = registry
    new = tmp_path / "tampered"
    shutil.copytree(skeleton._TEMPLATE_ROOT, new)
    (new / ".bashrc").write_text("unsafe change\n")
    monkeypatch.setattr(skeleton, "_TEMPLATE_ROOT", new)
    with pytest.raises(StateMismatchError):
        owner.provision(workspace, uuid4())
    assert not list(home.rglob("skeleton.json"))
    assert [item["state"] for item in owner.ledger.active(workspace)] == ["indeterminate"]


def test_symlinked_template_denied(registry, monkeypatch, tmp_path: Path):
    import potbot.tools.agent_skeleton as skeleton

    owner, workspace, _ = registry
    new = tmp_path / "tampered"
    shutil.copytree(skeleton._TEMPLATE_ROOT, new)
    replacement = new / ".bashrc"
    replacement.unlink()
    replacement.symlink_to(".profile")
    monkeypatch.setattr(skeleton, "_TEMPLATE_ROOT", new)
    with pytest.raises(StateMismatchError):
        owner.provision(workspace, uuid4())


def test_bash_startup_is_silent_for_noninteractive_tools(registry):
    if not shutil.which("bash"):
        pytest.skip("Bash not installed on unit test host")
    owner, workspace, home = registry
    agent = owner.provision(workspace, uuid4())
    env = {"HOME": str(home / agent.directory), "PATH": "/usr/bin:/bin", "POTBOT_WELCOME": "1"}
    result = subprocess.run(["bash", "--noprofile", "-c", "source \"$HOME/.bashrc\"; printf 'clean\\n'"],
                            env=env, capture_output=True, text=True, check=True)
    assert result.stdout == "clean\n"
    for script in sorted((home / agent.directory / ".local/share/potbot/motd.d").iterdir()):
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True)
        assert result.returncode == 0


def test_template_modes_do_not_claim_kernel_dac_protection(registry):
    owner, workspace, home = registry
    agent = owner.provision(workspace, uuid4())
    path = home / agent.directory
    assert stat.S_ISDIR(path.stat().st_mode)
    assert stat.S_ISREG((path / ".bashrc").stat().st_mode)
    # Mode bits do not establish distinct OS UIDs inside this unqualified fixture.
    assert agent.gid == 69


def test_interactive_pty_sources_the_agent_bashrc(registry):
    if not shutil.which("bash"):
        pytest.skip("Bash not installed on unit test host")
    owner, workspace, home = registry
    agent = owner.provision(workspace, uuid4())
    path = home / agent.directory
    # Replace the *agent's copy* of the welcome executable, not the vendor source.
    wrapper = path / ".local/bin/potbot-welcome"
    wrapper.write_text("#!/bin/bash\nprintf 'POTBOT-INTERACTIVE-MOTD\\n'\n")
    master, slave = os.openpty()
    try:
        process = subprocess.Popen(
            ["bash", "--noprofile", "-i", "-c", "printf 'SHELL-RAN\\n'"],
            env={"HOME": str(path), "PATH": "/usr/bin:/bin", "POTBOT_WELCOME": "1",
                 "TERM": "xterm-256color"},
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        )
        os.close(slave)
        slave = -1
        process.wait(timeout=5)
        output = bytearray()
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
        assert process.returncode == 0
        assert b"POTBOT-INTERACTIVE-MOTD" in output
        assert b"SHELL-RAN" in output
    finally:
        os.close(master)
        if slave != -1:
            os.close(slave)


def test_build_includes_vendor_assets_and_required_tools():
    import tomllib

    repo = Path(__file__).resolve().parents[2]
    settings = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    packaged = settings["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert packaged["src/potbot/tools/agent_skeleton"] == "potbot/tools/agent_skeleton"
    recipe = (repo / "sandbox/image/Dockerfile").read_text(encoding="utf-8")
    for package in ("figlet", "fortune-mod", "fortunes-min", "lolcat"):
        assert package in recipe


def _interactive_banner(agent_home: Path) -> str:
    """Exercise a genuine interactive Bash PTY with an observable figlet fixture."""
    tool = agent_home / ".local/bin/figlet"
    tool.write_text('#!/bin/sh\nprintf "FIGLET:%s\\n" "$*"\n', encoding="utf-8")
    tool.chmod(0o755)
    master, slave = os.openpty()
    try:
        process = subprocess.Popen(
            ["bash", "--noprofile", "-i", "-c", ":"],
            env={"HOME": str(agent_home), "PATH": "/usr/bin:/bin",
                 "POTBOT_WELCOME": "1", "TERM": "xterm-256color"},
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        )
        os.close(slave)
        slave = -1
        process.wait(timeout=5)
        chunks = bytearray()
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            chunks.extend(chunk)
        assert process.returncode == 0
        return chunks.decode("utf-8", errors="replace")
    finally:
        os.close(master)
        if slave != -1:
            os.close(slave)


def test_workspace_banner_switches_to_named_agent_style(registry):
    if not shutil.which("bash"):
        pytest.skip("Bash not installed")
    owner, workspace, home = registry
    agent_id = uuid4()
    anonymous = owner.provision(workspace, agent_id)
    root = home / anonymous.directory
    assert (root / ".config/potbot/workspace-name").read_text() == "danceflow\n"
    meta = json.loads((root / ".config/potbot/skeleton.json").read_text())
    assert meta["workspace_display_name"] == "danceflow"
    assert "FIGLET:-c -f standard danceflow" in _interactive_banner(root)

    # The title override must be ignored until the agent is formally named.
    (root / ".config/potbot/banner.title").write_text("Velvet Volt\n", encoding="utf-8")
    assert "FIGLET:-c -f standard danceflow" in _interactive_banner(root)

    named = owner.rename(workspace, agent_id, "velvet-volt", expected_revision=1)
    named_root = home / named.directory
    assert "FIGLET:-c -f standard Velvet Volt" in _interactive_banner(named_root)
    # Removing an override exposes the default name chosen during rename.
    (named_root / ".config/potbot/banner.title").unlink()
    assert "FIGLET:-c -f standard velvet volt" in _interactive_banner(named_root)

    (named_root / ".config/potbot/banner.title").write_text("Electric Dreams\n")
    (named_root / ".config/potbot/banner.font").write_text("slant\n")
    (named_root / ".config/potbot/banner.color").write_text("plain\n")
    assert "FIGLET:-c -f slant Electric Dreams" in _interactive_banner(named_root)
    assert owner.provision(workspace, agent_id) == named
    assert (named_root / ".config/potbot/banner.title").read_text() == "Electric Dreams\n"


def test_invalid_banner_customization_is_not_executed(registry):
    if not shutil.which("bash"):
        pytest.skip("Bash not installed")
    owner, workspace, home = registry
    agent_id = uuid4()
    anon = owner.provision(workspace, agent_id)
    named = owner.rename(workspace, agent_id, "safe-agent", expected_revision=1)
    root = home / named.directory
    (root / ".config/potbot/banner.title").write_text("Hi$(touch /tmp/UNSAFE)\n")
    (root / ".config/potbot/banner.font").write_text("../../home/attacker\n")
    result = _interactive_banner(root)
    assert "FIGLET:-c -f standard Workspace" in result
    assert "UNSAFE" not in result


def test_workspace_banner_normalizes_unicode_and_bounds_length():
    from potbot.tools.agent_skeleton import workspace_banner_label

    assert workspace_banner_label("DanceFlow 🎸 Studio") == "DanceFlow Studio"
    assert workspace_banner_label("électric nights") == "electric nights"
    assert workspace_banner_label("🎉") == "Workspace"
    assert len(workspace_banner_label("A" * 180)) == 48
