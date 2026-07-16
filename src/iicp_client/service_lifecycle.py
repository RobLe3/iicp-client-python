"""Opt-in draft asynchronous task lifecycle adapter (#668).

The store is transport-neutral. ``build_lifecycle_router`` exposes the draft
HTTP mapping only when an operator explicitly mounts it; importing the normal
SDK does not require FastAPI and does not change CALL/RESPONSE behavior.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

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


class LifecycleStorageError(RuntimeError):
    pass


class ObserverLagged(RuntimeError):
    def __init__(self, earliest_available: int, latest_sequence: int) -> None:
        self.earliest_available = earliest_available
        self.latest_sequence = latest_sequence


@dataclass(frozen=True)
class BackendCancellationEvidence:
    """Content-free evidence about what a cancellation actually achieved."""

    task_id: str
    outcome: str
    cleanup_complete: bool = False


class BackendCancellationRegistry:
    """Opt-in bridge from lifecycle cancellation to active backend handles."""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[[], bool | None]] = {}
        self._signalled: set[str] = set()
        self._evidence: dict[str, BackendCancellationEvidence] = {}
        self._evidence_order: deque[str] = deque(maxlen=256)
        self._lock = threading.RLock()

    def register(self, task_id: str, handler: Callable[[], bool | None]) -> None:
        with self._lock:
            self._handlers[task_id] = handler
            self._signalled.discard(task_id)

    def report(self, task_id: str, outcome: str) -> BackendCancellationEvidence:
        if outcome not in {
            "cancel_requested",
            "transport_aborted",
            "backend_acknowledged",
            "execution_stopped",
            "cancel_unsupported",
            "already_terminal",
        }:
            raise ValueError(f"unsupported cancellation evidence: {outcome}")
        with self._lock:
            current = self._evidence.get(task_id)
            if current is None and outcome is None:
                return BackendCancellationEvidence(task_id, "cancel_unsupported", True)
            evidence = BackendCancellationEvidence(
                task_id,
                outcome,
                current.cleanup_complete if current else False,
            )
            self._remember(evidence)
            return evidence

    def complete(self, task_id: str, outcome: str | None = None) -> BackendCancellationEvidence:
        with self._lock:
            self._handlers.pop(task_id, None)
            self._signalled.discard(task_id)
            current = self._evidence.get(task_id)
            evidence = BackendCancellationEvidence(
                task_id,
                outcome or (current.outcome if current else "cancel_unsupported"),
                True,
            )
            self._remember(evidence)
            return evidence

    def evidence(self, task_id: str) -> BackendCancellationEvidence | None:
        with self._lock:
            return self._evidence.get(task_id)

    def _remember(self, evidence: BackendCancellationEvidence) -> None:
        if evidence.task_id not in self._evidence:
            if len(self._evidence_order) == self._evidence_order.maxlen:
                oldest = self._evidence_order.popleft()
                self._evidence.pop(oldest, None)
            self._evidence_order.append(evidence.task_id)
        self._evidence[evidence.task_id] = evidence

    def request(self, task_id: str, state: str) -> str:
        with self._lock:
            if state in TERMINAL_STATES:
                self.complete(task_id, "already_terminal")
                return "already_terminal"
            handler = self._handlers.get(task_id)
            if handler is None:
                self.complete(task_id, "cancel_unsupported")
                return "cancel_unsupported"
            if task_id in self._signalled:
                return "cancel_signalled"
            if handler() is False:
                self.complete(task_id, "cancel_unsupported")
                return "cancel_unsupported"
            self._signalled.add(task_id)
            self.report(task_id, "cancel_requested")
            return "cancel_signalled"


class BoundedObserverBuffer:
    """Content-free ordered event buffer with explicit slow-consumer failure."""

    def __init__(self, capacity: int, *, max_observers: int = 32) -> None:
        self.capacity = max(1, capacity)
        self.max_observers = max(1, max_observers)
        self._events: deque[LifecycleEvent] = deque(maxlen=self.capacity)
        self._observers: set[str] = set()
        self._closed = False
        self._lock = threading.RLock()

    def subscribe(self, observer_id: str) -> None:
        with self._lock:
            if observer_id not in self._observers and len(self._observers) >= self.max_observers:
                raise LifecycleConflict("observer capacity exhausted")
            self._observers.add(observer_id)

    def disconnect(self, observer_id: str) -> None:
        with self._lock:
            self._observers.discard(observer_id)

    def publish(self, event: LifecycleEvent) -> None:
        with self._lock:
            if self._events and event.sequence <= self._events[-1].sequence:
                raise LifecycleConflict("observer sequence must increase")
            self._events.append(event)
            self._closed = event.is_final

    def poll(self, after_sequence: int) -> list[LifecycleEvent]:
        with self._lock:
            if self._events and after_sequence + 1 < self._events[0].sequence:
                raise ObserverLagged(self._events[0].sequence, self._events[-1].sequence)
            return [event for event in self._events if event.sequence > after_sequence]

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def observer_count(self) -> int:
        return len(self._observers)


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


@runtime_checkable
class LifecyclePersistence(Protocol):
    """Storage port for the opt-in lifecycle profile.

    Implementations preserve task/idempotency binding, ordered events, bounded
    replay and terminal TTL. Persistence formats are deliberately not part of
    the IICP profile.
    """

    def submit(self, task_id: str, idempotency_key: str, request_digest: str) -> tuple[LifecycleRecord, bool]: ...
    def status(self, task_id: str) -> LifecycleRecord: ...
    def transition(self, task_id: str, state: str, detail: dict[str, Any] | None = None) -> LifecycleEvent: ...
    def cancel(self, task_id: str) -> LifecycleRecord: ...
    def events_after(self, task_id: str, after_sequence: int, *, limit: int | None = None) -> list[LifecycleEvent]: ...


class LifecycleStore:
    """Bounded in-memory reference store for the opt-in draft profile."""

    def __init__(
        self,
        *,
        max_events: int = 256,
        terminal_status_ttl_s: float = 3600,
        clock: Callable[[], float] = time.time,
        cancel_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.max_events = max(2, max_events)
        self.terminal_status_ttl_s = terminal_status_ttl_s
        self._clock = clock
        self._cancel_hook = cancel_hook
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
            now = self._clock()
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
            now = self._clock()
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
                if self._cancel_hook is not None:
                    self._cancel_hook(task_id)
                self.transition(task_id, "cancelled", {"outcome": "cancelled"})
            return record

    def events_after(self, task_id: str, after_sequence: int, *, limit: int | None = None) -> list[LifecycleEvent]:
        with self._lock:
            record = self.status(task_id)
            first = record.events[0].sequence
            if after_sequence + 1 < first:
                raise ResumeUnavailable(record)
            events = [event for event in record.events if event.sequence > after_sequence]
            return events[: max(1, limit)] if limit is not None else events

    def snapshot(self) -> dict[str, Any]:
        """Return a content-free persistence snapshot for operator storage."""
        with self._lock:
            return {"profile": PROFILE, "records": [asdict(record) for record in self._records.values()]}

    def restore(self, snapshot: dict[str, Any]) -> None:
        """Restore records after a process restart; reject malformed sequences."""
        if snapshot.get("profile") != PROFILE:
            raise LifecycleConflict("unsupported lifecycle snapshot profile")
        restored: dict[str, LifecycleRecord] = {}
        for raw in snapshot.get("records", []):
            events = [LifecycleEvent(**event) for event in raw["events"]]
            if not events or any(event.sequence != events[0].sequence + index for index, event in enumerate(events)):
                raise LifecycleConflict("invalid lifecycle snapshot sequence")
            record = LifecycleRecord(
                raw["task_id"], raw["idempotency_key"], raw["request_digest"], raw["state"], events, raw["updated_at"]
            )
            restored[record.task_id] = record
        with self._lock:
            self._records = restored

    def _expired(self, record: LifecycleRecord) -> bool:
        return record.state in TERMINAL_STATES and self._clock() - record.updated_at > self.terminal_status_ttl_s


_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_DIGEST = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{64}$")
_ALLOWED_DETAIL_FIELDS = frozenset(
    {"event_id", "progress", "reason_code", "outcome", "receipt_digest", "checkpoint_digest"}
)


def _content_free_detail(detail: dict[str, Any] | None) -> dict[str, Any]:
    """Validate the deliberately narrow durable-event metadata subset."""
    if not detail:
        return {}
    unknown = set(detail) - _ALLOWED_DETAIL_FIELDS
    if unknown:
        raise LifecycleConflict(f"durable lifecycle detail contains unsupported fields: {', '.join(sorted(unknown))}")
    result: dict[str, Any] = {}
    for key, value in detail.items():
        if key == "progress":
            if not isinstance(value, dict) or set(value) - {"completed_units", "total_units", "unit"}:
                raise LifecycleConflict("invalid durable lifecycle progress")
            completed = value.get("completed_units")
            total = value.get("total_units")
            unit = value.get("unit")
            if not isinstance(completed, int) or completed < 0 or not isinstance(total, int) or total < completed:
                raise LifecycleConflict("invalid durable lifecycle progress counts")
            if unit is not None and (not isinstance(unit, str) or not _SAFE_TOKEN.fullmatch(unit)):
                raise LifecycleConflict("invalid durable lifecycle progress unit")
            result[key] = {"completed_units": completed, "total_units": total, **({"unit": unit} if unit else {})}
        elif key.endswith("_digest"):
            if not isinstance(value, str) or not _DIGEST.fullmatch(value):
                raise LifecycleConflict(f"invalid {key}")
            result[key] = value.lower()
        else:
            if not isinstance(value, str) or not _SAFE_TOKEN.fullmatch(value):
                raise LifecycleConflict(f"invalid durable lifecycle {key}")
            result[key] = value
    return result


class SqliteLifecyclePersistence:
    """Single-host transactional lifecycle store.

    This adapter coordinates multiple local processes. It is not a distributed
    consensus store and does not defend against restoration of the whole DB.
    """

    SCHEMA_VERSION = 1

    def __init__(
        self,
        path: str | Path,
        *,
        max_events: int = 256,
        terminal_status_ttl_s: float = 3600,
        clock: Callable[[], float] = time.time,
        cancel_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self.max_events = max(2, max_events)
        self.terminal_status_ttl_s = terminal_status_ttl_s
        self._clock = clock
        self._cancel_hook = cancel_hook
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LifecycleStorageError(str(exc)) from exc
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        try:
            db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=5000")
            return db
        except sqlite3.Error as exc:
            raise LifecycleStorageError(str(exc)) from exc

    def _initialize(self) -> None:
        try:
            with self._connect() as db:
                version = int(db.execute("PRAGMA user_version").fetchone()[0])
                if version not in {0, self.SCHEMA_VERSION}:
                    raise LifecycleStorageError(f"unsupported lifecycle database version {version}")
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS lifecycle_tasks (
                        task_id TEXT PRIMARY KEY,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        request_digest TEXT NOT NULL,
                        state TEXT NOT NULL,
                        latest_sequence INTEGER NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS lifecycle_events (
                        task_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        state TEXT NOT NULL,
                        is_final INTEGER NOT NULL,
                        observed_at REAL NOT NULL,
                        detail_json BLOB NOT NULL,
                        PRIMARY KEY(task_id, sequence),
                        FOREIGN KEY(task_id) REFERENCES lifecycle_tasks(task_id) ON DELETE CASCADE
                    );
                    PRAGMA user_version=1;
                    """
                )
            if os.name == "posix":
                os.chmod(self.path, 0o600)
        except (OSError, sqlite3.Error) as exc:
            raise LifecycleStorageError(str(exc)) from exc

    @staticmethod
    def _record(db: sqlite3.Connection, row: sqlite3.Row) -> LifecycleRecord:
        events = [
            LifecycleEvent(
                event["task_id"],
                int(event["sequence"]),
                event["state"],
                bool(event["is_final"]),
                float(event["observed_at"]),
                json.loads(bytes(event["detail_json"])),
            )
            for event in db.execute(
                "SELECT * FROM lifecycle_events WHERE task_id=? ORDER BY sequence", (row["task_id"],)
            )
        ]
        return LifecycleRecord(
            row["task_id"],
            row["idempotency_key"],
            row["request_digest"],
            row["state"],
            events,
            float(row["updated_at"]),
        )

    def submit(self, task_id: str, idempotency_key: str, request_digest: str) -> tuple[LifecycleRecord, bool]:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM lifecycle_tasks WHERE task_id=? OR idempotency_key=?", (task_id, idempotency_key)
            ).fetchone()
            if existing is not None:
                if (existing["task_id"], existing["idempotency_key"], existing["request_digest"]) != (
                    task_id,
                    idempotency_key,
                    request_digest,
                ):
                    raise LifecycleConflict("task or idempotency identifier reused for different content")
                record = self._record(db, existing)
                db.commit()
                return record, False
            now = self._clock()
            db.execute(
                "INSERT INTO lifecycle_tasks VALUES(?,?,?,?,?,?)",
                (
                    task_id,
                    idempotency_key,
                    request_digest,
                    "accepted",
                    0,
                    now,
                ),
            )
            db.execute(
                "INSERT INTO lifecycle_events VALUES(?,?,?,?,?,?)",
                (
                    task_id,
                    0,
                    "accepted",
                    0,
                    now,
                    b"{}",
                ),
            )
            row = db.execute("SELECT * FROM lifecycle_tasks WHERE task_id=?", (task_id,)).fetchone()
            record = self._record(db, row)
            db.commit()
            return record, True
        except (LifecycleConflict, sqlite3.Error) as exc:
            db.rollback()
            if isinstance(exc, LifecycleConflict):
                raise
            raise LifecycleStorageError(str(exc)) from exc
        finally:
            db.close()

    def status(self, task_id: str) -> LifecycleRecord:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM lifecycle_tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise UnknownTask(task_id)
            if (
                row["state"] in TERMINAL_STATES
                and self._clock() - float(row["updated_at"]) > self.terminal_status_ttl_s
            ):
                db.execute("DELETE FROM lifecycle_tasks WHERE task_id=?", (task_id,))
                db.commit()
                raise UnknownTask(task_id)
            record = self._record(db, row)
            db.commit()
            return record
        except UnknownTask:
            db.rollback()
            raise
        except sqlite3.Error as exc:
            db.rollback()
            raise LifecycleStorageError(str(exc)) from exc
        finally:
            db.close()

    def transition(self, task_id: str, state: str, detail: dict[str, Any] | None = None) -> LifecycleEvent:
        state = "expired" if state == "timed_out" else state
        safe_detail = _content_free_detail(detail)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,latest_sequence FROM lifecycle_tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise UnknownTask(task_id)
            if state not in TRANSITIONS.get(row["state"], set()):
                raise LifecycleConflict(f"illegal transition {row['state']} -> {state}")
            now = self._clock()
            sequence = int(row["latest_sequence"]) + 1
            db.execute(
                "UPDATE lifecycle_tasks SET state=?,latest_sequence=?,updated_at=? WHERE task_id=?",
                (state, sequence, now, task_id),
            )
            event = LifecycleEvent(task_id, sequence, state, state in TERMINAL_STATES, now, safe_detail)
            db.execute(
                "INSERT INTO lifecycle_events VALUES(?,?,?,?,?,?)",
                (
                    task_id,
                    sequence,
                    state,
                    int(event.is_final),
                    now,
                    json.dumps(safe_detail, sort_keys=True, separators=(",", ":")).encode(),
                ),
            )
            cutoff = sequence - self.max_events + 1
            db.execute("DELETE FROM lifecycle_events WHERE task_id=? AND sequence<?", (task_id, cutoff))
            db.commit()
            return event
        except (UnknownTask, LifecycleConflict):
            db.rollback()
            raise
        except sqlite3.Error as exc:
            db.rollback()
            raise LifecycleStorageError(str(exc)) from exc
        finally:
            db.close()

    def cancel(self, task_id: str) -> LifecycleRecord:
        record = self.status(task_id)
        if record.state not in TERMINAL_STATES:
            if self._cancel_hook is not None:
                self._cancel_hook(task_id)
            self.transition(task_id, "cancelled", {"outcome": "cancelled"})
        return self.status(task_id)

    def events_after(self, task_id: str, after_sequence: int, *, limit: int | None = None) -> list[LifecycleEvent]:
        record = self.status(task_id)
        first = record.events[0].sequence
        if after_sequence + 1 < first:
            raise ResumeUnavailable(record)
        events = [event for event in record.events if event.sequence > after_sequence]
        return events[: max(1, limit)] if limit is not None else events


