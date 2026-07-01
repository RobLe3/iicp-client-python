from __future__ import annotations

import base64
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from iicp_client.relay_ticket import verify_relay_bind_ticket


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _signed_ticket(claims: dict) -> tuple[str, str]:
    private = Ed25519PrivateKey.generate()
    public_hex = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()
    payload = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    sig = private.sign(b"iicp:relay-bind-ticket:v1\n" + payload.encode()).hex()
    return f"{payload}.{sig}", public_hex


def test_relay_bind_ticket_accepts_valid_worker_and_audience():
    token, pub = _signed_ticket({
        "v": 1, "typ": "relay-bind-ticket", "iss": "test",
        "sub": "worker-1", "aud": "relay-1", "iat": 1, "exp": 999_999,
    })
    claims = verify_relay_bind_ticket(token, pub, "worker-1", "relay-1", now_s=100)
    assert claims is not None
    assert claims.sub == "worker-1"


def test_relay_bind_ticket_rejects_wrong_worker_audience_expiry_and_tamper():
    token, pub = _signed_ticket({
        "v": 1, "typ": "relay-bind-ticket", "iss": "test",
        "sub": "worker-1", "aud": "relay-1", "iat": 1, "exp": 999_999,
    })
    tampered = token[:-1] + ("1" if token[-1] != "1" else "0")
    assert verify_relay_bind_ticket(token, pub, "attacker", "relay-1", now_s=100) is None
    assert verify_relay_bind_ticket(token, pub, "worker-1", "relay-2", now_s=100) is None
    assert verify_relay_bind_ticket(token, pub, "worker-1", "relay-1", now_s=1_000_000) is None
    assert verify_relay_bind_ticket(tampered, pub, "worker-1", "relay-1", now_s=100) is None
