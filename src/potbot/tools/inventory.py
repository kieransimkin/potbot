"""Source-only inventory over an existing immutable repository snapshot.

Never imports inspected source, probes hosts, starts services or grants execution.
Runtime/host qualification must be established independently of source presence.
"""

from __future__ import annotations

import ast
import base64
import json
import tomllib
from collections.abc import Callable
from typing import Any

from potbot.domain.errors import CapabilityUnavailableError, StateMismatchError
from potbot.domain.serialization import content_hash
from potbot.tools.repository_snapshot import RepositorySnapshot

# These are existing integration owners, not new registries or database services.
OWNERS = {
    "api": "existing authenticated REST/WebSocket service",
    "cogs": "sealed SYN Cog registry",
    "controller": "existing task/job/budget/execution controllers",
    "debugging": "language-neutral session and Python adapter",
    "domain": "existing versioned domain contracts",
    "evaluation": "existing fixture/acceptance harness",
    "memory": "existing evidence and memory service",
    "models": "existing model gateway and operator-selected local tools",
    "persistence": "existing PostgreSQL schemas and artifact store",
    "research": "existing research/retrieval service",
    "security": "existing native credential/signing boundary",
    "semantics": "existing SYN catalog/import/Ember service",
    "tools": "existing ToolGateway and admitted transport implementations",
    "entrypoints": "existing CLI/startup/dependency setup",
    "credentials": "existing credential and service-credential stores",
    "workspace": "existing registered workspace sandbox",
}
ROOT_OWNERS = {
    "__init__.py": "entrypoints",
    "__main__.py": "entrypoints",
    "cli.py": "entrypoints",
    "startup.py": "entrypoints",
    "dependencies.py": "entrypoints",
    "credentials.py": "credentials",
    "service_credentials.py": "credentials",
    "workspace_sandbox.py": "workspace",
}
CORE_ANCHORS = (
    "pyproject.toml",
    "uv.lock",
    "src/potbot/__init__.py",
    "src/potbot/controller/tasks.py",
    "src/potbot/controller/budgets.py",
    "src/potbot/controller/execution_events.py",
    "src/potbot/controller/cog_work.py",
    "src/potbot/persistence/schema.py",
    "src/potbot/persistence/artifacts.py",
    "src/potbot/cogs/registry.py",
    "src/potbot/semantics/service.py",
    "src/potbot/security/broker.py",
    "src/potbot/tools/gateway.py",
    "src/potbot/tools/repository.py",
    "src/potbot/tools/repository_snapshot.py",
    "src/potbot/tools/supervisor.py",
    "src/potbot/workspace_sandbox.py",
    "src/potbot/api/server.py",
)
PYTHON_ANCHORS = (
    "src/potbot/debugging/contracts.py",
    "src/potbot/debugging/dap.py",
    "src/potbot/debugging/session.py",
    "src/potbot/debugging/python.py",
    "src/potbot/debugging/cog_runtime.py",
    "src/potbot/cogs/debugger.py",
)
PROFILES = {"core": CORE_ANCHORS, "python_debugger": (*CORE_ANCHORS, *PYTHON_ANCHORS)}
SPECIALISTS = ("android", "apple", "remote_python", "hardware_debug", "profiling")
TRANSPORTS = {
    "mcp_stdio": "src/potbot/tools/mcp_stdio.py",
    "mcp_http": "src/potbot/tools/mcp_http.py",
    "cli": "src/potbot/tools/cli_adapter.py",
    "http_sdk": "src/potbot/tools/api_http.py",
    "grpc": "src/potbot/tools/api_grpc.py",
    "openapi": "src/potbot/tools/api_openapi.py",
    "python_dap": "src/potbot/debugging/dap.py",
}
STAGES = {
    "api": ("T10.2", "T12.2"),
    "cogs": ("T01.2", "T10.1"),
    "controller": ("T01.3", "T03.4", "T10.2"),
    "debugging": ("T07.3",),
    "domain": ("T01.2", "T01.3"),
    "evaluation": ("T01.4", "T11.1", "T11.4"),
    "memory": ("T08.2", "T10.4"),
    "models": ("T10.3",),
    "persistence": ("T01.3", "T03.4"),
    "research": ("T05.1", "T05.2", "T05.4"),
    "security": ("T03.3",),
    "semantics": ("T01.2", "T10.1"),
    "tools": ("T02.1", "T02.2", "T02.3", "T02.4", "T03.4"),
    "entrypoints": ("T12.1",),
    "credentials": ("T03.3",),
    "workspace": ("T03.2",),
}


