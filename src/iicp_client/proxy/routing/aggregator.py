# SPDX-License-Identifier: Apache-2.0
"""ResultAggregator — Phase-3 parallel redundancy mode for high-priority tasks.

Where FallbackChain walks candidates serially (lowest cost, highest latency
under failure), ResultAggregator fires the same task at up to `fan_out` nodes
simultaneously and returns the first success. Trade-off: ~fan_out× the
backend load per successful task, but ~1× the median-node latency even when
one or two nodes are slow/failed.

Use when:
    - QoS = interactive AND tail-latency matters more than backend cost
    - The intent is idempotent (safe to "waste" the slower responses)

Cross-references:
    - ADR-010 — idempotency: required for safe redundant dispatch
    - spec/iicp-core.md §4 — QoS hints inform when to switch from fallback
    - spec/iicp-core.md §7 — IICP-E033 (empty discover) vs no_available_node
"""
from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from iicp_client.proxy.routing.router import TaskRouter

logger = logging.getLogger(__name__)


class ResultAggregator:
    """Parallel redundancy mode (Phase 3.4).

    Sends the same task to up to `fan_out` nodes simultaneously.
    Returns the first successful response and cancels the remaining futures.
    Falls back to the first error response if all fail.
    """

    def __init__(self, router: TaskRouter, fan_out: int = 3) -> None:
        self._router = router
        self._fan_out = fan_out

    async def execute(
        self,
        nodes: list[dict],
        task_id: UUID,
        intent: str,
        payload: dict,
        timeout_ms: int,
    ) -> dict:
        """Race up to `fan_out` nodes; cancel losers as soon as one wins.

        Empty input → IICP-E033 (WQ-030, spec/iicp-core.md §7): discover returned
        0 candidates after filtering. Distinct from generic no_available_node —
        operator next-step is to verify intent URN or wait, not retry.

        First-success cancellation matters for backend cost: without it, the
        slower nodes still finish (and bill the operator). `p.cancel()` on the
        pending futures interrupts the awaited NodeClient call cleanly.

        If every dispatched node errors, return the first observed error
        response — preserves the structured envelope shape and gives the
        client a meaningful failure mode rather than a generic timeout.
        """
        # WQ-030: IICP-E033 = "no nodes serve this intent" (specific, actionable).
        # Distinct from "no_available_node" which now means "all returned nodes failed
        # during routing" (a runtime issue, not a discovery issue).
        if not nodes:
            return {
                "task_id": str(task_id),
                "status": "error",
                "result": None,
                "metrics": {"latency_ms": 0},
                "error": {
                    "code": "IICP-E033",
                    "message": (
                        f"No nodes serve intent '{intent}' — directory was reachable and returned 0 candidates "
                        "after filtering. Verify intent URN, check /nodes for current providers, or wait."
                    ),
                },
            }

        targets = nodes[: self._fan_out]
        tasks = [
            asyncio.create_task(
                self._router.route(node, task_id, intent, payload, timeout_ms),
                name=f"redundant-{node.get('node_id', '')[:8]}",
            )
            for node in targets
        ]

        first_error: dict | None = None
        pending = set(tasks)

        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for fut in done:
                try:
                    result = fut.result()
                    if result.get("status") == "success":
                        # Cancel remaining in-flight requests
                        for p in pending:
                            p.cancel()
                        return result
                    if first_error is None:
                        first_error = result
                except Exception as exc:
                    logger.debug("Redundant task failed: %s", exc)

        return first_error or {
            "task_id": str(task_id),
            "status": "error",
            "result": None,
            "metrics": {"latency_ms": 0},
            "error": {"code": "all_nodes_failed", "message": "All redundant nodes failed"},
        }
