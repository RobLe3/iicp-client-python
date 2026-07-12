"""Pre-normative intent/capability profile compatibility evaluator.

This module is deliberately additive: it evaluates a draft profile supplied by
the caller and does not alter current discovery or task wire formats.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class ProfileCompatibilityDecision:
    eligible: bool
    reason: str


def evaluate_pre_normative_profile(
    request: dict[str, Any], provider: dict[str, Any], aliases: Iterable[dict[str, str]] = (), now_s: int = 0,
) -> ProfileCompatibilityDecision:
    """Evaluate the fixture-gated draft profile without changing live routing."""
    if request.get("policy") == "deny":
        return ProfileCompatibilityDecision(False, "policy_refusal")
    binding = request.get("mapping_kind")
    if binding is not None and binding not in {"a2a_skill", "mcp_tool"}:
        return ProfileCompatibilityDecision(False, "unsupported_binding")
    alias_map = {item.get("from"): item.get("to") for item in aliases}
    requested_intent = alias_map.get(request.get("intent"), request.get("intent"))
    provider_intent = alias_map.get(provider.get("intent"), provider.get("intent"))
    if requested_intent != provider_intent:
        return ProfileCompatibilityDecision(False, "intent_mismatch")
    if request.get("schema_digest") and provider.get("schema_digest") != request.get("schema_digest"):
        return ProfileCompatibilityDecision(False, "schema_digest_mismatch")
    supported = {item.get("uri") for item in provider.get("extensions", []) if isinstance(item, dict)}
    for extension in request.get("extensions", []):
        if not isinstance(extension, dict) or not extension.get("required"):
            continue
        if extension.get("experimental") and int(extension.get("review_expires_at_s", 0)) <= now_s:
            return ProfileCompatibilityDecision(False, "experimental_extension_expired")
        if extension.get("uri") not in supported:
            return ProfileCompatibilityDecision(False, "required_extension_missing")
    return ProfileCompatibilityDecision(True, "compatible")
