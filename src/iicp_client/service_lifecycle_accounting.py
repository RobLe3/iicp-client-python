"""Pure, opt-in accounting decisions for the draft service lifecycle profile.

This module controls accounting cardinality only. It does not price work,
reserve credits, settle balances, or mount lifecycle behavior by default.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LifecycleAccountingDecision:
    decision: str
    reservation_action: str
    settlement_action: str
    new_execution: bool


def _decision(
    decision: str,
    reservation_action: str = "none",
    settlement_action: str = "none",
    new_execution: bool = False,
) -> LifecycleAccountingDecision:
    return LifecycleAccountingDecision(
        decision, reservation_action, settlement_action, new_execution
    )


def decide_lifecycle_accounting(
    request: Mapping[str, Any],
) -> LifecycleAccountingDecision:
    """Return a deterministic cardinality decision for one lifecycle action."""

    operation = request.get("operation")
    binding = request.get("binding")
    reservation_exists = request.get("reservation_exists") is True
    settlement_exists = request.get("settlement_exists") is True
    accepted = request.get("accepted") is True
    delivery = request.get("delivery")

    if operation not in {"submit", "status", "observe", "resume", "cancel", "terminal"}:
        return _decision("reject_invalid_input")
    if binding not in {"same", "conflict", "fresh"}:
        return _decision("reject_invalid_input")
    if delivery not in {"none", "partial", "complete"}:
        return _decision("reject_invalid_input")

    if operation in {"status", "observe", "cancel", "terminal"} and binding != "same":
        return _decision("reject_conflict")

    if operation == "status":
        return _decision("return_status")
    if operation == "observe":
        return _decision("replay_events")
    if operation == "resume":
        if request.get("resume_available") is True:
            return _decision("replay_events")
        if request.get("explicit_new_task") is not True:
            return _decision("explicit_new_task_required")
        if (
            binding != "fresh"
            or request.get("fresh_task_id") is not True
            or request.get("fresh_idempotency_key") is not True
        ):
            return _decision("reject_identifier_reuse")
        return _decision("start_new_task", "create", new_execution=True)

    if operation == "submit":
        if binding == "conflict":
            return _decision("reject_conflict")
        if binding == "same" and reservation_exists:
            return _decision("reuse_execution", "reuse")
        if binding == "same":
            return _decision("reject_missing_reservation")
        if reservation_exists:
            return _decision("reject_conflict")
        return _decision("start_execution", "create", new_execution=True)

    if operation == "cancel":
        if settlement_exists:
            return _decision("return_existing_settlement", "reuse", "reuse")
        if not reservation_exists:
            return _decision("cancel_without_accounting")
        if not accepted:
            return _decision("cancel_before_acceptance", "release")
        reason = (
            "cancel_after_partial_delivery"
            if delivery == "partial"
            else "cancel_after_acceptance"
        )
        return _decision(reason, "reuse", "create")

    if settlement_exists:
        return _decision("return_existing_settlement", "reuse", "reuse")
    if not reservation_exists:
        return _decision("reject_missing_reservation")
    terminal_state = request.get("terminal_state")
    if terminal_state not in {"completed", "failed", "cancelled", "expired"}:
        return _decision("reject_invalid_input")
    suffix = "_partial" if delivery == "partial" else ""
    return _decision(f"settle_{terminal_state}{suffix}", "reuse", "create")
