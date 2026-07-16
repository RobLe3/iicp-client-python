"""Pre-normative authenticated policy-detail disclosure decision helper.

The caller supplies authentication results produced by its provider-side trust
adapter.  This module deliberately does not accept raw tokens or tickets and
therefore cannot accidentally treat unverified payload claims as identity.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ALLOWED_DETAIL_FIELDS = (
    "retention_intervals",
    "subprocessor_references",
    "approval_evidence_references",
    "operational_evidence_references",
)


@dataclass(frozen=True)
class PolicyDetailDisclosureDecision:
    status: int
    reason: str
    body: dict[str, Any] | None = None


def verify_policy_detail_consumer_token(
    token: str,
    public_key_hex: str,
    target_node_id: str,
    intent: str,
    now_s: int,
) -> tuple[str, dict[str, Any] | None]:
    """Verify the Directory's domain-separated consumer-token v1 format."""
    parts = token.split(".", 1)
    if len(parts) != 2 or len(parts[1]) != 128:
        return "invalid", None
    payload_b64, signature_hex = parts
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex)).verify(
            bytes.fromhex(signature_hex), b"iicp:consumer-token:v1\n" + payload_b64.encode()
        )
        padded = payload_b64 + "=" * ((4 - len(payload_b64) % 4) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, InvalidSignature, json.JSONDecodeError):
        return "invalid", None
    if (
        claims.get("v") != 1
        or claims.get("aud") != target_node_id
        or claims.get("intent") != intent
        or not isinstance(claims.get("sub"), str)
    ):
        return "invalid", None
    if int(claims.get("exp", 0)) <= now_s:
        return "expired", claims
    return "valid", claims


def evaluate_policy_detail_disclosure(context: dict[str, Any]) -> PolicyDetailDisclosureDecision:
    """Apply the portable authorization, concealment and redaction contract.

    ``consumer_auth`` MUST be the result of cryptographic verification by the
    integration adapter, never a value copied from an untrusted request body.
    """

    auth = context.get("consumer_auth")
    if auth == "missing":
        return PolicyDetailDisclosureDecision(401, "consumer_auth_required")
    if auth == "invalid" or auth not in {"valid", "expired"}:
        return PolicyDetailDisclosureDecision(401, "consumer_auth_invalid")
    if auth == "expired":
        return PolicyDetailDisclosureDecision(401, "consumer_auth_expired")
    if context.get("disclosure_allowed") is not True:
        return PolicyDetailDisclosureDecision(403, "disclosure_forbidden")

    binding = (
        context.get("provider_node_id")
        and context.get("provider_node_id") == context.get("consumer_target_node_id")
        and context.get("provider_node_id") == context.get("ticket_target_node_id")
        and context.get("consumer_intent")
        and context.get("consumer_intent") == context.get("ticket_intent")
        and context.get("manifest_sha256")
        and context.get("manifest_sha256") == context.get("ticket_manifest_sha256")
    )
    if not binding:
        return PolicyDetailDisclosureDecision(404, "resource_concealed")

    details = context.get("details")
    safe = {key: details[key] for key in ALLOWED_DETAIL_FIELDS if isinstance(details, dict) and key in details}
    return PolicyDetailDisclosureDecision(
        200,
        "compatible",
        {
            "profile": "urn:iicp:profile:policy-detail-disclosure:v0",
            "manifest_sha256": context["manifest_sha256"],
            "details": safe,
            "claim_status": "provider_declared",
        },
    )
