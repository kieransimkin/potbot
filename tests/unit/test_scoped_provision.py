import subprocess
import sys

import pytest

from potbot.domain.errors import CapabilityUnavailableError
from potbot.tools import scoped_provision as provision


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux systemd provisioning")
def test_failed_start_rolls_back_only_new_launcher_files(tmp_path, monkeypatch):
    units = tmp_path / "units"
    units.mkdir()
    env = tmp_path / "configuration" / "monitor.env"
    env.parent.mkdir()
    audit = tmp_path / "state" / "audit"
    monkeypatch.setattr(provision, "UNIT_DIR", units)
    monkeypatch.setattr(provision, "ENV_FILE", env)
    monkeypatch.setattr(provision, "AUDIT", audit)
    monkeypatch.setattr(provision, "DEFAULT_SOCKET", tmp_path / "launcher.sock")
    monkeypatch.setattr(provision, "_protected", lambda path: None)

    def inspect(path, content):
        return path.exists() and path.read_text() == content

    def audit_directory(uid, gid):
        audit.mkdir()
        return True

    operations = []

    def systemctl(*args, check=True):
        operations.append(args)
        if args[0] == "start":
            assert (units / provision.SOCKET).is_file()
            raise CapabilityUnavailableError("cannot start")
        return subprocess.CompletedProcess(
            args,
            1 if args[0] in ("is-active", "is-enabled") else 0,
            "",
        )

    monkeypatch.setattr(provision, "_inspect", inspect)
    monkeypatch.setattr(provision, "_audit_directory", audit_directory)
    monkeypatch.setattr(provision, "_manager", systemctl)
    env.write_text("retained")
    files = {units / provision.SOCKET: "generated", env: "retained"}

    with pytest.raises(CapabilityUnavailableError, match="cannot start"):
        provision._apply(files, 1000, 1000)

    assert env.read_text() == "retained"
    assert not (units / provision.SOCKET).exists()
    assert not audit.parent.exists()
    assert operations[-3:] == [
        ("stop", provision.SOCKET, provision.KEEPER),
        ("disable", provision.SOCKET),
        ("daemon-reload",),
    ]
