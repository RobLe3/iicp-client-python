"""Local runtime health semantics; not an IICP wire profile."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

State = Literal["healthy", "degraded", "recovering", "unavailable", "not_applicable", "unknown"]
RUNTIME_STALE_AFTER_MS = 30_000
SUPERVISOR_STALE_AFTER_MS = 120_000


@dataclass
class ClassificationInput:
    lifecycle: str
    runtime_age_ms: int
    runtime_stale_after_ms: int
    supervisor_required: bool
    supervisor_age_ms: int
    supervisor_stale_after_ms: int
    provider: State
    capacity_available: bool
    routing: State
    directory: State
    dns: State
    internet: State
    tunnel: State


def classify(i: ClassificationInput) -> dict:
    if i.lifecycle == "starting":
        return {"liveness": "starting", "readiness": "not_ready", "reason_codes": ["STARTING"]}
    if i.runtime_age_ms > i.runtime_stale_after_ms:
        return {"liveness": "not_live", "readiness": "not_ready", "reason_codes": ["RUNTIME_PROGRESS_STALE"]}
    if i.supervisor_required and i.supervisor_age_ms > i.supervisor_stale_after_ms:
        return {"liveness": "not_live", "readiness": "not_ready", "reason_codes": ["SUPERVISOR_PROGRESS_STALE"]}
    if i.lifecycle == "stopping":
        return {"liveness": "live", "readiness": "not_ready", "reason_codes": ["STOPPING"]}
    reasons = []
    if i.provider == "unavailable":
        reasons.append("PROVIDER_UNAVAILABLE")
    if not i.capacity_available:
        reasons.append("NO_CAPACITY")
    if i.routing == "unavailable":
        reasons.append("ROUTING_UNAVAILABLE")
    if i.tunnel == "recovering":
        reasons.append("TUNNEL_RECOVERING")
    if i.directory == "unavailable":
        reasons.append("DIRECTORY_UNAVAILABLE")
    if i.dns == "unavailable":
        reasons.append("DNS_UNAVAILABLE")
    if i.internet == "unavailable":
        reasons.append("INTERNET_UNAVAILABLE")
    not_ready = any(x in reasons for x in ("PROVIDER_UNAVAILABLE", "NO_CAPACITY", "ROUTING_UNAVAILABLE"))
    return {
        "liveness": "live",
        "readiness": "not_ready" if not_ready else "degraded" if reasons else "ready",
        "reason_codes": reasons,
    }


@dataclass
class RuntimeHealth:
    supervisor_required: bool = False
    process_epoch: str = field(default_factory=lambda: str(uuid.uuid4()))
    lifecycle: str = "starting"
    runtime_sequence: int = 0
    supervisor_sequence: int = 0
    snapshot_sequence: int = 0
    _runtime_at: float = field(default_factory=time.monotonic)
    _supervisor_at: float = field(default_factory=time.monotonic)
    capacity_available: bool = False
    subsystems: dict[str, State] = field(
        default_factory=lambda: {"provider": "unknown", "routing": "unknown", "tunnel": "unknown"}
    )
    external: dict[str, State] = field(
        default_factory=lambda: {"directory": "unknown", "dns": "unknown", "internet": "unknown"}
    )
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def mark_running(self) -> None:
        with self._lock:
            self.lifecycle = "running"
            self.capacity_available = True
            self.subsystems.update(provider="healthy", routing="healthy")

    def mark_stopping(self) -> None:
        with self._lock:
            self.lifecycle = "stopping"

    def advance_runtime(self) -> None:
        with self._lock:
            self.runtime_sequence += 1
            self._runtime_at = time.monotonic()

    def advance_supervisor(self) -> None:
        with self._lock:
            self.supervisor_sequence += 1
            self._supervisor_at = time.monotonic()

    def set_supervisor_required(self, required: bool) -> None:
        with self._lock:
            self.supervisor_required = required

    def set_external(self, name: str, state: State) -> None:
        with self._lock:
            self.external[name] = state

    def snapshot(self) -> dict:
        with self._lock:
            now = time.monotonic()
            ra = int((now - self._runtime_at) * 1000)
            sa = int((now - self._supervisor_at) * 1000)
            self.snapshot_sequence += 1
            result = classify(
                ClassificationInput(
                    self.lifecycle,
                    ra,
                    RUNTIME_STALE_AFTER_MS,
                    self.supervisor_required,
                    sa,
                    SUPERVISOR_STALE_AFTER_MS,
                    self.subsystems.get("provider", "unknown"),
                    self.capacity_available,
                    self.subsystems.get("routing", "unknown"),
                    self.external.get("directory", "unknown"),
                    self.external.get("dns", "unknown"),
                    self.external.get("internet", "unknown"),
                    self.subsystems.get("tunnel", "unknown"),
                )
            )
            return {
                "health_schema_version": 1,
                "process_epoch": self.process_epoch,
                "pid": os.getpid(),
                "sequence": self.snapshot_sequence,
                "emitted_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "lifecycle": self.lifecycle,
                **result,
                "progress": {
                    "runtime": {
                        "sequence": self.runtime_sequence,
                        "age_ms": ra,
                        "stale_after_ms": RUNTIME_STALE_AFTER_MS,
                        "required": True,
                    },
                    "supervisor": {
                        "sequence": self.supervisor_sequence,
                        "age_ms": sa,
                        "stale_after_ms": SUPERVISOR_STALE_AFTER_MS,
                        "required": self.supervisor_required,
                    },
                },
                "subsystems": dict(self.subsystems),
                "external_connectivity": dict(self.external),
            }


def snapshot_path(node: str) -> Path:
    if not node or any(not (c.isalnum() or c in "-_.") for c in node):
        raise ValueError("invalid node name")
    return Path.home() / ".iicp" / "run" / node / "health-v1.json"


def write_snapshot(path: Path, snapshot: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".health-", dir=path.parent)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(snapshot, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
