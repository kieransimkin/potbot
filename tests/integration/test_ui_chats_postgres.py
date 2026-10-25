"""Real PostgreSQL durability, idempotency and workspace/principal isolation."""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from potbot.api.ui_chats import UiChatStore
from potbot.domain.errors import RevisionConflictError, ScopeDeniedError

pytestmark = pytest.mark.postgres


def test_durable_multi_turn_history_with_pending_fence(app_env):
    store = UiChatStore(app_env.db)
    session = uuid4()
    created = store.create_session(app_env.context, session_id=session, title="First conversation")
    assert created["created"]
    assert store.create_session(app_env.context, session_id=session, title="First conversation")["created"] is False
    assert store.list_sessions(app_env.context)[0]["id"] == str(session)
    assert UiChatStore(app_env.other_db).list_sessions(app_env.other_context) == []
    with pytest.raises(ScopeDeniedError):
        UiChatStore(app_env.other_db).read_session(app_env.other_context, session)
    request_id = uuid4()
    first = store.start_turn(app_env.context, session, request_id=request_id, text="Research a model")
    assert first["created"] and first["messages"] == [{"role": "user", "content": "Research a model"}]
    assert store.start_turn(app_env.context, session, request_id=request_id, text="Research a model")["created"] is False
    with pytest.raises(RevisionConflictError):
        store.start_turn(app_env.context, session, request_id=uuid4(), text="Concurrent request")
    assert store.finish_turn(app_env.context, session, UUID(first["id"]), status="completed",
                             assistant_text="I recommend benchmarking.", run_id="a"*32)["state"] == "completed"
    second = store.start_turn(app_env.context, session, request_id=uuid4(), text="What about VRAM?")
    assert second["messages"] == [
        {"role": "user", "content": "Research a model"},
        {"role": "assistant", "content": "I recommend benchmarking."},
        {"role": "user", "content": "What about VRAM?"},
    ]
    store.finish_turn(app_env.context, session, UUID(second["id"]), status="indeterminate",
                      assistant_text=None, run_id=None)
    assert store.read_session(app_env.context, session)["turns"][-1]["state"] == "indeterminate"
    with pytest.raises(RevisionConflictError):
        store.finish_turn(app_env.context, session, UUID(second["id"]), status="completed",
                          assistant_text="Invented", run_id=None)
    third = store.start_turn(app_env.context, session, request_id=uuid4(), text="Continue using verified data")
    assert all(item["content"] != "What about VRAM?" for item in third["messages"])
