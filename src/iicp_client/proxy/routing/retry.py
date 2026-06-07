# SPDX-License-Identifier: Apache-2.0
"""Retry manager — bounded, jittered, on a narrow allow-list of retriable errors.

Why an allow-list (vs. retry-on-any-exception): retrying a 4xx other than 429
is wasted work and can amplify a misconfiguration storm. Only transient
transport errors (timeouts, connect errors, mid-stream protocol drops) and
specific overload signals (429, 503) are retried.

Cross-references:
    - project/RELIABILITY.md — retry policy spec (max 3, exponential, ±20% jitter)
    - ADR-010 — idempotency: safe-by-design because the proxy uses the same task_id

Backoff formula: base * 2^attempt * (0.8 + 0.4*rand) — the ±20% jitter avoids
thundering-herd recovery patterns when many proxies retry the same upstream."""
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx

from iicp_client.proxy.metrics import record_retry

logger = logging.getLogger(__name__)

T = TypeVar("T")

_RETRIABLE = (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)


def _is_capacity_exceeded(exc: httpx.HTTPStatusError) -> bool:
    """Spec §2.2: capacity_exceeded 429 must be node-switched without backoff."""
    if exc.response.status_code != 429:
        return False
    try:
        body = exc.response.json()
        return (body.get("error") or {}).get("code") == "capacity_exceeded"
    except Exception:
        return False


class CapacityExceededError(Exception):
    """Raised when a node returns capacity_exceeded 429 — caller must switch nodes."""


class RetryManager:
    """Bounded retry with jittered exponential backoff.

    Defaults: 3 attempts total, 200 ms base. After the last attempt the most
    recent exception is re-raised so FallbackChain can move to the next node.
    """

    def __init__(self, max_retries: int = 3, base_ms: int = 200) -> None:
        self._max = max_retries
        self._base = base_ms / 1000.0

    async def run(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Run `fn` up to `max_retries` times against the retriable error set.

        On HTTPStatusError, 429 and 503 are treated as transient (server-side
        overload); every other 4xx/5xx raises immediately — retrying a 400 or
        a 401 would just amplify the bad request. The function is invoked with
        no arguments to keep the retry contract trivially substitutable.
        """
        last_exc: Exception | None = None
        for attempt in range(self._max):
            try:
                return await fn()
            except _RETRIABLE as exc:
                last_exc = exc
                record_retry(reason=type(exc).__name__)
                if attempt < self._max - 1:
                    delay = self._base * (2**attempt) * (0.8 + 0.4 * random.random())
                    logger.warning("retry %d/%d in %.0fms", attempt + 1, self._max, delay * 1000)
                    await asyncio.sleep(delay)
            except httpx.HTTPStatusError as exc:
                if _is_capacity_exceeded(exc):
                    # Spec §2.2: node-switch immediately, no backoff on this node
                    record_retry(reason="capacity_exceeded")
                    raise CapacityExceededError(str(exc)) from exc
                if exc.response.status_code in (429, 503):
                    last_exc = exc
                    record_retry(reason=str(exc.response.status_code))
                    if attempt < self._max - 1:
                        delay = self._base * (2**attempt) * (0.8 + 0.4 * random.random())
                        await asyncio.sleep(delay)
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    def is_retriable(self, exc: Exception) -> bool:
        return isinstance(exc, (*_RETRIABLE, httpx.HTTPStatusError))
