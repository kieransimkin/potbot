"""S37 browser Cog security/receipt tests, no live browser or host shell."""
from __future__ import annotations

import base64
import hashlib
import json
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from potbot.controller.browser_cogs import BrowserControlService
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    IndeterminateExecutionError, InvalidInputError, ScopeDeniedError, StateMismatchError,
)
from potbot.tools.browser_cog_contracts import (
    ACT_REQUEST, CAPTURE_REQUEST, EVIDENCE_RESULT, OBSERVE_REQUEST, PICK_REQUEST,
    schemas, validate_request,
)
from potbot.tools.execution_contracts import ExecutionPolicy
from potbot.tools.xpra_app_sessions import XpraApplicationSupervisor
from potbot.tools.xpra_browser_worker import browser_operation_may_mutate

SESSION = "a" * 32
POLICY_SHA = "f" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"safe synthetic jpeg" + b"\xff\xd9"


class Artifacts:
    def __init__(self):
        self.records = {}
        self.fail = False

    def put(self, actor, raw, media_type):
        if self.fail:
            raise OSError("fixture evidence backend offline")
        identifier = str(uuid4())
        self.records[identifier] = (actor, raw, media_type)
        return {"id": identifier, "digest": hashlib.sha256(raw).hexdigest()}


class Apps(XpraApplicationSupervisor):
    def __init__(self, actor):
        self.workspace_id, self.principal_id = actor.workspace_id, actor.principal_id
        self.commands = []
        self.fenced = []
        self.result = {"aria": "- button \"Save\"\n- textbox \"Username\"",
                       "url": "https://fixture.test/settings", "title": "Fixture"}

    def browser_command(self, session, command):
        self.commands.append((session, command))
        if self.fenced:
            raise StateMismatchError("already fenced")
        return self.result

    def fence_browser(self, session):
        self.fenced.append(session)


def policy():
    return ExecutionPolicy(POLICY_SHA, POLICY_SHA, POLICY_SHA, "s37-test-v1")


def setup(preview=False):
    actor = WorkspaceContext(uuid4(), uuid4())
    artifacts, apps = Artifacts(), Apps(actor)
    service = BrowserControlService(actor=actor, apps=apps, artifacts=artifacts,
        policy=policy(), approved_sessions=(SESSION,), allow_model_preview=preview)
    return actor, artifacts, apps, service


def request(command, session=SESSION):
    return {"schema_version": 1, "session_id": session, "command": command}


def test_four_typed_ports_have_real_valid_json_schemas():
    found = schemas()
    assert set(found) == {ACT_REQUEST, OBSERVE_REQUEST, PICK_REQUEST,
                          CAPTURE_REQUEST, EVIDENCE_RESULT}
    for value in found.values():
        Draft202012Validator.check_schema(value)
    Draft202012Validator(found[OBSERVE_REQUEST]).validate(request({"op": "accessibility"}))
    with pytest.raises(Exception):
        Draft202012Validator(found[OBSERVE_REQUEST]).validate(request({"op": "accessibility"}, "bad"))


@pytest.mark.parametrize(("group", "command"), [
    ("observe", {"op": "role.click", "role": "button", "name": "Submit"}),
    ("act", {"op": "accessibility"}),
    ("pick", {"op": "input.keyboard", "action": "press", "key": "Enter"}),
    ("capture", {"op": "sample", "image": False}),
    ("observe", {"op": "cdp", "method": "Input.dispatchMouseEvent", "params": {
        "type": "mouseMoved", "x": 1, "y": 1}}),
    ("act", {"op": "cdp", "method": "DOM.getDocument"}),
    ("observe", {"op": "cdp", "method": "Runtime.evaluate", "params": {
        "expression": "document.cookie"}}),
])
def test_effect_groups_cannot_bypass_their_cog_port(group, command):
    with pytest.raises((ScopeDeniedError, InvalidInputError)):
        validate_request(group, request(command))


def test_accessibility_metadata_private_and_preview_opt_in():
    actor, storage, apps, service = setup()
    op = {"op": "accessibility"}
    receipt = service.dispatch("observe", request(op), uuid4(), actor)
    assert receipt["status"] == "recorded"
    assert receipt["kind"] == "observation"
    assert receipt["preview"] is None
    assert receipt["image_artifact_id"] is None
    assert len(storage.records) == 2  # observation and manifest, not JPEG
    manifest = json.loads(storage.records[receipt["manifest_artifact_id"]][1])
    assert manifest["workspace_id"] == str(actor.workspace_id)
    assert manifest["principal_id"] == str(actor.principal_id)
    assert manifest["session_id"] == SESSION
    assert manifest["sensitive"] is True
    assert manifest["observation"]["sha256"] == receipt["observation_sha256"]
    assert apps.commands == [(SESSION, op)]

    actor2, _, _, opt_in = setup(preview=True)
    got = opt_in.dispatch("observe", request(op), uuid4(), actor2)
    assert got["preview"] == {"aria": apps.result["aria"],
                               "url": apps.result["url"], "title": "Fixture"}


