"""REST and live WebSocket delivery over real scoped PostgreSQL transactions."""

import json
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from potbot.api.server import ServerSettings, create_app
from potbot.api.tasks import DevelopmentBearerAuth
from potbot.controller.jobs import JobService
from potbot.controller.tasks import TaskService
from potbot.domain.tasks import ContractSpec, DeliverableType, Requirement

pytestmark = pytest.mark.postgres


def envelope(payload, key):
    return {
        "schema_version": 1,
        "request_id": str(uuid4()),
        "idempotency_key": key,
        "payload": payload,
    }


def test_http_submission_worker_events_cancellation_replay_and_scope(app_env) -> None:
    token = "api-test-" + "a" * 40
    other_token = "other-api-test-" + "b" * 40
    app = create_app(
        task_service=TaskService(app_env.db),
        job_service=JobService(app_env.db),
        authenticate=DevelopmentBearerAuth(
            {token: app_env.context, other_token: app_env.other_context}
        ),
        settings=ServerSettings(allowed_origins=("http://testserver",), poll_interval_seconds=0.01),
    )
    contract = ContractSpec(
        objective="Observe a model job",
        baseline_ref="git:" + "1" * 40,
        deliverable_type=DeliverableType.INVESTIGATION,
        requirements=(Requirement("R1", "Observe state", "Retain evidence"),),
        change_scope=("src/app.py",),
        capabilities=("test",),
        verification_policy="tests-v1",
        budget=(("tokens", 10000), ("calls", 10), ("tool_ms", 1000), ("concurrency", 2)),
    )
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + token
        response = client.post(
            "/v1/tasks",
            json=envelope(
                {"repository_id": str(app_env.repository_id), "contract": contract.to_dict()},
                "http-task",
            ),
        )
        assert response.status_code == 202, response.text
        task_id = response.json()["data"]["task_id"]
        payload = {
            "job_type": "model.generate",
            "episode_id": task_id,
            "input": {"prompt": "private-http-prompt-canary"},
            "selected_models": [{"provider": "openai", "model": "test-model", "role": "reasoning"}],
        }
        submitted = client.post("/v1/jobs", json=envelope(payload, "http-job"))
        assert submitted.status_code == 202, submitted.text
        job = submitted.json()["data"]
        job_id = UUID(job["id"])
        replay = client.post("/v1/jobs", json=envelope(payload, "http-job"))
        assert replay.json()["data"] == job
        assert client.get("/v1/queue").json()["data"]["counts"]["queued"] == 1
        assert "private-http-prompt-canary" not in client.get("/v1/jobs").text
        start = client.get(f"/v1/jobs/{job_id}").json()["data"]["event_cursor"]
        worker = JobService(app_env.db)
        with client.websocket_connect(f"/v1/jobs/{job_id}/stream") as ws:
            ws.send_json({"type": "authenticate", "token": token, "after": start})
            assert ws.receive_json()["type"] == "ready"
            lease = worker.claim(app_env.context, "http-test-worker")
            assert lease is not None
            worker.mark_dispatched(app_env.context, lease)
            worker.progress(app_env.context, lease, "generating", 0, 1)
            emitted = worker.output(
                app_env.context,
                lease,
                "retained streamed output",
                provider="openai",
                model="test-model",
                role="reasoning",
            )
            seen = []
            for _ in range(4):
                seen.append(ws.receive_json())
            assert [item["event"]["event_type"] for item in seen] == [
                "job.running",
                "job.dispatched",
                "job.progress",
                "job.output",
            ]
            assert seen[-1]["cursor"] == emitted["sequence"]
            assert seen[-1]["event"]["data"]["text"] == "retained streamed output"
            assert "private-http-prompt-canary" not in json.dumps(seen)
            cancelled = client.post(f"/v1/jobs/{job_id}/cancel", json=envelope({}, "http-cancel"))
            assert cancelled.json()["data"]["state"] == "cancelling"
            worker.finish(app_env.context, lease, "cancelled", error_code="cancelled")
            assert ws.receive_json()["event"]["event_type"] == "job.cancelling"
            assert ws.receive_json()["event"]["event_type"] == "job.cancelled"
        with client.websocket_connect(f"/v1/jobs/{job_id}/stream") as ws:
            ws.send_json({"type": "authenticate", "token": token, "after": emitted["sequence"]})
            assert ws.receive_json()["type"] == "ready"
            assert ws.receive_json()["event"]["event_type"] == "job.cancelling"
            assert ws.receive_json()["event"]["event_type"] == "job.cancelled"
        assert client.get(f"/v1/jobs/{job_id}").json()["data"]["state"] == "cancelled"
        client.headers["Authorization"] = "Bearer " + other_token
        assert client.get(f"/v1/jobs/{job_id}").status_code == 404
        assert client.get("/v1/jobs/events").status_code == 404
