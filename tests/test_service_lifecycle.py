from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from iicp_client.service_lifecycle import (
    LifecycleConflict,
    LifecyclePersistence,
    LifecycleStorageError,
    LifecycleStore,
    ResumeUnavailable,
    SqliteLifecyclePersistence,
    UnknownTask,
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


def test_restart_snapshot_backpressure_and_backend_cancel_hook() -> None:
    now = [1000.0]
    cancelled: list[str] = []
    store = LifecycleStore(max_events=3, terminal_status_ttl_s=10, clock=lambda: now[0], cancel_hook=cancelled.append)
    store.submit("restart", "idem-restart", "digest")
    store.transition("restart", "running")
    for chunk in range(1, 4):
        store.transition("restart", "streaming", {"chunk": chunk})
    restored = LifecycleStore(max_events=3, terminal_status_ttl_s=10, clock=lambda: now[0], cancel_hook=cancelled.append)
    restored.restore(store.snapshot())
    with pytest.raises(ResumeUnavailable):
        restored.events_after("restart", 0)
    assert len(restored.events_after("restart", 1, limit=1)) == 1
    assert restored.cancel("restart").state == "cancelled"
    assert restored.cancel("restart").state == "cancelled"
    assert cancelled == ["restart"]
    now[0] += 11
    with pytest.raises(UnknownTask):
        restored.status("restart")


def test_sqlite_persistence_is_opt_in_content_free_and_restart_safe(tmp_path: Path) -> None:
    fixture = json.loads((Path(__file__).parents[1] / "parity" / "service-lifecycle-persistence-v1.json").read_text())
    assert fixture["fixture_version"] == "0.1.0-draft"
    assert {vector["id"] for vector in fixture["vectors"]} == {
        f"LIFECYCLE-PERSIST-{number:02d}" for number in range(1, 11)
    }
    path = tmp_path / "lifecycle.sqlite3"
    store = SqliteLifecyclePersistence(path, max_events=3)
    assert isinstance(store, LifecyclePersistence)
    record, created = store.submit("durable", "idem-durable", "sha256:request")
    assert created and record.state == "accepted"
    assert store.submit("durable", "idem-durable", "sha256:request")[1] is False
    store.transition("durable", "running")
    store.transition("durable", "streaming", {
        "event_id": "event-2",
        "progress": {"completed_units": 1, "total_units": 2, "unit": "chunks"},
    })
    restarted = SqliteLifecyclePersistence(path, max_events=3)
    assert restarted.status("durable").state == "streaming"
    assert [event.sequence for event in restarted.events_after("durable", 0)] == [1, 2]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(LifecycleConflict):
        restarted.transition("durable", "completed", {"response": "must-not-persist"})
    database = path.read_bytes().lower()
    for forbidden in (b"prompt", b"response", b"credential", b"endpoint", b"peer_topology"):
        assert forbidden not in database


def test_sqlite_terminal_ttl_and_bounded_replay(tmp_path: Path) -> None:
    now = [100.0]
    store = SqliteLifecyclePersistence(
        tmp_path / "ttl.sqlite3", max_events=2, terminal_status_ttl_s=10, clock=lambda: now[0]
    )
    store.submit("ttl", "idem-ttl", "digest")
    store.transition("ttl", "running")
    store.transition("ttl", "streaming")
    store.transition("ttl", "completed", {"receipt_digest": "sha256:" + "a" * 64})
    with pytest.raises(ResumeUnavailable):
        store.events_after("ttl", 0)
    now[0] += 11
    with pytest.raises(UnknownTask):
        store.status("ttl")


def test_sqlite_two_process_crash_recovery_and_single_terminal_winner(tmp_path: Path) -> None:
    path = tmp_path / "shared.sqlite3"
    store = SqliteLifecyclePersistence(path, max_events=3)
    store.submit("shared-task", "shared-idem", "sha256:shared")
    store.transition("shared-task", "running")
    helper = Path(__file__).with_name("lifecycle_process_helper.py")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}

    crashed = subprocess.run(
        [sys.executable, str(helper), "crash-mid-transition", str(path)], env=env, check=False
    )
    assert crashed.returncode == 77
    recovered = SqliteLifecyclePersistence(path, max_events=3)
    assert recovered.status("shared-task").state == "running"
    assert recovered.status("shared-task").latest_sequence == 1

    first = subprocess.Popen([sys.executable, str(helper), "complete", str(path)], env=env)
    second = subprocess.Popen([sys.executable, str(helper), "fail", str(path)], env=env)
    outcomes = sorted((first.wait(), second.wait()))
    assert outcomes == [0, 2]
    terminal = SqliteLifecyclePersistence(path, max_events=3).status("shared-task")
    assert terminal.state in {"completed", "failed"}
    assert terminal.latest_sequence == 2
    assert [event.sequence for event in terminal.events] == [0, 1, 2]


def test_sqlite_rejects_corrupt_schema_and_unusable_path(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a sqlite database")
    with pytest.raises(LifecycleStorageError):
        SqliteLifecyclePersistence(corrupt)

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("blocked")
    with pytest.raises(LifecycleStorageError):
        SqliteLifecyclePersistence(blocker / "lifecycle.sqlite3")
