"""Small deterministic helpers for proxy ticket-verification tests."""

from __future__ import annotations

import base64
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from iicp_client.dispatch_ticket import AUDIENCE, DOMAIN


def public_key_hex(private_key: Ed25519PrivateKey) -> str:
    return private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def signed_ticket(
    private_key: Ed25519PrivateKey,
    *,
    issuer: str,
    node_id: str,
    intent: str,
    jti: str,
) -> str:
    claims = {
        "v": 1,
        "typ": "dispatch-route-ticket",
        "iss": issuer,
        "aud": AUDIENCE,
        "jti": jti,
        "node_id": node_id,
        "intent": intent,
        "iat": 1_700_000_000,
        "exp": 4_102_444_800,
    }
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    payload_b64 = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = private_key.sign(DOMAIN + payload_b64.encode()).hex()
    return f"{payload_b64}.{signature}"