def record_payload(record: LifecycleRecord) -> dict[str, Any]:
    return {
        "profile": PROFILE,
        "task_id": record.task_id,
        "state": record.state,
        "latest_sequence": record.latest_sequence,
        "is_final": record.state in TERMINAL_STATES,
    }


@dataclass(frozen=True)
class LifecycleAuthorizationRequest:
    credential: str | None
    operation: str
    task_id: str


@dataclass(frozen=True)
class LifecycleAuthorizationDecision:
    authenticated: bool
    allowed: bool
    conceal_task: bool = False


LifecycleAuthorizer = Callable[[LifecycleAuthorizationRequest], LifecycleAuthorizationDecision]


def build_lifecycle_router(store: LifecyclePersistence, *, bearer_token: str):
    """Compatibility/test router using one shared bearer token.

    Production integrations should use ``build_lifecycle_router_with_authorizer``
    and bind verified principals to task identifiers outside the lifecycle store.
    """

    def shared_token_authorizer(request: LifecycleAuthorizationRequest) -> LifecycleAuthorizationDecision:
        valid = request.credential == f"Bearer {bearer_token}"
        return LifecycleAuthorizationDecision(authenticated=valid, allowed=valid)

    return build_lifecycle_router_with_authorizer(store, authorizer=shared_token_authorizer)


