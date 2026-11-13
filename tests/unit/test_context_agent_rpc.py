"""Real framed streams plus unit contracts. These tests do not qualify Bubblewrap."""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import socket
import struct
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.context.rpc_contract import MAX_FRAME_BYTES, SCHEMA, parse_request
from potbot.context.rpc_transport import response_bytes, serve_context_streams
from potbot.context.sessions import ContextSession, ContextSessionStore, _BoundTransaction
from potbot.context.worker_channel import ContextWorkerChannel
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, RevisionConflictError, ScopeDeniedError


def message(**changes):
    value = {
        "schema": SCHEMA,
        "request_id": str(uuid4()),
        "method": "context.read",
        "scope": "agent",
        "role": "MODEL.md",
    }
    value.update(changes)
    return value


def encoded(value):
    return json.dumps(value).encode()


@pytest.mark.parametrize(
    "extra",
    [
        "agent_id",
        "principal",
        "workspace_id",
        "session_id",
        "generation",
        "path",
        "sql",
        "actor",
        "token",
    ],
)
def test_guest_cannot_select_identity_or_resources(extra):
    with pytest.raises(InvalidInputError):
        parse_request(encoded(message(**{extra: str(uuid4())})))


@pytest.mark.parametrize(
    "value",
    [
        b"[]",
        b"null",
        b"{",
        b"\xff",
        b'{"schema":1,"schema":2}',
        b'{"schema":NaN}',
        b" " * (MAX_FRAME_BYTES + 1),
    ],
)
def test_malformed_or_unbounded_input(value):
    with pytest.raises(InvalidInputError):
        parse_request(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("scope", "../peer"),
        ("role", "model.md"),
        ("method", "context.grant"),
        ("request_id", "UPPERCASE"),
        ("scope", ["agent"]),
    ],
)
def test_exact_role_and_method_contract(field, value):
    with pytest.raises(InvalidInputError):
        parse_request(encoded(message(**{field: value})))


def test_update_contract_and_protected_shared_instructions():
    value = message(
        method="context.update", content="# observed", expected_revision=1, reason="test"
    )
    request = parse_request(encoded(value))
    assert request.fingerprint == parse_request(encoded(value)).fingerprint
    assert (
        request.fingerprint != parse_request(encoded({**value, "reason": "different"})).fingerprint
    )
    for revision in (True, 0, -1, 2**63, 1.0):
        with pytest.raises(InvalidInputError):
            parse_request(encoded({**value, "expected_revision": revision}))
    for content in ("unsafe\x00", "\ud800", None):
        with pytest.raises(InvalidInputError):
            parse_request(encoded({**value, "content": content}))
    # Parsing is not authorization: session execution requires a live, explicit
    # per-role PostgreSQL writer grant (covered by the integration test).
    shared = parse_request(encoded({**value, "scope": "sandbox", "role": "AGENTS.md"}))
    assert shared.role == "AGENTS.md" and shared.scope == "sandbox"


async def pair(handler):
    a, b = socket.socketpair()
    reader, writer = await asyncio.open_connection(sock=a)
    server_reader, server_writer = await asyncio.open_connection(sock=b)
    task = asyncio.create_task(serve_context_streams(server_reader, server_writer, handler))
    return (reader, writer, task)


async def receive(reader):
    size = struct.unpack("!I", await reader.readexactly(4))[0]
    return json.loads((await reader.readexactly(size)).decode())


def test_real_stream_frame_read_and_identity_free_reply():

    async def exercise():
        request = message()
        reader, writer, task = await pair(lambda _: {"revision": 1, "sha256": "a" * 64})
        writer.write(response_bytes(request))
        await writer.drain()
        reply = await receive(reader)
        assert reply["request_id"] == request["request_id"] and reply["ok"] is True
        assert "secret" not in json.dumps(reply)
        writer.close()
        await writer.wait_closed()
        assert (await task).requests == 1

    asyncio.run(exercise())


