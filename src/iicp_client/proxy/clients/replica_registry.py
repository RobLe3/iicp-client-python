"""
Trusted-replicas registry client (P6-4.2a).

Fetches the canonical replica list from `<seed>/.well-known/iicp-replicas.json`
(S.13 §6.4 / DIR-FED-19). Used by the proxy to rank 307-redirect targets by
trust_tier, and (in P6-4.2b) to obtain the DID for replica signature verification.

The registry is static metadata, refreshed periodically. Dynamic state
(last_seen_at, event_log_lag_ms) is NOT in the registry — proxies query
each replica's /api/v1/stats for freshness.
"""
from __future__ import annotations

import logging
import ssl
import time
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_REQUIRED_FIELDS = ("replica_id", "did", "endpoint", "trust_tier", "registered_at")
_VALID_TIERS = frozenset({"low", "medium", "high"})
_DEFAULT_TTL_SECONDS = 3600.0  # 1h — registry is operator-driven, low churn


def _tls_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_default_certs()
    return ctx


class ReplicaRegistry:
    """Caches the seed's /.well-known/iicp-replicas.json document.

    Look up a replica by its endpoint URL to retrieve its DID + trust_tier
    + other published metadata. Returns None for unknown endpoints (which
    a proxy MUST treat as untrusted — no signature verification path exists).
    """

    def __init__(self, seed_url: str, ttl_s: float = _DEFAULT_TTL_SECONDS) -> None:
        self._seed_url = seed_url.rstrip("/")
        self._ttl_s = ttl_s
        self._cache_at: float = 0.0
        self._entries_by_endpoint: dict[str, dict[str, Any]] = {}

    async def refresh(self) -> int:
        """Fetch + parse the registry. Returns count of valid entries loaded.

        On fetch failure or malformed registry, the previous cache is kept
        (degraded operation — better than losing all trust signals on a
        transient network blip).
        """
        url = f"{self._seed_url}/.well-known/iicp-replicas.json"
        try:
            async with httpx.AsyncClient(timeout=10.0, verify=_tls_context()) as client:
                resp = await client.get(url)
        except Exception as exc:  # noqa: BLE001 — degraded mode on any fetch failure
            logger.warning("Replica registry fetch failed: %s", exc)
            return -1
        if resp.status_code != 200:
            logger.warning("Replica registry returned HTTP %d", resp.status_code)
            return -1
        try:
            doc = resp.json()
        except Exception as exc:  # noqa: BLE001 — malformed JSON
            logger.warning("Replica registry JSON parse failed: %s", exc)
            return -1
        if str(doc.get("schema_version", "")) != "2":
            logger.warning(
                "Replica registry schema_version mismatch: expected '2', got %r",
                doc.get("schema_version"),
            )
            return -1

        new_entries: dict[str, dict[str, Any]] = {}
        for i, entry in enumerate(doc.get("replicas", [])):
            if not isinstance(entry, dict):
                continue
            if not all(f in entry for f in _REQUIRED_FIELDS):
                logger.warning("Registry entry %d missing required field(s); skipping", i)
                continue
            if entry["trust_tier"] not in _VALID_TIERS:
                logger.warning(
                    "Registry entry %d has invalid trust_tier=%r; skipping",
                    i, entry["trust_tier"],
                )
                continue
            endpoint = str(entry["endpoint"]).rstrip("/")
            if not endpoint.startswith("https://"):
                logger.warning("Registry entry %d endpoint not https; skipping", i)
                continue
            new_entries[endpoint] = entry

        self._entries_by_endpoint = new_entries
        self._cache_at = time.monotonic()
        return len(new_entries)

    async def lookup(self, endpoint: str) -> dict[str, Any] | None:
        """Return the registry entry for an endpoint URL, or None if unknown.

        Auto-refreshes if the cache is stale (older than ttl_s). Treats
        scheme + host + port as the key (path stripped) so a request to
        `https://r.test/v1/discover?...` resolves the entry for endpoint
        `https://r.test`.
        """
        now = time.monotonic()
        if now - self._cache_at > self._ttl_s:
            await self.refresh()
        parsed = urlparse(endpoint)
        if not parsed.hostname:
            return None
        port = parsed.port
        host_key = f"{parsed.scheme}://{parsed.hostname}"
        if port and port != 443:
            host_key += f":{port}"
        return self._entries_by_endpoint.get(host_key)

    def trust_tier_of(self, endpoint: str) -> str:
        """Synchronous best-effort tier lookup against the current cache.

        Returns 'low' for any endpoint not in the registry — a proxy SHOULD
        treat unknown replicas as untrusted. Does NOT auto-refresh; callers
        that need fresh data must await lookup() first.
        """
        parsed = urlparse(endpoint)
        if not parsed.hostname:
            return "low"
        port = parsed.port
        host_key = f"{parsed.scheme}://{parsed.hostname}"
        if port and port != 443:
            host_key += f":{port}"
        entry = self._entries_by_endpoint.get(host_key)
        if entry is None:
            return "low"
        return entry.get("trust_tier", "low")

    @property
    def entry_count(self) -> int:
        return len(self._entries_by_endpoint)
