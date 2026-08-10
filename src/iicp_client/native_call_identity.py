"""Fail-closed identity validation for negotiated native lifecycle CALLs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

LIFECYCLE_PROFILE = "urn:iicp:profile:service-lifecycle:v1"


class NativeCallIdentityError(ValueError):
    """A CALL violates negotiated lifecycle identity rules."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class NativeCallIdentityRegistry:
    """Track stable task identity across attempt-scoped native CALLs."""

    def __init__(self) -> None:
        self._tasks: dict[str, str] = {}
        self._calls: set[str] = set()

    def accept(self, call: Mapping[str, Any]) -> None:
        if call.get("profile") != LIFECYCLE_PROFILE:
            return
        task_id = call.get("task_id")
        call_id = call.get("call_id")
        idempotency_key = call.get("idempotency_key")
        if not isinstance(task_id, str) or not task_id:
            raise NativeCallIdentityError("missing_task_id")
        if not isinstance(call_id, str) or not call_id:
            raise NativeCallIdentityError("missing_call_id")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise NativeCallIdentityError("missing_idempotency_key")
        if call_id in self._calls:
            raise NativeCallIdentityError("call_id_reuse")
        known_key = self._tasks.get(task_id)
        if known_key is not None and known_key != idempotency_key:
            raise NativeCallIdentityError("task_identity_conflict")
        self._tasks[task_id] = idempotency_key
        self._calls.add(call_id)
