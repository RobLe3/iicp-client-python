"""Local startup policy for the pre-normative managed operator profile."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ManagedOperatorInput:
    mode: str
    authentication_configured: bool
    identity_storage_protected: bool
    auto_update_requested: bool
    update_authenticated: bool
    rollback_verified: bool
    upnp_requested: bool
    tunnel_requested: bool
    upnp_approved: bool
    tunnel_approved: bool


def evaluate_managed_operator(value: ManagedOperatorInput) -> tuple[bool, str]:
    if value.mode == "convenience":
        return True, "convenience_mode"
    if value.mode != "managed":
        return False, "invalid_operator_profile"
    checks = (
        (value.authentication_configured, "authentication_required"),
        (value.identity_storage_protected, "protected_identity_storage_required"),
        (not value.auto_update_requested or value.update_authenticated, "authenticated_update_required"),
        (not value.auto_update_requested or value.rollback_verified, "rollback_required"),
        (not value.upnp_requested or value.upnp_approved, "upnp_approval_required"),
        (not value.tunnel_requested or value.tunnel_approved, "tunnel_approval_required"),
    )
    for accepted, reason in checks:
        if not accepted:
            return False, reason
    return True, "managed_requirements_met"
