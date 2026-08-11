"""Transport-independent validation for negotiated native RESPONSE sequences."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class NativeResponseSequenceError(ValueError):
    """A RESPONSE frame violates the negotiated lifecycle sequence."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass
class NativeResponseSequence:
    """Validate one CALL's ordered lifecycle RESPONSE history."""

    session_id: str
    call_id: str
    task_id: str
    next_sequence: int = 0
    terminal_seen: bool = False

    def accept(self, frame: Mapping[str, Any]) -> None:
        if self.terminal_seen:
            raise NativeResponseSequenceError("response_after_terminal")
        lifecycle = frame.get("lifecycle")
        if not isinstance(lifecycle, Mapping):
            raise NativeResponseSequenceError("missing_lifecycle")
        if frame.get("session_id") != self.session_id:
            raise NativeResponseSequenceError("session_id_drift")
        if frame.get("call_id") != self.call_id:
            raise NativeResponseSequenceError("call_id_drift")
        if lifecycle.get("task_id") != self.task_id:
            raise NativeResponseSequenceError("task_id_drift")
        if lifecycle.get("sequence") != self.next_sequence:
            raise NativeResponseSequenceError("sequence_drift")
        if frame.get("is_final") != lifecycle.get("is_final"):
            raise NativeResponseSequenceError("finality_disagreement")

        status = frame.get("status")
        event = lifecycle.get("event")
        if not isinstance(status, str):
            raise NativeResponseSequenceError("status_event_disagreement")
        expected_events: set[str] | None = {
            "partial": {"partial"},
            "success": {"completed"},
            "error": {"failed", "cancelled"},
            "timeout": {"timed_out", "expired"},
        }.get(status)
        if expected_events is None or event not in expected_events:
            raise NativeResponseSequenceError("status_event_disagreement")
        is_final = frame.get("is_final")
        if (status == "partial" and is_final is not False) or (
            status != "partial" and is_final is not True
        ):
            raise NativeResponseSequenceError("terminal_flag_mismatch")

        self.next_sequence += 1
        self.terminal_seen = bool(is_final)

    def finish(self) -> None:
        if not self.terminal_seen:
            raise NativeResponseSequenceError("missing_terminal_response")
