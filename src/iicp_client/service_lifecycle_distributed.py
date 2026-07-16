"""Portable evaluator for the pre-normative distributed lifecycle profile."""

from __future__ import annotations

from typing import Any

ALLOWED_DETAIL_FIELDS = {"event_id", "progress", "reason_code", "outcome", "receipt_digest", "checkpoint_digest"}


def evaluate_distributed_lifecycle(vector: dict[str, Any]) -> str:
    kind = vector.get("kind")
    if kind == "owner_write":
        return "write_accepted" if vector.get("writer_epoch") == vector.get("current_epoch") else "stale_owner_rejected"
    if kind == "failover_submit":
        if vector.get("request_digest_matches") is not True or vector.get("idempotency_key_matches") is not True:
            return "conflict_no_new_execution"
        return (
            "existing_execution_recovered" if vector.get("execution_started") is True else "existing_record_recovered"
        )
    if kind == "append_event":
        if vector.get("event_id_seen") is True:
            return "duplicate_event_ignored"
        return (
            "event_appended"
            if vector.get("sequence") == vector.get("latest_sequence", -1) + 1
            else "sequence_gap_rejected"
        )
    if kind == "observe":
        gap = vector.get("after_sequence", -1) + 1 < vector.get("first_retained_sequence", 0)
        if gap:
            return "terminal_snapshot_with_replay_gap" if vector.get("terminal") is True else "resume_unavailable"
        return "replay_available"
    if kind == "terminal_retention":
        return (
            "unknown_task_after_expiry"
            if vector.get("age_ms", 0) > vector.get("ttl_ms", 0)
            else "terminal_snapshot_available"
        )
    if kind == "mutation_admission":
        return "mutation_allowed" if vector.get("quorum_available") is True else "temporarily_unavailable_no_write"
    if kind == "content_minimization":
        detail = vector.get("detail")
        return (
            "accepted" if isinstance(detail, dict) and set(detail) <= ALLOWED_DETAIL_FIELDS else "reject_before_write"
        )
    return "unsupported_vector"
