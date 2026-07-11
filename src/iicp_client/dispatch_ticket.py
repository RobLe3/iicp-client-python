"""Verification for directory-signed, disclosure-only dispatch route tickets."""
from __future__ import annotations
import base64, json, time
from dataclasses import dataclass
from typing import Any
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

DOMAIN = b"iicp:dispatch-route-ticket:v1\n"
AUDIENCE = "iicp.directory.dispatch"

@dataclass(frozen=True)
class DispatchRouteTicketClaims:
    v: int; typ: str; iss: str; aud: str; jti: str; node_id: str; intent: str; iat: int; exp: int

def _b64pad(value: str) -> bytes:
    return (value + "=" * ((4 - len(value) % 4) % 4)).replace("-", "+").replace("_", "/").encode()

def verify_dispatch_route_ticket(token: str, public_key_hex: str, issuer: str, node_id: str, intent: str, now_s: int | None = None) -> DispatchRouteTicketClaims | None:
    parts = token.split(".", 1)
    if len(parts) != 2 or len(parts[1]) != 128:
        return None
    payload_b64, signature_hex = parts
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex)).verify(bytes.fromhex(signature_hex), DOMAIN + payload_b64.encode())
        payload: dict[str, Any] = json.loads(base64.b64decode(_b64pad(payload_b64)))
    except (ValueError, InvalidSignature, json.JSONDecodeError):
        return None
    now = int(time.time()) if now_s is None else now_s
    if (payload.get("v") != 1 or payload.get("typ") != "dispatch-route-ticket" or payload.get("iss") != issuer or payload.get("aud") != AUDIENCE or payload.get("node_id") != node_id or payload.get("intent") != intent or int(payload.get("exp", 0)) <= now):
        return None
    jti = payload.get("jti")
    if not isinstance(jti, str) or len(jti) != 24 or any(c not in "0123456789abcdef" for c in jti):
        return None
    return DispatchRouteTicketClaims(**{k: payload[k] for k in DispatchRouteTicketClaims.__annotations__})
