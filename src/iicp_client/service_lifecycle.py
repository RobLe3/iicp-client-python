"""Opt-in draft asynchronous task lifecycle adapter (#668).

The store is transport-neutral. ``build_lifecycle_router`` exposes the draft
HTTP mapping only when an operator explicitly mounts it; importing the normal
SDK does not require FastAPI and does not change CALL/RESPONSE behavior.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

PROFILE = "urn:iicp:profile:service-lifecycle:v1"
TERMINAL_STATES = frozenset({"rejected", "completed", "failed", "cancelled", "expired"})
TRANSITIONS = {
    "submitted": {"accepted", "rejected", "expired"},
    "accepted": {"queued", "running", "completed", "cancelled", "failed", "expired"},
    "queued": {"running", "waiting", "cancelled", "failed", "expired"},
    "running": {"waiting", "streaming", "completed", "cancelled", "failed", "expired"},
    "waiting": {"queued", "running", "cancelled", "failed", "expired"},
    "streaming": {"streaming", "waiting", "completed", "cancelled", "failed", "expired"},
}


class LifecycleConflict(ValueError):
    pass


class UnknownTask(KeyError):
    pass


class ResumeUnavailable(RuntimeError):
    def __init__(self, record: LifecycleRecord) -> None:
        self.record = record


@dataclass(frozen=True)
class LifecycleEvent:
    task_id: str
    sequence: int
    state: str
    is_final: bool
    observed_at: float
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class LifecycleRecord:
    task_id: str
    idempotency_key: str
    request_digest: str
    state: str
    events: list[LifecycleEvent]
    updated_at: float

    @property
    def latest_sequence(self) -> int:
        return self.events[-1].sequence


class LifecycleStore:
    """Bounded in-memory reference store for the opt-in draft profile."""

    def __init__(self, *, max_events: int = 256, terminal_status_ttl_s: float = 3600) -> None:
        self.max_events = max(2, max_events)
        self.terminal_status_ttl_s = terminal_status_ttl_s
        self._records: dict[str, LifecycleRecord] = {}
        self._lock = threading.RLock()

    def submit(self, task_id: str, idempotency_key: str, request_digest: str) -> tuple[LifecycleRecord, bool]:
        with self._lock:
            existing = self._records.get(task_id)
            if existing:
                if (existing.idempotency_key, existing.request_digest) != (idempotency_key, request_digest):
                    raise LifecycleConflict("task or idempotency identifier reused for different content")
                return existing, False
            if any(record.idempotency_key == idempotency_key for record in self._records.values()):
                raise LifecycleConflict("idempotency identifier reused with a different task identifier")
            now = time.time()
            event = LifecycleEvent(task_id, 0, "accepted", False, now)
            record = LifecycleRecord(task_id, idempotency_key, request_digest, "accepted", [event], now)
            self._records[task_id] = record
            return record, True

    def status(self, task_id: str) -> LifecycleRecord:
        with self._lock:
            record = self._records.get(task_id)
            if record is None or self._expired(record):
                self._records.pop(task_id, None)
                raise UnknownTask(task_id)
            return record

    def transition(self, task_id: str, state: str, detail: dict[str, Any] | None = None) -> LifecycleEvent:
        with self._lock:
            record = self.status(task_id)
            state = "expired" if state == "timed_out" else state
            if state not in TRANSITIONS.get(record.state, set()):
                raise LifecycleConflict(f"illegal transition {record.state} -> {state}")
            now = time.time()
            event = LifecycleEvent(
                task_id, record.latest_sequence + 1, state, state in TERMINAL_STATES, now, detail or {}
            )
            record.events.append(event)
            record.events[:] = record.events[-self.max_events :]
            record.state = state
            record.updated_at = now
            return event

    def cancel(self, task_id: str) -> LifecycleRecord:
        with self._lock:
            record = self.status(task_id)
            if record.state not in TERMINAL_STATES:
                self.transition(task_id, "cancelled", {"outcome": "cancelled"})
            return record

    def events_after(self, task_id: str, after_sequence: int) -> list[LifecycleEvent]:
        with self._lock:
            record = self.status(task_id)
            first = record.events[0].sequence
            if after_sequence + 1 < first:
                raise ResumeUnavailable(record)
            return [event for event in record.events if event.sequence > after_sequence]

    def _expired(self, record: LifecycleRecord) -> bool:
        return record.state in TERMINAL_STATES and time.time() - record.updated_at > self.terminal_status_ttl_s


def record_payload(record: LifecycleRecord) -> dict[str, Any]:
    return {
        "profile": PROFILE,
        "task_id": record.task_id,
        "state": record.state,
        "latest_sequence": record.latest_sequence,
        "is_final": record.state in TERMINAL_STATES,
    }


def build_lifecycle_router(store: LifecycleStore, *, bearer_token: str):
    """Build the explicitly mounted FastAPI router for the draft HTTP binding."""
    from fastapi import APIRouter, Header, HTTPException
    from fastapi.responses import JSONResponse, StreamingResponse

    router = APIRouter()

    def authorize(authorization: str | None) -> None:
        if authorization != f"Bearer {bearer_token}":
            raise HTTPException(401, "lifecycle authorization required")

    @router.post("/v1/tasks")
    def submit(body: dict[str, Any], authorization: str | None = Header(default=None)):
        authorize(authorization)
        try:
            record, created = store.submit(body["task_id"], body["idempotency_key"], body["request_digest"])
        except (KeyError, LifecycleConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        return JSONResponse(record_payload(record), status_code=202 if created else 200)

    @router.get("/v1/tasks/{task_id}")
    def status(task_id: str, authorization: str | None = Header(default=None)):
        authorize(authorization)
        try:
            return record_payload(store.status(task_id))
        except UnknownTask as exc:
            raise HTTPException(404, "unknown_task") from exc

    @router.get("/v1/tasks/{task_id}/events")
    def observe(task_id: str, after_sequence: int = -1, authorization: str | None = Header(default=None)):
        authorize(authorization)
        try:
            events = store.events_after(task_id, after_sequence)
        except ResumeUnavailable as exc:
            raise HTTPException(409, {"code": "resume_unavailable", **record_payload(exc.record)}) from exc
        except UnknownTask as exc:
            raise HTTPException(404, "unknown_task") from exc
        lines = (json.dumps(asdict(event), separators=(",", ":")) + "\n" for event in events)
        return StreamingResponse(lines, media_type="application/x-ndjson")

    @router.post("/v1/tasks/{task_id}/cancel")
    def cancel(task_id: str, authorization: str | None = Header(default=None)):
        authorize(authorization)
        try:
            return record_payload(store.cancel(task_id))
        except UnknownTask as exc:
            raise HTTPException(404, "unknown_task") from exc

    return router