def test_conflict_is_safe_and_channel_can_continue():

    def handler(request):
        if request.role == "MODEL.md":
            raise RevisionConflictError("postgresql://private-password should never leak")
        return {"revision": 2}

    async def exercise():
        reader, writer, task = await pair(handler)
        writer.write(response_bytes(message()))
        reply = await receive(reader)
        assert reply["error"]["code"] == "revision_conflict"
        assert "password" not in json.dumps(reply)
        writer.write(response_bytes(message(role="MEMORY.md")))
        assert (await receive(reader))["ok"] is True
        writer.close()
        await writer.wait_closed()
        assert (await task).errors == 1

    asyncio.run(exercise())


def test_oversized_header_rejected_without_reading_payload_or_calling_store():
    calls = []

    async def exercise():
        reader, writer, task = await pair(lambda req: calls.append(req))
        writer.write(struct.pack("!I", MAX_FRAME_BYTES + 1))
        assert (await receive(reader))["error"]["code"] == "invalid_input"
        await task
        writer.close()
        await writer.wait_closed()

    asyncio.run(exercise())
    assert calls == []


def test_partial_frame_has_no_effect():
    calls = []

    async def exercise():
        reader, writer, task = await pair(lambda req: calls.append(req))
        writer.write(struct.pack("!I", 100) + b"{")
        writer.write_eof()
        assert (await receive(reader))["ok"] is False
        await task
        writer.close()
        await writer.wait_closed()

    asyncio.run(exercise())
    assert calls == []


def test_cancelled_channel_settles_inflight_dispatch_before_returning():
    entered, release, finished = (threading.Event(), threading.Event(), threading.Event())

    def handler(_):
        entered.set()
        assert release.wait(3)
        finished.set()
        return {"revision": 2}

    async def exercise():
        reader, writer, task = await pair(handler)
        writer.write(response_bytes(message()))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        writer.close()
        await writer.wait_closed()

    asyncio.run(exercise())


def test_borrowed_transaction_cannot_switch_principal():
    actor = WorkspaceContext(uuid4(), uuid4())
    conn = object()
    db = _BoundTransaction(actor, conn)
    with db.transaction(actor) as borrowed:
        assert borrowed is conn
    with (
        pytest.raises(ScopeDeniedError),
        db.transaction(WorkspaceContext(actor.workspace_id, uuid4())),
    ):
        pass


class QueryResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class SessionConnection:
    def __init__(self, grant):
        import hashlib

        self.now = datetime.now(UTC)
        self.row = {
            "agent_id": grant.agent_id,
            "run_id": grant.run_id,
            "secret_sha256": hashlib.sha256(grant.secret.encode()).hexdigest(),
            "state": "active",
            "generation": 1,
            "expires_at": self.now + timedelta(seconds=30),
        }
        self.queries = []

    def execute(self, query, params=()):
        self.queries.append(query)
        return QueryResult({"now": self.now} if "clock_timestamp" in query else self.row)


def test_session_secret_generation_expiry_and_post_lock_clock():
    grant = ContextSession(
        WorkspaceContext(uuid4(), uuid4()), uuid4(), uuid4(), uuid4(), 1, "a" * 43
    )
    conn = SessionConnection(grant)
    store = ContextSessionStore(None)
    assert grant.secret not in repr(grant)
    assert store._lock(conn, grant)["generation"] == 1
    assert "FOR UPDATE" in conn.queries[0] and "clock_timestamp" in conn.queries[1]
    for bad in (
        replace(grant, secret="b" * 43),
        replace(grant, generation=2),
        replace(grant, agent_id=uuid4()),
        replace(grant, run_id=uuid4()),
    ):
        with pytest.raises(ScopeDeniedError):
            store._lock(conn, bad)
    conn.row["expires_at"] = conn.now
    with pytest.raises(ScopeDeniedError):
        store._lock(conn, grant)
    conn.row["expires_at"] = conn.now + timedelta(seconds=30)
    conn.row["state"] = "revoked"
    with pytest.raises(ScopeDeniedError):
        store._lock(conn, grant)


