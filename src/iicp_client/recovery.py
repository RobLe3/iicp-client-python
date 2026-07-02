"""Deterministic provider-node recovery helpers.

These helpers are deliberately small and side-effect-light so Rust, Python and
TypeScript clients classify the same failure modes: local process health,
public-route availability, directory presence, backend drain, and supervised
restart eligibility.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
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


@dataclass(frozen=True)
class RegistryRouteStatus:
    presence: DirectoryPresence
    route_needs_promotion: bool = False


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


def route_needs_promotion_from_registry_json(data: dict) -> bool:
    """Return True for direct IPv6 routes that are only self-attested."""
    node = data.get("node") if isinstance(data.get("node"), dict) else data
    summary = node.get("status_summary") if isinstance(node.get("status_summary"), dict) else {}

    if summary.get("state") == "direct_unverified":
        return True

    route_evidence = node.get("route_evidence") or summary.get("evidence_source")
    routing_hint = node.get("routing_hint") or summary.get("routing_hint")
    browser_usable = node.get("browser_usable")
    if browser_usable is None:
        browser_usable = summary.get("browser_usable")

    return (
        routing_hint == "http_ipv6"
        and route_evidence != "directory_observed"
        and browser_usable is not True
    )


async def registry_route_status(
    http: httpx.AsyncClient,
    directory_url: str,
    node_id: str,
    *,
    timeout: float = 5.0,
) -> RegistryRouteStatus:
    """Probe registry presence plus whether the advertised route needs promotion."""
    url = f"{directory_url.rstrip('/')}/v1/registry/nodes/{node_registry_prefix(node_id)}"
    try:
        resp = await http.get(url, timeout=timeout)
    except Exception:  # noqa: BLE001 — diagnostic probe, not a serving-path failure
        return RegistryRouteStatus(DirectoryPresence.UNKNOWN)
    if 200 <= resp.status_code < 300:
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            data = {}
        return RegistryRouteStatus(
            DirectoryPresence.PRESENT,
            route_needs_promotion_from_registry_json(data) if isinstance(data, dict) else False,
        )
    if resp.status_code == 404:
        return RegistryRouteStatus(DirectoryPresence.ABSENT)
    return RegistryRouteStatus(DirectoryPresence.UNKNOWN)
