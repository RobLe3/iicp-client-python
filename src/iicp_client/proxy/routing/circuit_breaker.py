# SPDX-License-Identifier: Apache-2.0
"""Per-node circuit breaker — 5 consecutive failures → open, 30s half-open probe.

Without a circuit breaker, every call to a failed node pays the full per-node
timeout before the proxy moves on. With it, persistently-failing nodes are
short-circuited at the cost of one extra probe per `reset_s` window.

Cross-references:
    - project/RELIABILITY.md — circuit-breaker policy (5 failures, 30s recovery)
    - ADR-008 — directory score eventually reflects failure; the breaker is an
      in-process fast-path that does NOT mutate the directory's view

In-process state only: each proxy instance has its own breaker. This is fine
because failures correlate strongly per node (network or backend) and the
discovery layer already smooths cross-client opinions via the directory.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict

logger = logging.getLogger(__name__)


class CircuitOpenError(Exception):
    """Raised when a circuit is open and the request should not be attempted."""

    def __init__(self, node_id: str) -> None:
        super().__init__(f"circuit open for node {node_id}")
        self.node_id = node_id


class CircuitBreaker:
    """Three-state breaker: closed (default), open (skip), half-open (one probe).

    `_failures` counts CONSECUTIVE failures — a single success clears it. Once
    the count crosses `threshold` the breaker opens; opening stamps
    `_opened_at` with the current monotonic time. After `reset_s` elapses,
    `allow()` returns True for one probe; success clears state, failure
    re-stamps `_opened_at` (effectively keeping the breaker open another window).
    """

    def __init__(self, threshold: int = 5, reset_s: int = 30) -> None:
        self._threshold = threshold
        self._reset_s = reset_s
        self._failures: dict[str, int] = defaultdict(int)
        self._opened_at: dict[str, float] = {}

    def allow(self, node_id: str) -> bool:
        """True when a request to `node_id` should be attempted.

        Returns True when either (a) the breaker is closed or (b) the breaker
        is open but `reset_s` has elapsed (half-open probe window).
        """
        if node_id not in self._opened_at:
            return True
        elapsed = time.monotonic() - self._opened_at[node_id]
        if elapsed >= self._reset_s:
            # half-open: allow one probe
            return True
        return False

    def record_success(self, node_id: str) -> None:
        """Reset all state for a node — both the consecutive-failure count and
        the open timestamp. One success closes the breaker fully (no slow
        ramp-up); the directory's reputation score handles the longer-term
        memory of past failures.
        """
        self._failures.pop(node_id, None)
        self._opened_at.pop(node_id, None)

    def record_failure(self, node_id: str) -> None:
        """Increment the failure count and (re)open the breaker if needed.

        The `if node_id not in self._opened_at` guard ensures we only log the
        transition once per open episode, not every subsequent failure.
        """
        self._failures[node_id] += 1
        if self._failures[node_id] >= self._threshold:
            if node_id not in self._opened_at:
                logger.warning("circuit opened for node %s", node_id)
            self._opened_at[node_id] = time.monotonic()

    def check(self, node_id: str) -> None:
        """Raise CircuitOpenError when a node is open — saves a wasted HTTP call.

        TaskRouter calls this BEFORE attempting the request so we don't pay
        the timeout cost for a node we already know is bad.
        """
        if not self.allow(node_id):
            raise CircuitOpenError(node_id)
