"""Deterministic provider-node recovery helpers.

These helpers are deliberately small and side-effect-light so Rust, Python and
TypeScript clients classify the same failure modes: local process health,
public-route availability, directory presence, backend drain, and supervised
restart eligibility.
"""

from __future__ import annotations

import os
from enum import StrEnum

import httpx

RECOVERY_EXIT_CODE = 76
DEFAULT_RECOVERY_GRACE_CHECKS = 3
DEFAULT_RECOVERY_CHECK_EVERY_HEARTBEATS = 2


class RecoveryState(StrEnum):
    HEALTHY = "healthy"
    LOCAL_UNHEALTHY = "local_unhealthy"
    BACKEND_ATTENTION = "backend_attention"
    ROUTE_MISMATCH = "route_mismatch"
    TUNNEL_COOLING_DOWN = "tunnel_cooling_down"
    DIRECTORY_ABSENT = "directory_absent"
    LIMITED_REACH = "limited_reach"
    RESTART_RECOMMENDED = "restart_recommended"
    UNKNOWN = "unknown"


class RecoveryAction(StrEnum):
    NONE = "none"
    REREGISTER = "reregister"
    WAIT_COOLDOWN = "wait_cooldown"
    MARK_UNAVAILABLE = "mark_unavailable"
    RESTART_SELF = "restart_self"
    OPERATOR_ENDPOINT_NEEDED = "operator_endpoint_needed"
    BACKEND_ATTENTION = "backend_attention"


class DirectoryPresence(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


def node_registry_prefix(node_id: str) -> str:
    """Return the public registry lookup prefix for a node id."""
    parts = node_id.split("-")
    is_uuid = (
        len(node_id) == 36
        and len(parts) == 5
        and [len(p) for p in parts] == [8, 4, 4, 4, 12]
        and all(all(c in "0123456789abcdefABCDEF" for c in p) for p in parts)
    )
    return node_id[:8] if is_uuid else node_id


def env_grace_checks() -> int:
    try:
        value = int(os.environ.get("IICP_RECOVERY_GRACE_CHECKS", str(DEFAULT_RECOVERY_GRACE_CHECKS)))
        return value if value > 0 else DEFAULT_RECOVERY_GRACE_CHECKS
    except ValueError:
        return DEFAULT_RECOVERY_GRACE_CHECKS


def env_check_every_heartbeats() -> int:
    try:
        value = int(
            os.environ.get("IICP_RECOVERY_CHECK_EVERY_HEARTBEATS", str(DEFAULT_RECOVERY_CHECK_EVERY_HEARTBEATS))
        )
        return value if value > 0 else DEFAULT_RECOVERY_CHECK_EVERY_HEARTBEATS
    except ValueError:
        return DEFAULT_RECOVERY_CHECK_EVERY_HEARTBEATS


def supervised_recovery_enabled() -> bool:
    supervised = os.environ.get("IICP_SUPERVISED", "").strip().lower() in {"1", "true", "yes"}
    disabled = os.environ.get("IICP_RECOVERY_SUPERVISED_EXIT", "").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }
    return supervised and not disabled


def classify(
    *,
    local_health_ok: bool,
    public_available: bool,
    directory_presence: DirectoryPresence,
    consecutive_failures: int,
    grace_checks: int,
    backend_attention: bool = False,
) -> tuple[RecoveryState, RecoveryAction]:
    """Classify provider recovery state and the next deterministic action."""
    if not local_health_ok:
        return RecoveryState.LOCAL_UNHEALTHY, RecoveryAction.RESTART_SELF
    if backend_attention:
        return RecoveryState.BACKEND_ATTENTION, RecoveryAction.BACKEND_ATTENTION
    if not public_available:
        if consecutive_failures >= grace_checks:
            return RecoveryState.RESTART_RECOMMENDED, RecoveryAction.RESTART_SELF
        return RecoveryState.LIMITED_REACH, RecoveryAction.WAIT_COOLDOWN
    if directory_presence is DirectoryPresence.PRESENT:
        return RecoveryState.HEALTHY, RecoveryAction.NONE
    if directory_presence is DirectoryPresence.ABSENT:
        if consecutive_failures >= grace_checks:
            return RecoveryState.ROUTE_MISMATCH, RecoveryAction.RESTART_SELF
        return RecoveryState.DIRECTORY_ABSENT, RecoveryAction.REREGISTER
    return RecoveryState.UNKNOWN, RecoveryAction.NONE


async def registry_node_presence(
    http: httpx.AsyncClient,
    directory_url: str,
    node_id: str,
    *,
    timeout: float = 5.0,
) -> DirectoryPresence:
    """Probe the public registry detail endpoint for this node."""
    url = f"{directory_url.rstrip('/')}/v1/registry/nodes/{node_registry_prefix(node_id)}"
    try:
        resp = await http.get(url, timeout=timeout)
    except Exception:  # noqa: BLE001 — diagnostic probe, not a serving-path failure
        return DirectoryPresence.UNKNOWN
    if 200 <= resp.status_code < 300:
        return DirectoryPresence.PRESENT
    if resp.status_code == 404:
        return DirectoryPresence.ABSENT
    return DirectoryPresence.UNKNOWN
