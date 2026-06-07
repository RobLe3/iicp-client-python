# SPDX-License-Identifier: Apache-2.0
"""Task router — single-node dispatch with retry + circuit-breaker enforcement.

The proxy's routing layer composes three small classes:
    NodeSelector  → picks (in directory score order)  →
        FallbackChain → walks candidates in order      →
            TaskRouter   → submits one node, with retry + circuit breaker

This module is the innermost step: take ONE node and ONE task, perform the
submit with retries, and update the circuit-breaker book. Higher layers
(FallbackChain) react to the exceptions raised here to move to the next
candidate or surface a structured "all nodes exhausted" error.

Cross-references:
    - ADR-001 — proxy is the Client plane; routing decisions live here, not in the directory
    - ADR-008 — directory score ordering is authoritative; the proxy preserves it
    - spec/iicp-core.md §10 — retry/idempotency semantics for client implementations
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from iicp_client.proxy.clients.node import NodeClient, _is_ssrf_safe
from iicp_client.proxy.routing.circuit_breaker import CircuitBreaker, CircuitOpenError
from iicp_client.proxy.routing.retry import RetryManager

logger = logging.getLogger(__name__)


class TaskRouter:
    """Dispatch a task to one node, respecting retry + circuit-breaker policy.

    Invariants:
      - `route()` either returns a successful task result OR raises.
      - Every success path records a circuit-breaker success.
      - Every non-CircuitOpenError exception records a circuit-breaker failure.
      - `CircuitOpenError` is re-raised unchanged so FallbackChain can skip
        cleanly without polluting the breaker book a second time.
    """

    def __init__(
        self,
        node_token: str,
        retry: RetryManager,
        circuit: CircuitBreaker,
    ) -> None:
        self._token = node_token
        self._retry = retry
        self._circuit = circuit

    async def route(
        self,
        node: dict[str, Any],
        task_id: UUID,
        intent: str,
        payload: dict[str, Any],
        timeout_ms: int,
        cip_envelope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Submit one task to one node; raise on any failure mode.

        The pre-check (`circuit.check`) fast-fails without an HTTP attempt
        when the breaker is open — saving ~timeout_ms of wasted wall-clock
        when we already know the node is unreachable.

        Side effects: updates the circuit-breaker counters via record_success
        / record_failure. CircuitOpenError bypasses record_failure on purpose
        — the breaker is already open.
        """
        node_id = node["node_id"]
        endpoint = node["endpoint"]
        transport_endpoint = node.get("transport_endpoint")  # spec v0.7.0 dual-endpoint fallback

        if not _is_ssrf_safe(endpoint):
            logger.warning(
                "Router: SSRF guard — skipping node %s with non-routable endpoint %s",
                node_id[:8] if len(node_id) > 8 else node_id,
                endpoint,
            )
            raise ValueError(
                f"Node endpoint '{endpoint}' is not publicly routable (SSRF guard)"
            )

        self._circuit.check(node_id)

        client = NodeClient(endpoint, self._token, transport_endpoint=transport_endpoint)

        async def attempt() -> dict[str, Any]:
            return await client.submit_task(task_id, intent, payload, timeout_ms, cip_envelope=cip_envelope)

        try:
            result = await self._retry.run(attempt)
            self._circuit.record_success(node_id)
            return result
        except (CircuitOpenError, Exception) as exc:
            if not isinstance(exc, CircuitOpenError):
                self._circuit.record_failure(node_id)
            raise