def test_screenshot_verified_and_not_in_cog_output():
    actor, storage, apps, service = setup(preview=True)
    apps.result = {"frame": {"mime": "image/jpeg", "sha256": hashlib.sha256(JPEG).hexdigest(),
                             "bytes": len(JPEG), "base64": base64.b64encode(JPEG).decode()},
                   "aria": "- button \"Save\"", "url": "file:///home/site/index.html"}
    receipt = service.dispatch("capture", request({"op": "sample", "image": True}), uuid4(), actor)
    assert receipt["image_artifact_id"]
    assert receipt["preview"] is None
    image = storage.records[receipt["image_artifact_id"]]
    assert image[1] == JPEG and image[2] == "image/jpeg" and image[0] == actor
    observation = storage.records[receipt["observation_artifact_id"]][1]
    assert b"base64" not in observation and b"safe synthetic jpeg" not in observation
    assert len(storage.records) == 3
    Draft202012Validator(schemas()[EVIDENCE_RESULT]).validate(receipt)


def test_forged_image_digest_or_bad_jpeg_refused():
    actor, storage, apps, service = setup()
    apps.result = {"frame": {"mime": "image/jpeg", "sha256": "0" * 64,
                             "bytes": len(JPEG), "base64": base64.b64encode(JPEG).decode()}}
    with pytest.raises(InvalidInputError):
        service.dispatch("capture", request({"op": "sample"}), uuid4(), actor)
    assert not storage.records


def test_mutating_action_evidence_write_failure_fences_session():
    actor, storage, apps, service = setup()
    storage.fail = True
    with pytest.raises(IndeterminateExecutionError):
        service.dispatch("act", request({"op": "role.click", "role": "button", "name": "Submit"}),
                         uuid4(), actor)
    assert apps.fenced == [SESSION]
    assert len(apps.commands) == 1


def test_action_rpc_failure_also_fences_session():
    actor, _, apps, service = setup()
    def fail(*args):
        raise StateMismatchError("channel died")
    apps.browser_command = fail
    with pytest.raises(IndeterminateExecutionError):
        service.dispatch("act", request({"op": "navigate", "url": "about:blank"}), uuid4(), actor)
    assert apps.fenced == [SESSION]


def test_scoped_sessions_and_invocation_ids_are_one_use():
    actor, storage, apps, service = setup()
    id_ = uuid4()
    cmd = request({"op": "state"})
    service.dispatch("observe", cmd, id_, actor)
    with pytest.raises(StateMismatchError):
        service.dispatch("observe", cmd, id_, actor)
    with pytest.raises(ScopeDeniedError):
        service.dispatch("observe", request({"op": "state"}, "b" * 32), uuid4(), actor)
    with pytest.raises(ScopeDeniedError):
        service.dispatch("observe", cmd, uuid4(), WorkspaceContext(uuid4(), actor.principal_id))
    assert len(apps.commands) == 1


def test_mutation_exception_classes_in_the_browser_worker():
    assert browser_operation_may_mutate({"op": "role.click"})
    assert browser_operation_may_mutate({"op": "input.pointer", "action": "click"})
    assert browser_operation_may_mutate({"op": "cdp", "method": "Input.dispatchKeyEvent"})
    assert not browser_operation_may_mutate({"op": "accessibility"})
    assert not browser_operation_may_mutate({"op": "cdp", "method": "DOM.getDocument"})


def test_snapshot_exposes_installed_browser_cogs_only():
    from potbot.cogs.browser import BROWSER_COGS, browser_cog_snapshot
    from potbot.cogs.registry import BoundCogRegistry
    from potbot.cogs.execution import EXECUTION_COGS
    catalog = BoundCogRegistry.browser()
    assert {str(x.cog_ref) for x in BROWSER_COGS} <= {
        str(x.cog_ref) for x in catalog.installed()
    }
    assert catalog.snapshot.fingerprint == browser_cog_snapshot().fingerprint
    assert len(catalog.installed()) == 4 + len(EXECUTION_COGS) + len(
        BoundCogRegistry.builtins().installed())


def test_post_click_worker_error_is_fenced_instead_of_definite_rejection():
    """A failed Playwright wait *after* clicking must not allow a retry."""
    import subprocess
    import sys
    from potbot.tools.xpra_browser_control import BrowserControlChannel

    script = """import json,sys
for line in sys.stdin:
    request=json.loads(line)
    print(json.dumps({'id':request['id'], 'ok':False,
                      'error':'browser command was rejected or failed',
                      'indeterminate':True}), flush=True)
"""
    process = subprocess.Popen([sys.executable, "-u", "-c", script],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        channel = BrowserControlChannel(process)
        command = {"op": "role.click", "role": "button", "name": "Submit"}
        with pytest.raises(StateMismatchError, match="indeterminate"):
            channel.call(command, timeout=2)
        with pytest.raises(StateMismatchError):
            channel.call({"op": "state"}, timeout=1)
        channel.close()
    finally:
        process.terminate()
        process.wait(timeout=3)


def test_old_worker_failure_without_uncertain_flag_is_also_fenced():
    """An old guest image cannot downgrade S37's mutation safety policy."""
    import subprocess
    import sys
    from potbot.tools.xpra_browser_control import BrowserControlChannel

    script = """import json,sys
for line in sys.stdin:
    cmd=json.loads(line)
    print(json.dumps({'id':cmd['id'],'ok':False,'error':'legacy worker failed'}),flush=True)
"""
    process = subprocess.Popen([sys.executable, "-u", "-c", script],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        channel = BrowserControlChannel(process)
        with pytest.raises(StateMismatchError):
            channel.call({"op": "role.click", "role": "button", "name": "Submit"}, timeout=2)
        with pytest.raises(StateMismatchError):
            channel.call({"op": "state"}, timeout=1)
        channel.close()
    finally:
        process.terminate()
        process.wait(timeout=3)
