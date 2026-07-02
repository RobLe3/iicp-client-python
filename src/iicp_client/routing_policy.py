"""Remote-routing policy gates for prompt dispatch (#585).

These checks run after directory discovery and before a prompt leaves the
client. They do not turn the directory into a content processor: the directory
still receives only the intent/constraints discovery query, never the prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from iicp_client.types import Node, RoutingPolicy

ROUTING_POLICY_REFUSAL_CODE = "IICP-POLICY-ROUTING"
_EU_REGION_PREFIXES = ("eu", "eea")


@dataclass
class RoutingPolicyDecision:
    eligible: list[Node]
    rejected_reasons: list[str]
    skipped_keyless: int = 0


def resolved_policy(policy: RoutingPolicy | None) -> RoutingPolicy:
    """Return a copy with profile defaults filled in."""

    src = policy or RoutingPolicy()
    profile = (src.profile or "standard").replace("-", "_").lower()

    defaults = {
        "standard": {
            "require_encryption": True,
            "allow_remote_executor": True,
            "require_policy_manifest": False,
            "require_no_payload_retention": False,
        },
        "sensitive": {
            "require_encryption": True,
            "allow_remote_executor": False,
            "require_policy_manifest": False,
            "require_no_payload_retention": False,
        },
        "eu_restricted": {
            "require_encryption": True,
            "allow_remote_executor": True,
            "require_policy_manifest": False,
            "require_no_payload_retention": False,
            "allowed_regions": list(_EU_REGION_PREFIXES),
        },
        "strict_policy": {
            "require_encryption": True,
            "allow_remote_executor": True,
            "require_policy_manifest": True,
            "require_no_payload_retention": True,
        },
        "debug_override": {
            "require_encryption": False,
            "allow_remote_executor": True,
            "require_policy_manifest": False,
            "require_no_payload_retention": False,
        },
    }.get(profile)
    if defaults is None:
        profile = "standard"
        defaults = {
            "require_encryption": True,
            "allow_remote_executor": True,
            "require_policy_manifest": False,
            "require_no_payload_retention": False,
        }

    return RoutingPolicy(
        profile=profile,
        allowed_regions=src.allowed_regions if src.allowed_regions is not None else defaults.get("allowed_regions"),
        require_encryption=(
            src.require_encryption
            if src.require_encryption is not None
            else bool(defaults["require_encryption"])
        ),
        require_policy_manifest=(
            src.require_policy_manifest
            if src.require_policy_manifest is not None
            else bool(defaults["require_policy_manifest"])
        ),
        require_no_payload_retention=(
            src.require_no_payload_retention
            if src.require_no_payload_retention is not None
            else bool(defaults["require_no_payload_retention"])
        ),
        allow_remote_executor=(
            src.allow_remote_executor
            if src.allow_remote_executor is not None
            else bool(defaults["allow_remote_executor"])
        ),
        known_operator_only=bool(src.known_operator_only) if src.known_operator_only is not None else False,
    )


def filter_nodes_for_routing_policy(
    nodes: Iterable[Node],
    policy: RoutingPolicy | None,
    *,
    allow_plaintext_debug: bool = False,
) -> RoutingPolicyDecision:
    effective = resolved_policy(policy)
    eligible: list[Node] = []
    reasons: list[str] = []
    skipped_keyless = 0

    for node in nodes:
        reason = _node_rejection_reason(node, effective, allow_plaintext_debug=allow_plaintext_debug)
        if reason:
            reasons.append(reason)
            if reason == "missing_encryption_key":
                skipped_keyless += 1
            continue
        eligible.append(node)

    return RoutingPolicyDecision(eligible=eligible, rejected_reasons=reasons, skipped_keyless=skipped_keyless)


def routing_policy_refusal_message(intent: str, decision: RoutingPolicyDecision, policy: RoutingPolicy | None) -> str:
    effective = resolved_policy(policy)
    reason_summary = _summarize(decision.rejected_reasons)
    return (
        f"Routing policy {effective.profile!r} refused all discovered nodes for {intent!r} "
        f"before prompt dispatch; no prompt was sent. Reasons: {reason_summary}. "
        "Remote nodes can read prompts they execute; use local/browser mode for sensitive data "
        "or relax the policy explicitly."
    )


def _node_rejection_reason(
    node: Node,
    policy: RoutingPolicy,
    *,
    allow_plaintext_debug: bool,
) -> str | None:
    if policy.allow_remote_executor is False:
        return "remote_executor_disabled"
    if policy.allowed_regions and not _region_allowed(node.region, policy.allowed_regions):
        return "region_not_allowed"
    if policy.require_encryption and not node.cx_public_key and not allow_plaintext_debug:
        return "missing_encryption_key"
    manifest = node.node_policy_manifest if isinstance(node.node_policy_manifest, dict) else None
    if policy.require_policy_manifest and not manifest:
        return "missing_policy_manifest"
    if policy.require_no_payload_retention and not _declares_no_payload_retention(manifest):
        return "payload_retention_not_none"
    if policy.known_operator_only:
        return "known_operator_not_verified"
    return None


def _region_allowed(region: str, allowed: Iterable[str]) -> bool:
    value = (region or "").strip().lower()
    for raw in allowed:
        item = (raw or "").strip().lower()
        if not item:
            continue
        if value == item or value.startswith(f"{item}-"):
            return True
        if item == "eea" and value.startswith("eu-"):
            return True
    return False


def _declares_no_payload_retention(manifest: dict | None) -> bool:
    if not manifest:
        return False
    retention = manifest.get("retention")
    if not isinstance(retention, dict):
        return False
    return retention.get("task_payload") == "none"


def _summarize(reasons: list[str]) -> str:
    if not reasons:
        return "none"
    counts: dict[str, int] = {}
    for reason in reasons:
        counts[reason] = counts.get(reason, 0) + 1
    return ", ".join(f"{reason}={count}" for reason, count in sorted(counts.items()))
