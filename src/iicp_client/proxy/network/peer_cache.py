# SPDX-License-Identifier: Apache-2.0
"""PeerCache — intent-level node list cache with background refresh (Phase 2).

WHY cache at the proxy rather than poll per-task: the IICP directory is shared
infrastructure on shared hosting. Polling it on every routing call (potentially
thousands of tasks/minute on a busy proxy) would overload the PHP/MySQL backend.
A 30s TTL (default) means at most one directory call per active intent per 30s,
regardless of request rate.

WHY 30s TTL specifically: matches the adapter heartbeat cadence (30s). A node that
misses one heartbeat is unlikely to be expired yet (directory EXPIRY_SECONDS > 60s),
but any node that missed *two* heartbeats should no longer appear in discover results.
A 30s cache window keeps the view roughly in sync without over-polling.

WHY bootstrap on start: the first routing call is latency-sensitive. Pre-warming the
cache with seed nodes from /v1/bootstrap avoids a cold-cache penalty on the first task.

Spec: spec/iicp-dir.md §discover. ADR: ADR-003 (directory load reduction).
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TTL_S = 30.0
_DEFAULT_TIMEOUT_S = 5.0


class PeerCache:
    """Caches per-intent node lists from the directory with a TTL.

    When the cache is fresh, task routing uses the cached list without hitting
    the directory. On expiry, the next routing call refreshes the cache.
    """

    def __init__(self, directory_url: str, ttl_s: float = _DEFAULT_TTL_S) -> None:
        self._directory_url = directory_url.rstrip("/")
        self._ttl_s = ttl_s
        self._cache: dict[str, tuple[list[dict], float]] = {}  # intent → (nodes, ts)
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Warm the cache by bootstrapping from directory."""
        await self._bootstrap()
        self._refresh_task = asyncio.create_task(self._refresh_loop(), name="peer-refresh")

    def stop(self) -> None:
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()

    async def get_nodes(
        self,
        intent: str,
        region: str | None = None,
        limit: int = 10,
    ) -> list[dict] | None:
        """Return cached nodes for the intent, or None if cache is stale."""
        async with self._lock:
            entry = self._cache.get(intent)
        if entry is None:
            return None
        nodes, ts = entry
        if time.monotonic() - ts > self._ttl_s:
            return None
        return nodes

    async def fetch_and_cache(
        self,
        intent: str,
        region: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Query directory and update cache. Always returns fresh data."""
        params: dict[str, str | int] = {"intent": intent, "limit": limit}
        if region:
            params["region"] = region

        try:
            async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_S) as client:
                resp = await client.get(
                    f"{self._directory_url}/v1/discover",
                    params=params,
                )
                resp.raise_for_status()
                nodes: list[dict] = resp.json().get("nodes", [])
        except Exception as exc:
            logger.warning("Directory discover failed for %s: %s", intent, exc)
            return []

        async with self._lock:
            self._cache[intent] = (nodes, time.monotonic())

        return nodes

    async def _bootstrap(self) -> None:
        """Fetch seed nodes from /v1/bootstrap to warm up."""
        try:
            async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_S) as client:
                resp = await client.get(f"{self._directory_url}/v1/bootstrap")
                if resp.is_success:
                    count = resp.json().get("count", 0)
                    logger.info("Peer cache bootstrap: %d seed nodes available", count)
        except Exception as exc:
            logger.debug("Peer cache bootstrap skipped: %s", exc)

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self._ttl_s)
            async with self._lock:
                intents = list(self._cache.keys())
            for intent in intents:
                await self.fetch_and_cache(intent)
