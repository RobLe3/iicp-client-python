"""Pre-normative policy/data-handling compatibility evaluator.

This module is opt-in. It evaluates caller requirements against provider claims;
it does not attest that a claim is true and is not wired into default routing.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

KNOWN_REQUIREMENTS = frozenset({
    "version", "role", "data_class", "remote_routing", "allowed_regions", "retention",
    "training_use", "subprocessors", "approval", "tool_risk",
    "requires_encryption", "requires_receipt", "requires_human_review",
    "critical_requirements",
})
_APPROVAL = {"none": 0, "user": 1, "operator": 2, "human_review": 3}
_TOOL_RISK = {"none": 0, "read_only": 1, "write": 2, "privileged": 3}

@dataclass(frozen=True)
class PolicyDataDecision:
    eligible: bool
    reason: str


def evaluate_policy_data_handling(
    requirement: Mapping[str, Any],
    declaration: Mapping[str, Any],
    context: Mapping[str, Any] | None = None,
) -> PolicyDataDecision:
    """Return a portable compatibility decision using fixture-defined precedence."""
    ctx = context or {}
    critical = set(requirement.get("critical_requirements", ()))
    if any(field not in KNOWN_REQUIREMENTS for field in critical):
        return PolicyDataDecision(False, "unsupported_policy_requirement")
    if requirement.get("remote_routing") == "local_only":
        return PolicyDataDecision(False, "remote_routing_forbidden")
    accepted = declaration.get("accepted_data_classes", ())
    if requirement.get("data_class") not in accepted:
        return PolicyDataDecision(False, "data_class_not_accepted")
    if requirement.get("remote_routing") == "requires_approval" and not ctx.get("approval_granted", False):
        return PolicyDataDecision(False, "approval_required")
    regions = requirement.get("allowed_regions")
    if regions and declaration.get("jurisdiction") not in regions:
        return PolicyDataDecision(False, "region_not_allowed")
    required_retention = requirement.get("retention", {})
    declared_retention = declaration.get("retention", {})
    required_mode = required_retention.get("task_payload")
    declared_mode = declared_retention.get("task_payload")
    if required_mode == "none" and declared_mode != "none":
        return PolicyDataDecision(False, "retention_requirement_unsatisfied")
    if required_mode == "transient":
        if declared_mode not in {"none", "transient"}:
            return PolicyDataDecision(False, "retention_requirement_unsatisfied")
        required_max = required_retention.get("max_seconds")
        declared_max = 0 if declared_mode == "none" else declared_retention.get("max_seconds")
        if required_max is not None and (declared_max is None or declared_max > required_max):
            return PolicyDataDecision(False, "retention_requirement_unsatisfied")
    if requirement.get("training_use") == "none" and declaration.get("training_use") != "none":
        return PolicyDataDecision(False, "training_use_requirement_unsatisfied")
    if requirement.get("subprocessors") == "none" and declaration.get("subprocessors") != "none":
        return PolicyDataDecision(False, "subprocessor_requirement_unsatisfied")
    required_approval = requirement.get("approval")
    if required_approval is not None and _APPROVAL.get(declaration.get("approval", "none"), -1) < _APPROVAL.get(required_approval, 99):
        return PolicyDataDecision(False, "approval_requirement_unsatisfied")
    allowed_tool_risk = requirement.get("tool_risk")
    if allowed_tool_risk is not None and _TOOL_RISK.get(declaration.get("tool_risk", "privileged"), 99) > _TOOL_RISK.get(allowed_tool_risk, -1):
        return PolicyDataDecision(False, "tool_risk_requirement_unsatisfied")
    if requirement.get("requires_encryption") and not ctx.get("encryption_ready", False):
        return PolicyDataDecision(False, "encryption_requirement_unsatisfied")
    if requirement.get("requires_receipt") and not ctx.get("receipt_supported", False):
        return PolicyDataDecision(False, "receipt_requirement_unsatisfied")
    if requirement.get("requires_human_review") and not declaration.get("requires_human_review", False):
        return PolicyDataDecision(False, "human_review_requirement_unsatisfied")
    return PolicyDataDecision(True, "compatible")
