"""Production-identity projection policy for opt-in lifecycle operations."""

from __future__ import annotations

from typing import Any

ALLOWED_AUDIT_FIELDS = {
    "event_id",
    "task_ref",
    "principal_ref_digest",
    "credential_key_id",
    "revocation_epoch",
    "operation",
    "outcome",
    "reason_code",
    "occurred_at",
}


def evaluate_lifecycle_identity(case: dict[str, Any], audit_retention_seconds: int = 604800) -> str:
    kind = case.get("kind")
    if kind == "audit_retention":
        return (
            "audit_record_pruned" if case.get("age_seconds", 0) > audit_retention_seconds else "audit_record_retained"
        )
    if kind == "audit_redaction":
        audit = case.get("audit")
        return (
            "audit_record_allowed"
            if isinstance(audit, dict) and set(audit) <= ALLOWED_AUDIT_FIELDS
            else "reject_before_write"
        )
    if case.get("profile_requested") is not True and case.get("surface") == "ordinary_task":
        return "legacy_open_mesh_unchanged"
    if case.get("credential_status") != "valid":
        return "unauthenticated"
    if case.get("credential_revocation_epoch", 0) < case.get("minimum_revocation_epoch", 0):
        return "unauthenticated"
    operation = case.get("operation")
    scopes = set(case.get("scope") or [])
    if operation == "submit":
        return "allowed_bind_owner" if "submit" in scopes else "forbidden"
    if case.get("principal_ref_digest") != case.get("task_owner_ref_digest"):
        if case.get("operator_override") is True and f"operator:{operation}" in scopes:
            return "allowed_operator_override"
        return "concealed_task"
    return "allowed" if operation in scopes else "forbidden"
