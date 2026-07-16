"""Verification for directory-signed, disclosure-only dispatch route tickets."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

DOMAIN = b"iicp:dispatch-route-ticket:v1\n"
AUDIENCE = "iicp.directory.dispatch"


@dataclass(frozen=True)
class DispatchRouteTicketClaims:
    v: int
    typ: str
    iss: str
    aud: str
    jti: str
    node_id: str
    intent: str
    iat: int
    exp: int
    policy_manifest_sha256: str | None = None


def _b64pad(value: str) -> bytes:
    return (value + "=" * ((4 - len(value) % 4) % 4)).replace("-", "+").replace("_", "/").encode()


def verify_dispatch_route_ticket(
    token: str, public_key_hex: str, issuer: str, node_id: str, intent: str, now_s: int | None = None
) -> DispatchRouteTicketClaims | None:
    parts = token.split(".", 1)
    if len(parts) != 2 or len(parts[1]) != 128:
        return None
    payload_b64, signature_hex = parts
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex)).verify(
            bytes.fromhex(signature_hex), DOMAIN + payload_b64.encode()
        )
        payload: dict[str, Any] = json.loads(base64.b64decode(_b64pad(payload_b64)))
    except (ValueError, InvalidSignature, json.JSONDecodeError):
        return None
    now = int(time.time()) if now_s is None else now_s
    if (
        payload.get("v") != 1
        or payload.get("typ") != "dispatch-route-ticket"
        or payload.get("iss") != issuer
        or payload.get("aud") != AUDIENCE
        or payload.get("node_id") != node_id
        or payload.get("intent") != intent
        or int(payload.get("exp", 0)) <= now
    ):
        return None
    jti = payload.get("jti")
    if not isinstance(jti, str) or len(jti) != 24 or any(c not in "0123456789abcdef" for c in jti):
        return None
    policy_digest = payload.get("policy_manifest_sha256")
    if policy_digest is not None and (
        not isinstance(policy_digest, str)
        or len(policy_digest) != 64
        or any(c not in "0123456789abcdef" for c in policy_digest)
    ):
        return None
    fields = {key: payload.get(key) for key in DispatchRouteTicketClaims.__annotations__}
    return DispatchRouteTicketClaims(**fields)


def policy_manifest_binding_matches(claims: DispatchRouteTicketClaims, route: object) -> bool:
    """Require the route's public manifest digest when a ticket binds one."""
    if claims.policy_manifest_sha256 is None:
        return True
    if not isinstance(route, dict):
        return False
    manifest = route.get("node_policy_manifest")
    verification = manifest.get("verification") if isinstance(manifest, dict) else None
    route_digest = verification.get("canonical_sha256") if isinstance(verification, dict) else None
    return route_digest == claims.policy_manifest_sha256
