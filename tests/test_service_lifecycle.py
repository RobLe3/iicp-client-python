from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from iicp_client.service_lifecycle import (
    LifecycleConflict,
    LifecycleStore,
    ResumeUnavailable,
    build_lifecycle_router,
)


def test_lifecycle_fixture_transitions_and_alias() -> None:
    fixture = json.loads((Path(__file__).parents[1] / "parity" / "service-lifecycle-v1.json").read_text())
    for vector in fixture["vectors"]:
        if vector["kind"] not in {"valid", "alias"}:
            continue
        store = LifecycleStore()
        store.submit(vector["id"], vector["id"], "sha256:test")
        for state, _, _ in vector["events"][1:]:
            store.transition(vector["id"], state)
        expected = "expired" if vector["kind"] == "alias" else vector["events"][-1][0]
        assert store.status(vector["id"]).state == expected


def test_opt_in_http_adapter_resume_idempotency_cancel_and_authorization() -> None:
    store = LifecycleStore(max_events=8)
    app = FastAPI()
    app.include_router(build_lifecycle_router(store, bearer_token="test-token"))
    client = TestClient(app)
    auth = {"Authorization": "Bearer test-token"}
    body = {"task_id": "task-1", "idempotency_key": "key-1", "request_digest": "sha256:one"}

    assert client.post("/v1/tasks", json=body).status_code == 401
    assert client.post("/v1/tasks", json=body, headers=auth).status_code == 202
    assert client.post("/v1/tasks", json=body, headers=auth).status_code == 200
    reused_key = {**body, "task_id": "task-2"}
    assert client.post("/v1/tasks", json=reused_key, headers=auth).status_code == 409
    with pytest.raises(LifecycleConflict):
        store.submit("task-1", "key-1", "sha256:different")

    store.transition("task-1", "running")
    store.transition("task-1", "streaming", {"progress": {"completed_units": 1, "total_units": 2}})
    first = client.get("/v1/tasks/task-1/events?after_sequence=0", headers=auth)
    assert [json.loads(line)["sequence"] for line in first.text.splitlines()] == [1, 2]

    # A delivery disconnect does not alter execution; resume starts strictly
    # after the last observed sequence and cannot duplicate earlier events.
    store.transition("task-1", "completed", {"result_ref": "opaque:test"})
    resumed = client.get("/v1/tasks/task-1/events?after_sequence=2", headers=auth)
    assert [json.loads(line)["state"] for line in resumed.text.splitlines()] == ["completed"]
    assert client.post("/v1/tasks/task-1/cancel", headers=auth).json()["state"] == "completed"


def test_replay_window_reports_resume_unavailable_without_reexecution() -> None:
    store = LifecycleStore(max_events=2)
    store.submit("task-window", "key-window", "sha256:window")
    store.transition("task-window", "running")
    store.transition("task-window", "streaming")
    store.transition("task-window", "completed")
    with pytest.raises(ResumeUnavailable):
        store.events_after("task-window", 0)