def test_guest_client_lost_reply_retains_request_id():
    path = Path(__file__).resolve().parents[2] / "sandbox/clients/context_client.py"
    spec = importlib.util.spec_from_file_location("guest_context_client", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    key = str(uuid4())
    out = io.BytesIO()
    client = module.ContextClient(io.BytesIO(), out)
    with pytest.raises(module.ContextRPCError) as caught:
        client.update("agent", "MODEL.md", "# State", expected_revision=1, request_id=key)
    assert caught.value.code == "indeterminate" and caught.value.request_id == key
    wire = out.getvalue()
    assert json.loads(wire[4:])["request_id"] == key


def test_worker_binding_rejects_wrong_run_identity_and_never_issues_session(tmp_path):
    principal, agent = (WorkspaceContext(uuid4(), uuid4()), uuid4())
    root = tmp_path / "home"
    home = root / "alice"
    home.mkdir(parents=True)
    ledger = SimpleNamespace(home=root, image_status=lambda _: {"digest": "a" * 64})
    guard = SimpleNamespace(
        ledger=ledger,
        lease=SimpleNamespace(workspace_id=principal.workspace_id),
        request=SimpleNamespace(
            workspace_id=principal.workspace_id, actor_id=agent, image_digest="a" * 64
        ),
        ensure_live=lambda: None,
    )
    registry = SimpleNamespace(
        ledger=ledger,
        lookup=lambda *args: SimpleNamespace(
            directory="alice", uid=420, gid=69, workspace_id=principal.workspace_id, agent_id=agent
        ),
    )
    request = SimpleNamespace(
        telemetry_workspace_id=principal.workspace_id,
        telemetry_agent_id=uuid4(),
        telemetry_identity_kind="agent",
        telemetry_run_id=uuid4(),
        guest_uid=420,
        guest_gid=69,
        stdin=b"",
        ipc_profile="none",
        read_only_roots=(),
        writable_roots=(),
        workspace_root=root,
        sandbox_root=root,
        cwd=home,
    )
    process = SimpleNamespace(closed=False, request=request, isolation_identity="b" * 64)
    owner = ContextWorkerChannel(
        store=None,
        principal=principal,
        agent_id=agent,
        guard=guard,
        registry=registry,
        check_operator=lambda: None,
    )
    with pytest.raises(ScopeDeniedError):
        owner._validate_process(process)
    request.telemetry_agent_id = agent
    assert owner._validate_process(process) == request.telemetry_run_id
    guard.request.actor_id = uuid4()
    with pytest.raises(ScopeDeniedError):
        owner._validate_process(process)


def test_unknown_error_never_reflects_exception_text():

    def fail(_):
        raise RuntimeError("SECRET=/host/operator/credentials password=do-not-reflect")

    async def exercise():
        reader, writer, task = await pair(fail)
        writer.write(response_bytes(message()))
        reply = await receive(reader)
        assert reply["error"] == {
            "code": "internal_error",
            "message": "Context operation unavailable",
        }
        assert (await task).errors == 1
        writer.close()
        await writer.wait_closed()

    asyncio.run(exercise())


def test_client_and_host_exchange_over_real_subprocess_pipes(tmp_path):
    import hashlib
    import sys

    client_file = Path(__file__).resolve().parents[2] / "sandbox/clients/context_client.py"
    program = "import sys;sys.path.insert(0,sys.argv[1]);from context_client import ContextClient;c=ContextClient();d=c.read('agent');assert d['content']=='# Current';r=c.update('agent','MODEL.md','# Updated',expected_revision=d['revision'],request_id=sys.argv[2]);assert r['revision']==2"
    requests = []

    def execute(request):
        requests.append(request)
        if request.method == "context.read":
            return {
                "revision": 1,
                "sha256": hashlib.sha256(b"# Current").hexdigest(),
                "content": "# Current",
                "role": "MODEL.md",
                "object_id": str(uuid4()),
            }
        return {
            "revision": 2,
            "sha256": hashlib.sha256(b"# Updated").hexdigest(),
            "role": "MODEL.md",
            "object_id": str(uuid4()),
        }

    async def exercise():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            program,
            str(client_file.parent),
            str(uuid4()),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        report = await serve_context_streams(process.stdout, process.stdin, execute)
        assert await asyncio.wait_for(process.wait(), 3) == 0, (
            await process.stderr.read()
        ).decode()
        assert report.requests == 2 and report.errors == 0

    asyncio.run(exercise())
    assert [r.method for r in requests] == ["context.read", "context.update"]


@pytest.mark.parametrize("deadline", [True, None, "2", float("nan"), float("inf"), 0, 61])
def test_transport_rejects_invalid_deadline(deadline):

    async def exercise():
        with pytest.raises(InvalidInputError):
            await serve_context_streams(None, None, lambda _: {}, frame_timeout=deadline)

    asyncio.run(exercise())


def test_precommit_expiry_check_does_not_use_transaction_start_time():
    grant = ContextSession(
        WorkspaceContext(uuid4(), uuid4()), uuid4(), uuid4(), uuid4(), 1, "a" * 43
    )
    conn = SessionConnection(grant)
    store = ContextSessionStore(None)
    snapshot = store._lock(conn, grant)
    conn.now += timedelta(seconds=31)
    with pytest.raises(ScopeDeniedError):
        store._check_expiry(conn, snapshot)


@pytest.mark.parametrize("cancel_cleanup", [False, True])
def test_worker_channel_revokes_after_confirmed_process_shutdown(tmp_path, cancel_cleanup):
    principal, agent = (WorkspaceContext(uuid4(), uuid4()), uuid4())
    root = tmp_path / "home"
    home = root / "alice"
    home.mkdir(parents=True)
    events = []
    ledger = SimpleNamespace(home=root, image_status=lambda _: {"digest": "a" * 64})
    guard = SimpleNamespace(
        ledger=ledger,
        lease=SimpleNamespace(workspace_id=principal.workspace_id),
        request=SimpleNamespace(
            workspace_id=principal.workspace_id, actor_id=agent, image_digest="a" * 64
        ),
        ensure_live=lambda: None,
    )
    registry = SimpleNamespace(
        ledger=ledger,
        lookup=lambda *args: SimpleNamespace(
            directory="alice", uid=420, gid=69, workspace_id=principal.workspace_id, agent_id=agent
        ),
    )
    request = SimpleNamespace(
        telemetry_workspace_id=principal.workspace_id,
        telemetry_agent_id=agent,
        telemetry_identity_kind="agent",
        telemetry_run_id=uuid4(),
        guest_uid=420,
        guest_gid=69,
        stdin=b"",
        ipc_profile="none",
        read_only_roots=(),
        writable_roots=(),
        workspace_root=root,
        sandbox_root=root,
        cwd=home,
    )

    class Store:
        def ensure_agent_context(self, actor, agent_id):
            assert actor == principal and agent_id == agent
            events.append("provision")

        def issue(self, actor, agent_id, run_id):
            events.append("issue")
            return ContextSession(actor, uuid4(), agent_id, run_id, 1, "a" * 43)

        def execute(self, grant, req):
            events.append("read")
            return {"revision": 1}

        def revoke(self, grant):
            events.append("revoke")

        def renew(self, grant):
            events.append("renew")

    async def exercise():
        a, b = socket.socketpair()
        reader, writer = await asyncio.open_connection(sock=a)
        sr, sw = await asyncio.open_connection(sock=b)
        closing, release = (asyncio.Event(), asyncio.Event())

        class Process:
            closed = False
            isolation_identity = "b" * 64

            def remaining_seconds(self):
                return 60

            async def close(self, *, force=False):
                closing.set()
                if cancel_cleanup:
                    await release.wait()
                events.append("stopped")
                self.closed = True
                return SimpleNamespace(processes_stopped=True)

        process = Process()
        process.request = request
        process.reader = sr
        process.writer = sw
        owner = ContextWorkerChannel(
            store=Store(),
            principal=principal,
            agent_id=agent,
            guard=guard,
            registry=registry,
            check_operator=lambda: None,
        )
        task = asyncio.create_task(owner.serve(process))
        writer.write(response_bytes(message()))
        assert (await receive(reader))["ok"] is True
        writer.close()
        await writer.wait_closed()
        await closing.wait()
        if cancel_cleanup:
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result.exit.processes_stopped and result.channel.requests == 1
        with pytest.raises(ScopeDeniedError):
            await owner.serve(process)

    asyncio.run(exercise())
    assert events == ["provision", "issue", "read", "stopped", "revoke"]


def test_client_reports_parse_denial_without_inventing_an_indeterminate_write():
    path = Path(__file__).resolve().parents[2] / "sandbox/clients/context_client.py"
    spec = importlib.util.spec_from_file_location("guest_client_denial", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    reply = response_bytes(
        {
            "schema": SCHEMA,
            "request_id": None,
            "ok": False,
            "error": {"code": "scope_denied", "message": "denied"},
        }
    )
    client = module.ContextClient(io.BytesIO(reply), io.BytesIO())
    with pytest.raises(module.ContextRPCError) as caught:
        client.update("sandbox", "AGENTS.md", "# bad", expected_revision=1, request_id=str(uuid4()))
    assert caught.value.code == "scope_denied"


def test_mutation_request_without_explicit_reason_or_revision_is_not_accepted():
    good = message(method="context.update", content="# Updated", expected_revision=1, reason="")
    for name in ("reason", "expected_revision", "request_id"):
        bad = {k: v for k, v in good.items() if k != name}
        with pytest.raises(InvalidInputError):
            parse_request(encoded(bad))


def test_rest_worker_discloses_and_acknowledges_only_the_observed_value():
    from types import SimpleNamespace

    from potbot.context.rpc_contract import WorkRequest
    from potbot.context.work_exchange import ContextWorkExchange
    from potbot.domain.errors import IndeterminateExecutionError, StateMismatchError
    from potbot.tools.rest_cog import REQUEST, RESULT

    context = {
        "schema_version": 1,
        "operation_ref": "syn://thing/test-rest",
        "invocation_id": str(uuid4()),
        "input": {"type_ref": REQUEST, "value": {"arguments": {"query": {"secret": "input"}}}},
    }
    result = {"status": "succeeded", "value": {"count": 1}, "observation_sha256": "a" * 64}
    calls = []

    def provide():
        calls.append("GET")
        return result

    store = SimpleNamespace(admit_work_read=lambda _: None, complete_work=lambda *_: {"ok": True})
    grant = object()
    exchange = ContextWorkExchange(context, RESULT, work_only=True, provide_value=provide)
    request = WorkRequest(uuid4(), "work.input")
    assert exchange.execute(store, grant, request)["work"]["input"] == {
        "type_ref": RESULT,
        "value": result,
    }
    with pytest.raises(StateMismatchError, match="already requested"):
        exchange.execute(store, grant, request)

    def acknowledge(value):
        return exchange.execute(
            store,
            grant,
            WorkRequest(
                uuid4(),
                "work.result",
                {"schema_version": 1, "type_ref": RESULT, "value": value},
            ),
        )

    with pytest.raises(ScopeDeniedError, match="retained REST observation"):
        acknowledge({**result, "value": {"count": True}})
    assert acknowledge(result) == {"ok": True}
    assert calls == ["GET"]

    def uncertain():
        calls.append("GET")
        raise IndeterminateExecutionError("Fixture lost upstream response")

    exchange = ContextWorkExchange(context, RESULT, work_only=True, provide_value=uncertain)
    with pytest.raises(IndeterminateExecutionError):
        exchange.execute(store, grant, request)
    assert exchange.provider_uncertain
    with pytest.raises(StateMismatchError, match="already requested"):
        exchange.execute(store, grant, request)
    assert calls == ["GET", "GET"]