def build_lifecycle_router_with_authorizer(store: LifecyclePersistence, *, authorizer: LifecycleAuthorizer):
    """Build the explicitly mounted draft router with operation-level authorization."""
    from fastapi import APIRouter, Header, HTTPException
    from fastapi.responses import JSONResponse, StreamingResponse

    router = APIRouter()

    def authorize(authorization: str | None, operation: str, task_id: str) -> None:
        decision = authorizer(LifecycleAuthorizationRequest(authorization, operation, task_id))
        if decision.allowed and decision.authenticated:
            return
        if not decision.authenticated:
            raise HTTPException(401, "lifecycle authorization required")
        if decision.conceal_task:
            raise HTTPException(404, "unknown_task")
        raise HTTPException(403, "lifecycle operation forbidden")

    @router.post("/v1/tasks")
    def submit(body: dict[str, Any], authorization: str | None = Header(default=None)):
        authorize(authorization, "submit", str(body.get("task_id", "")))
        try:
            record, created = store.submit(body["task_id"], body["idempotency_key"], body["request_digest"])
        except (KeyError, LifecycleConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        return JSONResponse(record_payload(record), status_code=202 if created else 200)

    @router.get("/v1/tasks/{task_id}")
    def status(task_id: str, authorization: str | None = Header(default=None)):
        authorize(authorization, "status", task_id)
        try:
            return record_payload(store.status(task_id))
        except UnknownTask as exc:
            raise HTTPException(404, "unknown_task") from exc

    @router.get("/v1/tasks/{task_id}/events")
    def observe(task_id: str, after_sequence: int = -1, authorization: str | None = Header(default=None)):
        authorize(authorization, "observe", task_id)
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
        authorize(authorization, "cancel", task_id)
        try:
            return record_payload(store.cancel(task_id))
        except UnknownTask as exc:
            raise HTTPException(404, "unknown_task") from exc

    return router
