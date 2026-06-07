"""
DID document resolver for replica public keys (P6-4.2b-ii).

Fetches `<base>/.well-known/did.json` and extracts the first Ed25519 OKP
verification method, returning the raw 32-byte public key. Cached per-origin
with TTL — the registry's `did` field tells us WHICH key authority to trust;
this resolver dereferences that authority to raw key material.

Parity with directory-side App\\Services\\SeedDidResolver — different language,
same behavior, intentional 1:1 mapping so future schema changes propagate
identically to both sides.
"""
from __future__ import annotations

import base64
import logging
import ssl
import time

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 86400.0  # 24h — DID docs are stable; rotation is rare
_PLACEHOLDER_KEY = "GENESIS_KEY_PENDING"


def _tls_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_default_certs()
    return ctx


class DidResolver:
    """Fetch + cache Ed25519 public keys from /.well-known/did.json.

    Use case: a proxy receives a discover response signed by a replica;
    looks up the replica's `did` from the registry; calls
    public_key(replica_base_url) to get the raw key to feed
    verify_replica_sig().
    """

    def __init__(self, ttl_s: float = _DEFAULT_TTL_SECONDS) -> None:
        self._ttl_s = ttl_s
        # base_url → (raw_pub_key | None, cached_at_monotonic)
        # None means "fetched but no valid key" (negative cache, same TTL)
        self._cache: dict[str, tuple[bytes | None, float]] = {}

    async def public_key(self, base_url: str) -> bytes | None:
        """Return the raw 32-byte Ed25519 public key for `base_url`, or None."""
        key = base_url.rstrip("/")
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and now - cached[1] < self._ttl_s:
            return cached[0]
        pub = await self._fetch_and_extract(key)
        self._cache[key] = (pub, now)
        return pub

    def forget(self, base_url: str) -> None:
        self._cache.pop(base_url.rstrip("/"), None)

    async def _fetch_and_extract(self, base_url: str) -> bytes | None:
        url = f"{base_url}/.well-known/did.json"
        try:
            async with httpx.AsyncClient(timeout=10.0, verify=_tls_context()) as client:
                resp = await client.get(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("DID fetch failed for %s: %s", url, exc)
            return None
        if resp.status_code != 200:
            logger.warning("DID document %s returned HTTP %d", url, resp.status_code)
            return None
        try:
            doc = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("DID document %s JSON parse failed: %s", url, exc)
            return None

        methods = doc.get("verificationMethod") or []
        for method in methods:
            jwk = method.get("publicKeyJwk")
            if not isinstance(jwk, dict):
                continue
            if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
                continue
            x = jwk.get("x") or ""
            if not x or x == _PLACEHOLDER_KEY:
                continue
            try:
                padding = "=" * (-len(x) % 4)
                raw = base64.urlsafe_b64decode(x + padding)
            except Exception:  # noqa: BLE001
                continue
            if len(raw) != 32:
                continue
            return raw
        return None
