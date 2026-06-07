# SPDX-License-Identifier: Apache-2.0
"""Phase 5A CIP routing strategies — S.12 §2.2 local-first precedence.

LocalFirstStrategy implements the S.12 §2.2 MUST: prefer local inference
when a local provider is available and within capacity. Remote dispatch is
a fallback, not the default.

A node is considered "local" when its endpoint resolves to a loopback address
(127.0.0.1 or [::1]) or the literal hostname "localhost". This covers the
standard Ollama or adapter deployment on the same host as the proxy.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _is_local_endpoint(endpoint: str) -> bool:
    """Return True when the endpoint's host portion is a loopback address."""
    try:
        # Strip scheme (https://host:port/path)
        without_scheme = endpoint.split("://", 1)[-1]
        host = without_scheme.split("/")[0].split(":")[0].strip("[]")
        return host in _LOOPBACK_HOSTS
    except Exception:
        return False


@dataclass
class NodeInfo:
    """Minimal node descriptor for strategy evaluation."""

    node_id: str
    endpoint: str
    intent: str | None = None


@dataclass
class LocalFirstStrategy:
    """S.12 §2.2: prefer local (loopback) provider; fall back to remote.

    Call `should_dispatch_remote(nodes, intent)` to determine whether
    the task should be routed remotely. Returns False (stay local) when
    at least one available node has a loopback endpoint matching the
    intent; returns True (go remote) when no local node is found.

    The strategy does NOT inspect node capacity — callers that receive a
    LOCAL decision and find the local provider full should retry with an
    empty `local_nodes` list to trigger the remote fallback.
    """

    def should_dispatch_remote(
        self,
        node_list: Sequence[NodeInfo],
        intent: str | None = None,
    ) -> bool:
        """Return True iff no local node can serve this intent.

        A local node matches when:
        - Its endpoint resolves to a loopback address, AND
        - Either no intent filter is set, or the node's intent matches.
        """
        for node in node_list:
            if not _is_local_endpoint(node.endpoint):
                continue
            if intent is None or node.intent is None or node.intent == intent:
                return False  # local node found → keep local
        return True  # no local node → fall back to remote


@dataclass
class SessionBudgetTracker:
    """Track cumulative credit spend across all CIP tasks in a session.

    `session_credit_budget` is the ceiling in credits. Pass `None` for
    an unlimited session. Call `can_spend(credits)` before dispatching;
    call `record_spend(credits)` after a successful award.
    """

    session_credit_budget: float | None = None
    _spent: float = field(default=0.0, init=False)

    def can_spend(self, estimated_credits: float) -> bool:
        """Return True when spending `estimated_credits` is within budget."""
        if self.session_credit_budget is None:
            return True
        return self._spent + estimated_credits <= self.session_credit_budget

    def record_spend(self, credits: float) -> None:
        """Accumulate actual spend after a successful award."""
        self._spent += credits

    @property
    def remaining(self) -> float | None:
        """Remaining budget, or None if unlimited."""
        if self.session_credit_budget is None:
            return None
        return max(0.0, self.session_credit_budget - self._spent)
