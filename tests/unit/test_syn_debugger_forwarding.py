import asyncio
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.cogs.contracts import CogReceipt, CogRunContext, TypedCogValue
from potbot.cogs.debugger import cog_ref, signature
from potbot.debugging.syn_forwarding import (
    TOPIC,
    DebuggerInvocationClaims,
    SynDebuggerRoute,
    debugger_group_id,
)
from potbot.domain.errors import IndeterminateExecutionError, ScopeDeniedError

OPERATOR = "a" * 64
CLIENT = "b" * 64
SERVER = "c" * 64
BOOT = "d" * 32


class Endpoint:
    node = CLIENT

    def __init__(self, workspace, session, messages):
        self.scope = {
            "workspace_id": str(workspace),
            "operator": OPERATOR,
            "group_id": str(debugger_group_id(OPERATOR)),
            "session_id": session,
            "members": [{"body": {"node_public_key_hash": node}} for node in (CLIENT, SERVER)],
        }
        self.messages = messages
        self.sent = []

    def call(self, op, payload):
        if op == "publish":
            self.sent.append(payload)
            return {"relay_accepted": True}
        if self.messages is None:
            raise OSError("relay dropped")
        return {"messages": self.messages.pop(0) if self.messages else []}


def test_forwarding_discards_old_signed_reply_and_does_not_resend():
    async def scenario():
        workspace, invocation = uuid4(), uuid4()
        selected = signature("threads")
        context = CogRunContext("e" * 64, uuid4(), uuid4(), invocation)
        source = TypedCogValue(selected.input_type_ref, {"schema_version": 1})
        output = TypedCogValue(selected.output_type_ref, {"threads": []})
        receipt = CogReceipt(
            str(selected.cog_ref),
            selected.cog_type_ref,
            selected.binding_hash,
            context.cog_snapshot_hash,
            context.task_revision_id,
            context.repository_state_id,
            invocation,
            source.type_ref,
            source.fingerprint,
            output.type_ref,
            output.fingerprint,
            None,
            None,
        )

        def reply(session, body_session, payload):
            return {
                "envelope": {
                    "header": {
                        "node": SERVER,
                        "boot_id": BOOT,
                        "topic": TOPIC,
                        "workspace_id": str(workspace),
                        "session_id": session,
                    }
                },
                "payload": {
                    "kind": "reply",
                    "session": body_session,
                    "to": CLIENT,
                    "invocation_id": str(invocation),
                    "receipt": receipt.to_dict(),
                    "value": asdict(output) | {"value": payload},
                },
            }

        previous = Endpoint(workspace, "old", None)
        route = SynDebuggerRoute(previous, operator=OPERATOR, peer=SERVER, peer_boot=BOOT)
        with pytest.raises(IndeterminateExecutionError):
            await route.invoke(cog_ref("threads"), source, context)
        with pytest.raises(IndeterminateExecutionError):
            await route.invoke(cog_ref("threads"), source, context)
        assert len(previous.sent) == 1

        fresh = Endpoint(
            workspace,
            "fresh",
            [[reply("old", "fresh", {"invalid": True})], [reply("fresh", "fresh", output.value)]],
        )
        route = SynDebuggerRoute(fresh, operator=OPERATOR, peer=SERVER, peer_boot=BOOT)
        result = await route.invoke(cog_ref("threads"), source, context)
        assert result.value == output
        assert len(fresh.sent) == 1
        with pytest.raises(ScopeDeniedError):
            await route.invoke(cog_ref("evaluate"), source, context)

    asyncio.run(scenario())


@pytest.mark.skipif(__import__("os").name == "nt", reason="native POSIX claim directory")
def test_invocation_claim_survives_new_forwarding_lifetime(tmp_path):
    root = Path(tmp_path)
    root.chmod(0o700)
    workspace, invocation = uuid4(), uuid4()
    assert DebuggerInvocationClaims(root, workspace).claim(invocation)
    assert not DebuggerInvocationClaims(root, workspace).claim(invocation)
    assert DebuggerInvocationClaims(root, uuid4()).claim(invocation)
