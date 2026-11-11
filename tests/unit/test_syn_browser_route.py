from uuid import uuid4

import pytest

from potbot.domain.errors import StateMismatchError
from potbot.tools.syn_browser_route import SynBrowserRoute


@pytest.mark.parametrize("lost_exchange", [1, 2, None])
def test_browser_executes_only_after_signed_delivery_and_never_retries(lost_exchange):
    workspace, boot, uid = uuid4(), "a" * 32, 1000
    calls, effects = [], []

    def exchange(ident, payload):
        calls.append(ident)
        header = {
            "profile": "syn.local-rpc-dispatch/1",
            "workspace_id": str(workspace),
            "method": "browser.command",
            "request_id": str(ident),
            "boot_id": boot,
            "caller_uid": uid,
            "size": len(payload),
        }
        reply = route.accept(header, payload)
        if len(calls) == lost_exchange:
            raise TimeoutError("test dropped the signed reply")
        return reply, boot

    def execute(command):
        effects.append(command)
        return {"acted": True}

    command = {"op": "role.click", "role": "button", "name": "Count"}
    route = SynBrowserRoute(workspace, boot, uid, exchange)
    if lost_exchange is None:
        assert route.call(command, execute) == {"acted": True}
    else:
        with pytest.raises(StateMismatchError, match="indeterminate"):
            route.call(command, execute)
    assert effects == ([] if lost_exchange == 1 else [command])
    assert len(calls) == len(set(calls)) == (lost_exchange or 2)
