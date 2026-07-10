"""Directory-signed relay bind ticket helpers (#510 / DIR-RELAY-03)."""
from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_DOMAIN = b"iicp:relay-bind-ticket:v1\n"


@dataclass(frozen=True)
class RelayBindTicketClaims:
    v: int
    typ: str
    jti: str
    iss: str
    sub: str
    aud: str
    iat: int
    exp: int


class RelayBindTicketReplayCache:
    """Process-local, atomic one-use cache for accepted relay bind tickets."""

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}
        self._lock = threading.Lock()

    def consume(self, claims: RelayBindTicketClaims, now_s: int | None = None) -> bool:
        now = int(time.time()) if now_s is None else now_s
        with self._lock:
            self._seen = {jti: exp for jti, exp in self._seen.items() if exp > now}
            if claims.jti in self._seen:
                return False
            self._seen[claims.jti] = claims.exp
            return True


_RELAY_BIND_REPLAY_CACHE = RelayBindTicketReplayCache()


def consume_relay_bind_ticket(claims: RelayBindTicketClaims, now_s: int | None = None) -> bool:
    return _RELAY_BIND_REPLAY_CACHE.consume(claims, now_s)


def _b64pad(value: str) -> bytes:
    return (value + "=" * ((4 - len(value) % 4) % 4)).replace("-", "+").replace("_", "/").encode()


def verify_relay_bind_ticket(
    token: str,
    public_key_hex: str,
    worker_id: str,
    relay_audience: str = "*",
    now_s: int | None = None,
) -> RelayBindTicketClaims | None:
    parts = token.split(".", 1)
    if len(parts) != 2 or len(parts[1]) != 128:
        return None
    b64_payload, sig_hex = parts
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        pub.verify(bytes.fromhex(sig_hex), _DOMAIN + b64_payload.encode())
        payload: dict[str, Any] = json.loads(base64.b64decode(_b64pad(b64_payload)))
    except (ValueError, InvalidSignature, json.JSONDecodeError):
        return None
    if payload.get("typ") != "relay-bind-ticket":
        return None
    jti = payload.get("jti")
    if not isinstance(jti, str) or len(jti) != 32 or any(ch not in "0123456789abcdef" for ch in jti):
        return None
    if payload.get("sub") != worker_id:
        return None
    now = int(time.time()) if now_s is None else now_s
    if int(payload.get("exp", 0)) <= now:
        return None
    aud = str(payload.get("aud", ""))
    if aud != "*" and aud != relay_audience:
        return None
    return RelayBindTicketClaims(
        v=int(payload.get("v", 0)),
        typ=str(payload.get("typ", "")),
        jti=jti,
        iss=str(payload.get("iss", "")),
        sub=str(payload.get("sub", "")),
        aud=aud,
        iat=int(payload.get("iat", 0)),
        exp=int(payload.get("exp", 0)),
    )


async def fetch_relay_bind_ticket(
    directory_url: str,
    node_token: str,
    worker_id: str,
    relay_node_id: str | None = None,
) -> str | None:
    url = directory_url.rstrip("/") + "/v1/relay/ticket"
    headers = {
        "Authorization": f"Bearer {node_token}",
        "X-Node-Id": worker_id,
    }
    body = {"relay_node_id": relay_node_id} if relay_node_id else {}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json=body, headers=headers)
    if resp.status_code >= 400:
        return None
    data = resp.json()
    ticket = data.get("ticket")
    return ticket if isinstance(ticket, str) else None
