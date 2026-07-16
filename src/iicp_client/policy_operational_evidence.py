"""Opt-in evaluator for pre-normative policy operational evidence.

The ``verified`` input is local authenticated-verifier context. It must not be
copied from an untrusted provider declaration and is not legal certification.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

KNOWN_EVIDENCE_TYPES = frozenset({
    "retention_control", "subprocessor_disclosure", "approval_event",
})


@dataclass(frozen=True)
class PolicyEvidenceDecision:
    eligible: bool
    reason: str


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def evaluate_policy_operational_evidence(
    requirement: Mapping[str, Any],
    context: Mapping[str, Any],
    evaluated_at: str,
) -> PolicyEvidenceDecision:
    required = tuple(requirement.get("required_evidence", ()))
    if any(kind not in KNOWN_EVIDENCE_TYPES for kind in required):
        return PolicyEvidenceDecision(False, "unsupported_evidence_requirement")
    if requirement.get("manifest_sha256") != context.get("manifest_sha256"):
        return PolicyEvidenceDecision(False, "manifest_digest_mismatch")
    evidence: Sequence[Mapping[str, Any]] = context.get("evidence", ())
    now = _instant(evaluated_at)
    for kind in required:
        matches = [item for item in evidence if item.get("type") == kind]
        if not matches:
            return PolicyEvidenceDecision(False, "evidence_missing")
        verified = [item for item in matches if item.get("verified") is True]
        if not verified:
            return PolicyEvidenceDecision(False, "evidence_unauthenticated")
        if not any(isinstance(item.get("expires_at"), str) and _instant(item["expires_at"]) > now for item in verified):
            return PolicyEvidenceDecision(False, "evidence_expired")
    return PolicyEvidenceDecision(True, "compatible")
