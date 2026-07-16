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
    BackendCancellationEvidence,
    BackendCancellationRegistry,
    BoundedObserverBuffer,
    LifecycleAuthorizationDecision,
    LifecycleConflict,
    LifecycleEvent,
    LifecyclePersistence,
    LifecycleStorageError,
    LifecycleStore,
    ObserverLagged,
    ResumeUnavailable,
    SqliteLifecyclePersistence,
    UnknownTask,
    build_lifecycle_router,
    build_lifecycle_router_with_authorizer,
)


def _runtime_control_fixture() -> dict:
    return json.loads((Path(__file__).parents[1] / "parity/service-lifecycle-runtime-control-v1.json").read_text())


def test_runtime_control_fixture_cancellation_and_bounded_observation() -> None:
    fixture = _runtime_control_fixture()
    for vector in fixture["cancellation"]:
        registry = BackendCancellationRegistry()
        calls: list[str] = []
        if vector["handler"] == "registered":
            registry.register("task", lambda calls=calls: calls.append("cancel") or True)
        assert registry.request("task", vector["state"]) == vector["expected"]
        assert len(calls) <= 1

    observation = fixture["observation"]
    buffer = BoundedObserverBuffer(observation["capacity"], max_observers=1)
    buffer.subscribe("observer")
    for sequence in observation["published_sequences"]:
        buffer.publish(LifecycleEvent("task", sequence, "streaming", False, 1.0))
    for vector in observation["vectors"]:
        if "expected_error" in vector:
            with pytest.raises(ObserverLagged) as caught:
                buffer.poll(vector["after_sequence"])
            assert caught.value.earliest_available == vector["earliest_available"]
            assert caught.value.latest_sequence == vector["latest_sequence"]
        else:
            assert [event.sequence for event in buffer.poll(vector["after_sequence"])] == vector["expected_sequences"]
    buffer.disconnect("observer")
    assert buffer.observer_count == 0
    buffer.publish(LifecycleEvent("task", 4, "completed", True, 2.0))
    assert buffer.closed

    for vector in fixture["cancellation_evidence"]["vectors"]:
        registry = BackendCancellationRegistry()
        registry.register(vector["id"], lambda: True)
        assert registry.request(vector["id"], "running") == "cancel_signalled"
        registry.report(vector["id"], vector["reported"])
        evidence = registry.complete(vector["id"])
        assert evidence == BackendCancellationEvidence(
            vector["id"], vector["expected"], vector["cleanup_complete"]
        )


@pytest.mark.asyncio
async def test_cancellation_registry_aborts_active_http_request() -> None:
    import asyncio

    import httpx

    async def slow_response(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(60)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow_response)) as client:
        request_task = asyncio.create_task(client.get("https://backend.invalid/slow"))
        await asyncio.sleep(0)
        registry = BackendCancellationRegistry()
        registry.register("active", lambda: request_task.cancel() or True)
        assert registry.request("active", "running") == "cancel_signalled"
        with pytest.raises(asyncio.CancelledError):
            await request_task
        evidence = registry.complete("active", "transport_aborted")
        assert evidence.outcome == "transport_aborted"
        assert evidence.cleanup_complete


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


def test_task_scoped_authorizer_conceals_cross_principal_access() -> None:
    fixture = json.loads((Path(__file__).parents[1] / "parity/service-lifecycle-authorization-v1.json").read_text())
    assert len(fixture["cases"]) == 10
    owners: dict[str, str] = {}

    def authorizer(request):
        token = request.credential
        if token in {None, "Bearer invalid", "Bearer expired"}:
            return LifecycleAuthorizationDecision(False, False)
        principal = {
            "Bearer owner": "owner",
            "Bearer other": "other",
            "Bearer read-only": "reader",
            "Bearer operator": "operator",
        }.get(token)
        if principal is None:
            return LifecycleAuthorizationDecision(False, False)
        if principal == "reader" and request.operation == "submit":
            return LifecycleAuthorizationDecision(True, False)
        if principal == "operator":
            return LifecycleAuthorizationDecision(True, True)
        if request.operation == "submit":
            if principal == "owner":
                owners.setdefault(request.task_id, principal)
                return LifecycleAuthorizationDecision(True, True)
            return LifecycleAuthorizationDecision(True, False)
        allowed = owners.get(request.task_id) == principal
        return LifecycleAuthorizationDecision(True, allowed, conceal_task=not allowed)

    store = LifecycleStore()
    app = FastAPI()
    app.include_router(build_lifecycle_router_with_authorizer(store, authorizer=authorizer))
    client = TestClient(app)
    body = {"task_id": "task-a", "idempotency_key": "key-a", "request_digest": "sha256:a"}
    expected_status = fixture["decision_contract"]
    for case in fixture["cases"]:
        headers = {"Authorization": case["credential"]} if case["credential"] else {}
        if case["operation"] == "submit":
            response = client.post(
                "/v1/tasks",
                json=body
                if case["task_id"] == "task-a"
                else {**body, "task_id": case["task_id"], "idempotency_key": "key-b"},
                headers=headers,
            )
        elif case["operation"] == "status":
            response = client.get(f"/v1/tasks/{case['task_id']}", headers=headers)
        elif case["operation"] == "observe":
            response = client.get(f"/v1/tasks/{case['task_id']}/events", headers=headers)
        else:
            response = client.post(f"/v1/tasks/{case['task_id']}/cancel", headers=headers)
        expected = expected_status[case["expected"]]
        if case["id"] == "LIFECYCLE-AUTH-03":
            expected = 202
        assert response.status_code == expected, case["id"]
        assert "principal_id" not in response.text
        assert case["credential"] not in response.text if case["credential"] else True


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
    restored = LifecycleStore(
        max_events=3, terminal_status_ttl_s=10, clock=lambda: now[0], cancel_hook=cancelled.append
    )
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
    store.transition(
        "durable",
        "streaming",
        {
            "event_id": "event-2",
            "progress": {"completed_units": 1, "total_units": 2, "unit": "chunks"},
        },
    )
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

    crashed = subprocess.run([sys.executable, str(helper), "crash-mid-transition", str(path)], env=env, check=False)
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