def build_inventory(
    snapshot: RepositorySnapshot,
    *,
    profile: str = "core",
    progress: Callable[[str], None] = lambda _: None,
) -> dict[str, Any]:
    if profile not in PROFILES:
        raise CapabilityUnavailableError("Inventory profile is not implemented; no host probe")
    manifest = snapshot.manifest
    if manifest["source_kind"] != "committed":
        raise StateMismatchError("Source inventory requires an explicit committed snapshot")
    # The public manifest deliberately omits content; read bytes only from the
    # already validated immutable snapshot, never the live filesystem.
    files = {entry["path"]: entry for entry in json.loads(snapshot.payload)["files"]}
    if any(path not in files or files[path]["kind"] != "file" for path in PROFILES[profile]):
        raise StateMismatchError("Enabled inventory profile lacks mandatory source files")
    groups: dict[str, list[dict[str, Any]]] = {owner: [] for owner in OWNERS}
    for path, entry in sorted(files.items()):
        if not path.startswith("src/potbot/") or not path.endswith(".py"):
            continue
        relative = path.removeprefix("src/potbot/")
        owner = relative.split("/", 1)[0] if "/" in relative else ROOT_OWNERS.get(relative)
        if owner not in OWNERS or entry["kind"] != "file":
            raise StateMismatchError("Source module lacks an integration owner or is a symlink")
        groups[owner].append({key: entry[key] for key in ("path", "sha256", "size")})

    def source(path: str) -> str:
        entry = files[path]
        if entry["size"] > 1024 * 1024:
            raise StateMismatchError("Inventory metadata source exceeds its parsing bound")
        return base64.b64decode(entry["data"], validate=True).decode("utf-8")

    try:
        project = tomllib.loads(source("pyproject.toml"))["project"]
        runtime_ast = ast.parse(source("src/potbot/__init__.py"))
        version = project["version"]
        if not isinstance(version, str) or not 1 <= len(version) <= 100:
            raise ValueError("bounded string version required")
    except (ValueError, KeyError, TypeError, SyntaxError, UnicodeError):
        raise StateMismatchError(
            "Invalid source version metadata; no inspected code was executed"
        ) from None
    versions = [
        node.value.value
        for node in runtime_ast.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets)
    ]
    if versions != [project["version"]]:
        raise StateMismatchError("Snapshot package/runtime versions disagree")
    components = []
    for index, (owner, entries) in enumerate(groups.items(), 1):
        components.append(
            {
                "id": owner,
                "integration_owner": OWNERS[owner],
                "source_state": "present" if entries else "absent",
                "source_files": entries,
                "stage_refs": list(STAGES[owner]),
                "qualification": "not_established_by_source_inventory",
            }
        )
        progress(f"inventory_components:{index}/{len(groups)}")
    inventory = {
        "schema_version": 1,
        "status": "source_inventory_verified",
        "profile": profile,
        "source": {
            key: manifest[key]
            for key in ("commit_id", "tree_id", "source_kind", "content_digest", "lock_identity")
        },
        "source_version": project["version"],
        "submodule_references": manifest["submodules"],
        "components": components,
        "transports": [
            {
                "id": name,
                "source": path,
                "state": "source_present" if path in files else "missing",
                "callability": "not_established_by_source_inventory",
                "qualification": "not_established_by_source_inventory",
            }
            for name, path in TRANSPORTS.items()
        ],
        "specialist_routes": [
            {"id": name, "state": "unqualified", "enabled": False} for name in SPECIALISTS
        ],
        "extension_map": {
            "database": "extend existing versioned PostgreSQL migrations; no parallel database",
            "identity": "reuse authenticated WorkspaceContext and native security broker",
            "dispatch": "installed SYN Cog -> controller admission -> existing ToolGateway/supervisor",
            "monitoring": "existing SYN execution ledger, CogWorkMonitor, artifacts and shared cursor",
            "snapshots": "existing RepositoryStateStore and canonical RepositorySnapshot",
        },
        "assumptions": [
            "Source presence is not installation, host health, permission or execution qualification.",
            "A transport module without a registered Cog binding is not agent-callable.",
            "The selected committed snapshot may differ from the current working tree or installed runtime.",
            "Inventory hashes are content identities, not signatures or code attestation.",
        ],
    }
    # No timestamp, private source content, absolute paths or host configuration in this projection.
    json.dumps(inventory, allow_nan=False)
    inventory["inventory_sha256"] = content_hash(inventory)
    return inventory
