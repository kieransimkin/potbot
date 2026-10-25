"""Portable, least-privilege persistent service management for the existing uv launchers.

The three start.* launchers install uv/Python and call ``potbot service install``.
Services run as the installing user so OS-vault credentials remain accessible.
Systemd user units, launchd user agents and Windows per-user scheduled tasks
are distinct managers; no SYSTEM/root task or shell-script service wrapper is used.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import secrets
import signal
import subprocess
import sys
import threading
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.api.observability import EventLogger
from potbot.credentials import CredentialStore, credential_profile, resolve_postgres_dsn
from potbot.dependencies import project_root
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import CapabilityUnavailableError, DomainError, InvalidInputError
from potbot.models.hosted_policy import HostedPolicy

UNIT_NAME = "potbot.service"
MAC_NAME = "uk.co.potbot.service"
WINDOWS_NAME = "Potbot-User-Service"


@dataclass(frozen=True)
class ServiceConfig:
    workspace_id: str
    principal_id: str
    provider_policy: str
    schema: str = "potbot"
    host: str = "127.0.0.1"
    port: int = 8765
    allowed_origins: tuple[str, ...] = ()
    log_content: bool = False
    credential_profile: str = "default"
    operator_manifest: str | None = None

    @classmethod
    def load(cls, path: Path) -> ServiceConfig:
        try:
            with path.open("rb") as reader:
                raw = reader.read(8193)
            if len(raw) > 8192:
                raise ValueError
            record = json.loads(raw)
            record["allowed_origins"] = tuple(record.get("allowed_origins", []))
            config = cls(**record)
            config.validate()
            return config
        except (OSError, ValueError, TypeError, KeyError):
            raise InvalidInputError("Service configuration is unavailable or invalid.") from None

    def validate(self) -> None:
        from potbot.api.server_cli import _origins
        from potbot.persistence.database import _identifier

        try:
            UUID(self.workspace_id)
            UUID(self.principal_id)
            _identifier(self.schema)
            if not 1 <= self.port <= 65535:
                raise ValueError
            args = argparse.Namespace(host=self.host, port=self.port,
                                      allowed_origin=list(self.allowed_origins))
            _origins(args)
            if credential_profile({"POTBOT_CREDENTIAL_PROFILE": self.credential_profile}) != self.credential_profile:
                raise ValueError
            if type(self.log_content) is not bool:
                raise ValueError
            if not Path(self.provider_policy).is_absolute():
                raise ValueError
            if self.operator_manifest is not None and (
                not isinstance(self.operator_manifest, str)
                or not Path(self.operator_manifest).is_absolute()
                or not sys.platform.startswith("linux")
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise InvalidInputError("Service configuration contains invalid scope or listener settings.") from None


def service_home() -> Path:
    return project_root() / ".potbot" / "service"


def config_path() -> Path:
    return service_home() / "config.json"


def _atomic_configuration(config: ServiceConfig) -> None:
    config.validate()
    directory = service_home()
    if directory.exists() and directory.is_symlink():
        raise InvalidInputError("Service directory cannot be a symbolic link.")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        directory.chmod(0o700)
    target = config_path()
    if target.is_symlink():
        raise InvalidInputError("Service configuration cannot be a symbolic link.")
    temporary = directory / (".config-" + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as writer:
            json.dump(asdict(config), writer, sort_keys=True)
            writer.write("\n")
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _manager() -> str:
    if sys.platform == "win32":
        return "windows_task"
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd_user"
    raise InvalidInputError("Persistent service installation is unsupported on this platform.")


def _service_command() -> list[str]:
    return [str(Path(sys.executable).resolve()), "-m", "potbot.startup", "service", "run"]


def _systemd_unit() -> str:
    def quote(arg: str) -> str:
        return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'

    command = " ".join(quote(part) for part in _service_command())
    directory = quote(str(project_root()))
    return ("[Unit]\nDescription=Potbot API and hosted inference worker\n"
            "After=network-online.target\nWants=network-online.target\n\n"
            "[Service]\nType=simple\nWorkingDirectory=" + directory + "\nExecStart=" + command +
            "\nRestart=on-failure\nRestartSec=5\nTimeoutStopSec=30\n"
            "NoNewPrivileges=true\n\n[Install]\nWantedBy=default.target\n")


def _run_manager(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=40, check=False)


def _managed(command: list[str]) -> None:
    try:
        result = _run_manager(command)
        if result.returncode:
            # Manager errors can echo arbitrary environment/user paths. Never print
            # stderr from systemctl, launchctl or schtasks without redaction.
            raise InvalidInputError("Service manager rejected the operation; inspect its local diagnostics.")
    except (OSError, subprocess.TimeoutExpired):
        raise InvalidInputError("The native service manager is unavailable.") from None


def install_manager() -> None:
    manager = _manager()
    if manager == "systemd_user":
        unit = Path.home() / ".config" / "systemd" / "user" / UNIT_NAME
        unit.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        unit.write_text(_systemd_unit(), encoding="utf-8")
        _managed(["systemctl", "--user", "daemon-reload"])
        _managed(["systemctl", "--user", "enable", UNIT_NAME])
    elif manager == "launchd":
        plist = Path.home() / "Library" / "LaunchAgents" / (MAC_NAME + ".plist")
        plist.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        content = {
            "Label": MAC_NAME,
            "ProgramArguments": _service_command(),
            "WorkingDirectory": str(project_root()),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "StandardOutPath": str(service_home() / "launchd.log"),
            "StandardErrorPath": str(service_home() / "launchd.log"),
        }
        plist.write_bytes(plistlib.dumps(content))
    else:
        # A per-user ONLOGON task has vault access; SYSTEM would not. The Python
        # supervisor restarts both children when they exit unexpectedly.
        argument = subprocess.list2cmdline(_service_command())
        _managed(["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", WINDOWS_NAME,
                  "/TR", argument, "/RL", "LIMITED"])


def manager_action(action: str) -> None:
    manager = _manager()
    if manager == "systemd_user":
        _managed(["systemctl", "--user", action, UNIT_NAME])
    elif manager == "launchd":
        domain = f"gui/{os.getuid()}"
        label = f"{domain}/{MAC_NAME}"
        plist = str(Path.home() / "Library" / "LaunchAgents" / (MAC_NAME + ".plist"))
        if action == "start":
            existing = _run_manager(["launchctl", "print", label])
            if existing.returncode == 0:
                _managed(["launchctl", "kickstart", "-k", label])
            else:
                _managed(["launchctl", "bootstrap", domain, plist])
        elif action == "stop":
            _managed(["launchctl", "bootout", label])
        else:
            raise InvalidInputError("Unknown launchd operation.")
    elif action in {"start", "stop"}:
        _managed(["schtasks", "/Run" if action == "start" else "/End", "/TN", WINDOWS_NAME])
    else:
        raise InvalidInputError("Unknown service manager operation.")


def remove_manager() -> None:
    manager = _manager()
    if manager == "systemd_user":
        _managed(["systemctl", "--user", "disable", "--now", UNIT_NAME])
        (Path.home() / ".config" / "systemd" / "user" / UNIT_NAME).unlink(missing_ok=True)
        _managed(["systemctl", "--user", "daemon-reload"])
    elif manager == "launchd":
        try:
            manager_action("stop")
        except InvalidInputError:
            pass
        (Path.home() / "Library" / "LaunchAgents" / (MAC_NAME + ".plist")).unlink(missing_ok=True)
    else:
        _managed(["schtasks", "/Delete", "/F", "/TN", WINDOWS_NAME])


def _token_store() -> tuple[bool, str | None]:
    # Refuse plaintext fallback on a headless machine with no native vault.
    store = CredentialStore()
    existing = store.get("api.access_token")
    if existing:
        return False, None
    generated = secrets.token_urlsafe(48)
    store.set("api.access_token", generated)
    return True, generated


def install(args: argparse.Namespace) -> dict[str, Any]:
    if config_path().exists() and not args.replace:
        raise InvalidInputError("Service already configured; use --replace to change settings.")
    policy = HostedPolicy.read(args.provider_policy.resolve())
    if args.download_embeddings:
        from potbot.api.observability import TransferProgress
        from potbot.dependencies import ConsoleProgress, ensure_standard_model, project_root

        logger = EventLogger(log_path=service_home() / "downloads.jsonl")
        console, telemetry = ConsoleProgress(), TransferProgress(logger)

        def progress(event: Any) -> None:
            console(event)
            telemetry(event)

        logger.emit("model.install.start", model="Qwen/Qwen3-Embedding-0.6B")
        ensure_standard_model(project_root(), progress=progress)
        logger.emit("model.install.completed", model="Qwen/Qwen3-Embedding-0.6B", verified=True)
    if args.migrate:
        from potbot.persistence.database import migrate
        migrate(resolve_postgres_dsn("admin"), args.schema)
    from potbot.persistence.database import Database
    context = WorkspaceContext(args.workspace_id, args.principal_id)
    with Database(resolve_postgres_dsn(), args.schema).transaction(context):
        pass
    config = ServiceConfig(
        str(args.workspace_id), str(args.principal_id), str(args.provider_policy.resolve()),
        schema=args.schema, host=args.host, port=args.port,
        allowed_origins=tuple(args.allowed_origin), log_content=args.log_content,
        credential_profile=credential_profile(),
        operator_manifest=str(args.operator_manifest.resolve(strict=True))
        if args.operator_manifest is not None else None,
    )
    config.validate()
    if config.operator_manifest is not None:
        from potbot.tools.operator_cli import parse_manifest
        selected = parse_manifest(Path(config.operator_manifest))
        if str(selected.workspace_id) != config.workspace_id:
            raise InvalidInputError("Operator manifest belongs to a different workspace")
    # Server-side disclosure policy always governs external provider use.
    if not policy.rules or not policy.allow_prompt_disclosure:
        print("[Potbot setup] No hosted models enabled by provider policy; "
              "the API will list potbot but inference will remain unavailable.", file=sys.stderr)
    created, key = _token_store()
    _atomic_configuration(config)
    install_manager()
    if not args.no_start:
        manager_action("start")
    # Only display the new token once, to the installing user's terminal, never
    # when stdout is redirected to a log/CI pipeline.
    if created and key and sys.stdout.isatty():
        print("Potbot API key (shown once; store safely): " + key, file=sys.stdout)
    return {
        "installed": True, "manager": _manager(), "started": not args.no_start,
        "api_base_url": f"http://{config.host}:{config.port}/v1",
        "dashboard": f"http://{config.host}:{config.port}",
        "api_key": "stored_in_native_vault" if created else "existing_native_vault_key",
        "api_key_displayed": bool(created and sys.stdout.isatty()),
        "credential_retrieval": "start.* credentials api-show (interactive terminal only)",
    }


def _supervise(config: ServiceConfig) -> int:
    """Keep API and worker alive under one native user-managed service process."""
    if config.credential_profile != credential_profile():
        raise InvalidInputError("Service credential profile does not match the running user.")
    home = service_home()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if home.is_symlink():
        raise InvalidInputError("Operator service home must not be a symbolic link")
    if os.name == "posix":
        home.chmod(0o700)
    events = EventLogger(log_path=home / "supervisor.jsonl")
    policy = config.provider_policy
    common = ["--workspace-id", config.workspace_id, "--principal-id", config.principal_id,
              "--schema", config.schema]
    commands = {
        "api": _service_command()[:2] + ["potbot.startup", "serve", *common, "--provider-policy", policy,
              "--host", config.host, "--port", str(config.port),
              "--log-file", str(home / "api.jsonl"),
              *(["--log-content"] if config.log_content else []),
              *(part for origin in config.allowed_origins for part in ("--allowed-origin", origin))],
        "worker": _service_command()[:2] + ["potbot.startup", "jobs", "worker", *common,
              "--provider-policy", policy],
    }
    stopped = threading.Event()

    def halt(_sig: int, _frame: Any) -> None:
        stopped.set()

    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), halt)

    children: dict[str, subprocess.Popen[str]] = {}
    env = dict(os.environ)
    env["POTBOT_CREDENTIAL_PROFILE"] = config.credential_profile
    env["POTBOT_PROJECT_ROOT"] = str(project_root())
    env["POTBOT_LOG_FILE"] = str(home / "worker.jsonl")
    env["POTBOT_LOG_CONTENT"] = "1" if config.log_content else "0"
    operator_attachment = None
    if config.operator_manifest is not None:
        from potbot.tools.operator_cli import parse_manifest
        from potbot.tools.operator_attachment_s13 import (
            OperatorAttachmentClient, mount_fingerprint,
        )
        paths = parse_manifest(Path(config.operator_manifest))
        if str(paths.workspace_id) != config.workspace_id:
            raise InvalidInputError("Operator manifest workspace differs from service scope")
        socket_path = home / "operator.sock"
        for mount in (paths.rootfs, paths.home, paths.model_staging,
                      paths.shared_models, paths.shared_tools,
                      paths.ai_config_dir, paths.ai_history_dir):
            physical = mount.resolve(strict=True)
            if socket_path == physical or socket_path.is_relative_to(physical):
                raise InvalidInputError("Managed service socket overlaps a guest mount")
        operator_attachment = OperatorAttachmentClient(
            socket_path=socket_path, workspace_id=paths.workspace_id,
            image_digest=paths.image_digest,
            qualification_sha256=paths.qualification_sha256,
            mounts_sha256=mount_fingerprint(paths.ai_config_dir, paths.ai_history_dir),
        )
        env.update({
            "POTBOT_OPERATOR_REQUIRED": "1",
            "POTBOT_OPERATOR_SOCKET": str(socket_path),
            "POTBOT_OPERATOR_WORKSPACE_ID": str(paths.workspace_id),
            "POTBOT_OPERATOR_IMAGE_DIGEST": paths.image_digest,
            "POTBOT_OPERATOR_QUALIFICATION_SHA256": paths.qualification_sha256,
            "POTBOT_OPERATOR_MOUNTS_SHA256": operator_attachment.mounts_sha256,
        })
        commands = {"operator": _service_command()[:2] + [
            "potbot.startup", "operator", "run", "--manifest",
            config.operator_manifest,
            "--socket-path", str(socket_path),
        ], **commands}
    threads: list[threading.Thread] = []

    def watch(name: str, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            events.emit("service.console", source=name, line=line.rstrip("\n")[:8192])

    try:
        while not stopped.is_set():
            for name, command in commands.items():
                previous = children.get(name)
                if name == "operator" and previous is not None:
                    # Never silently adopt a replacement operator epoch.
                    if previous.poll() is not None or (
                        operator_attachment is not None and not operator_attachment.healthy()
                    ):
                        raise CapabilityUnavailableError("Managed operator disappeared")
                    continue
                if name != "operator" and operator_attachment is not None:
                    operator_attachment.require_healthy()
                if previous is not None and previous.poll() is None:
                    continue
                if previous is not None:
                    events.emit("service.restart", component=name, return_code=previous.returncode)
                process = subprocess.Popen(
                    command, cwd=project_root(), env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", bufsize=1,
                )
                children[name] = process
                thread = threading.Thread(target=watch, args=(name, process), daemon=True)
                thread.start()
                threads.append(thread)
                events.emit("service.started", component=name, pid=process.pid)
                if name == "operator":
                    assert operator_attachment is not None
                    # Admit no API/worker before the real operator attests.
                    for _ in range(80):
                        if process.poll() is not None or stopped.is_set():
                            break
                        if operator_attachment.healthy():
                            break
                        stopped.wait(0.1)
                    operator_attachment.require_healthy()
            if operator_attachment is not None:
                operator_attachment.require_healthy()
            stopped.wait(0.5 if operator_attachment is not None else 5)
    finally:
        # Stop API and worker before asking the history owner to exit.
        for name, process in children.items():
            if name != "operator" and process.poll() is None:
                process.terminate()
        for name, process in children.items():
            if name == "operator":
                continue
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        operator_process = children.get("operator")
        if operator_process is not None and operator_process.poll() is None:
            operator_process.terminate()
            try:
                operator_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # Indeterminate workspace leases block clean operator shutdown;
                # do not forcibly reap the owner and destroy recovery evidence.
                events.emit("service.operator_stop_blocked", recovery_required=True)
        events.emit("service.stopped")
        events.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="potbot service", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    install_parser = commands.add_parser("install", help="provision the user service and start it")
    install_parser.add_argument("--workspace-id", required=True, type=UUID)
    install_parser.add_argument("--principal-id", required=True, type=UUID)
    install_parser.add_argument("--provider-policy", required=True, type=Path)
    install_parser.add_argument("--operator-manifest", type=Path, default=None,
                                help="opt into Linux/WSL2 managed operator startup")
    install_parser.add_argument("--schema", default="potbot")
    install_parser.add_argument("--host", default="127.0.0.1")
    install_parser.add_argument("--port", type=int, default=8765)
    install_parser.add_argument("--allowed-origin", action="append", default=[])
    install_parser.add_argument("--migrate", action="store_true", help="run migrations using stored admin DSN")
    install_parser.add_argument("--no-start", action="store_true")
    install_parser.add_argument("--download-embeddings", action="store_true",
                                help="download and verify the pinned local Qwen embedding model")
    install_parser.add_argument("--replace", action="store_true")
    install_parser.add_argument("--log-content", action="store_true",
                                help="opt in to sensitive prompt and response logging")
    for command in ("start", "stop", "status", "uninstall", "run", "logs"):
        commands.add_parser(command)
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            result = install(args)
        elif args.command == "run":
            return _supervise(ServiceConfig.load(config_path()))
        elif args.command == "uninstall":
            remove_manager()
            result = {"installed": False, "configuration_retained": True,
                      "credentials_retained": True}
        elif args.command in {"start", "stop"}:
            ServiceConfig.load(config_path())
            manager_action(args.command)
            result = {"action": args.command, "manager": _manager()}
        elif args.command == "logs":
            directory = service_home()
            for name in ("supervisor", "api", "worker"):
                path = directory / (name + ".jsonl")
                if path.is_file():
                    with path.open(encoding="utf-8") as reader:
                        from collections import deque
                        for line in deque(reader, maxlen=20):
                            print(f"[{name}] {line}", end="")
            return 0
        else:
            configured = config_path().exists()
            result = {"configured": configured, "manager": _manager()}
            if configured:
                config = ServiceConfig.load(config_path())
                result["api_base_url"] = f"http://{config.host}:{config.port}/v1"
                try:
                    from urllib.request import urlopen
                    with urlopen(f"http://{config.host}:{config.port}/health", timeout=2) as response:
                        result["running"] = response.status == 200
                except (OSError, ValueError):
                    result["running"] = False
        print(json.dumps({"schema_version": 1, "data": result}, sort_keys=True))
        return 0
    except (DomainError, OSError, ValueError) as error:
        code = error.code.value if isinstance(error, DomainError) else "service_unavailable"
        # Never echo native manager stderr, connection secrets, policy data or exceptions.
        print(json.dumps({"error": {"code": code,
                                    "message": "The service operation failed; check configuration and native manager permissions."}}),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
