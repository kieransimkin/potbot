"""Sealed ports and pinned guest acknowledgement for installed HTTPS reads."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from potbot.domain.errors import ScopeDeniedError
from potbot.domain.serialization import content_hash

if TYPE_CHECKING:
    from potbot.tools.api_https import InstalledRestRead
    from potbot.tools.execution_contracts import ExecutionPolicy
    from potbot.tools.sandbox_cog_worker import SandboxedCogOperation

REQUEST = "syn://type/potbot-rest-read-request-v1"
RESULT = "syn://type/potbot-rest-read-result-v1"
INTERPRETER = "/usr/bin/python3.11"
WORKER_SOURCE = """import hashlib
import json
import sys
from pathlib import Path
from uuid import uuid4

path = Path("/opt/potbot/context_client.py")
raw = path.read_bytes()
if hashlib.sha256(raw).hexdigest() != sys.argv[1]:
    raise SystemExit("Context client differs from its installed digest")
module = type(sys)("potbot_guest_context")
sys.modules[module.__name__] = module
exec(compile(raw, str(path), "exec"), module.__dict__)
client = module.ContextClient()
request = client.work_input()
if request["operation_ref"] != sys.argv[2] or request["input"]["type_ref"] != sys.argv[3]:
    raise SystemExit("REST worker received a different typed operation")
result = request["input"]["value"]
checksum = hashlib.sha256(json.dumps(
    {"schema_version": 1, "type_ref": sys.argv[3], "value": result},
    sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
).encode()).hexdigest()
receipt = client.finish_work(sys.argv[3], result, request_id=str(uuid4()))
if receipt != {"revision": 1, "sha256": checksum}:
    raise SystemExit("REST worker did not receive its result receipt")
"""


def schemas() -> dict[str, dict[str, Any]]:
    from potbot.tools.semantic_contracts import HASH, obj

    return {
        REQUEST: obj({"arguments": {"type": "object"}}),
        RESULT: obj(
            {
                "status": {"enum": ["succeeded", "failed"]},
                "value": {},
                "observation_sha256": HASH,
            }
        ),
    }


def rest_operation_ref(read: InstalledRestRead) -> str:
    return "syn://thing/potbot-rest-read-" + read.installation.fingerprint


def install_rest_cog(
    read: InstalledRestRead,
    policy: ExecutionPolicy,
    executable_sha256: str,
    client_sha256: str,
) -> SandboxedCogOperation:
    from potbot.tools.execution_contracts import WorkBinding, digest
    from potbot.tools.sandbox_cog_worker import SandboxedCogOperation

    operation = read.operation
    if (
        policy.policy_revision_hash != read.installation.policy_revision_hash
        or policy.configuration_hash != read.installation.configuration_hash
        or operation.timeout_ms >= policy.max_wall_ms
        or operation.max_output_bytes + 2048 > policy.max_output_bytes
    ):
        raise ScopeDeniedError("REST worker policy differs from the installed operation bounds")
    operation_ref = rest_operation_ref(read)
    argv = (
        INTERPRETER,
        "-I",
        "-B",
        "-c",
        WORKER_SOURCE,
        digest(client_sha256),
        operation_ref,
        RESULT,
    )
    return SandboxedCogOperation(
        WorkBinding(
            "api",
            operation_ref,
            REQUEST,
            RESULT,
            content_hash(
                {
                    "installation": read.installation.fingerprint,
                    "argv": argv,
                    "executable_sha256": executable_sha256,
                }
            ),
            "read_only",
            policy,
        ),
        argv,
        executable_sha256,
        protocol="context-rpc/v1",
    )


def require_rest_cog(read: InstalledRestRead, op: SandboxedCogOperation) -> None:
    if (
        not op.directed_rest
        or len(op.argv) != 8
        or op != install_rest_cog(read, op.binding.policy, op.executable_sha256, op.argv[5])
    ):
        raise ScopeDeniedError("REST read requires its exact installed Syn worker")
