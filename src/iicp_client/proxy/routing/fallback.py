# SPDX-License-Identifier: Apache-2.0
"""Fallback chain — walk ranked candidates until one succeeds or all exhaust.

Where TaskRouter handles "one node, retry policy" and CircuitBreaker handles
"is this node viable right now", FallbackChain handles "we have N candidates,
try them in directory-score order until one works or we run out".

Cross-references:
    - ADR-008 — directory-supplied score order is preserved (NodeSelector enforces this)
    - project/RELIABILITY.md — fallback contract: exhaustion returns a structured
      error envelope, never a raw exception (project rule #6)
    - spec/iicp-core.md §10 — client retry / fallback semantics
    - spec/iicp-core.md §7 — IICP-E033 (empty discover) vs no_available_node (runtime exhaustion)
"""
from __future__ import annotations

import asyncio
import hashlib
import json as _json
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from iicp_client.proxy.routing.circuit_breaker import CircuitOpenError
from iicp_client.proxy.routing.router import TaskRouter

if TYPE_CHECKING:
    from iicp_client.proxy.cip.coordinator import ReplayCache

logger = logging.getLogger(__name__)


async def _fire_award(
    *,
    raw_receipt: dict[str, Any],
    expected_session_key: str | None,
    replay_cache: ReplayCache,
    directory_url: str,
    node_token: str,
) -> None:
    """TC-9d: best-effort background credit award — never raises."""
    try:
        from iicp_client.proxy.cip.coordinator import CIPWorkerReceipt, submit_award
        receipt = CIPWorkerReceipt.from_dict(raw_receipt)
        await submit_award(
            receipt=receipt,
            expected_session_key=expected_session_key,
            replay_cache=replay_cache,
            directory_url=directory_url,
            node_token=node_token,
        )
    except Exception:
        pass


def _verify_receipt_hash(response: dict[str, Any], node_id: str) -> str | None:
    """Return None if receipt hash verification passes, or an error string on failure.

    Only called when response contains a non-None cip_receipt. Hash covers the canonical
    JSON encoding of the result field (sorted keys, no whitespace) per TC-9c §10.3.
    """
    raw_receipt = response.get("cip_receipt")
    if raw_receipt is None:
        return None  # no receipt — no hash to check
    receipt_hash = raw_receipt.get("response_hash") if isinstance(raw_receipt, dict) else None
    if receipt_hash is None:
        logger.warning(
            "node %s CIPWorkerReceipt missing response_hash — discarding (TC-9c §10.3)",
            node_id,
        )
        return "response_hash_missing"
    result_field = response.get("result")
    if result_field is None:
        actual_hash = hashlib.sha256(b"").hexdigest()
    else:
        canonical = _json.dumps(result_field, sort_keys=True, separators=(",", ":")).encode("utf-8")
        actual_hash = hashlib.sha256(canonical).hexdigest()
    if actual_hash != receipt_hash:
        logger.warning(
            "node %s response_hash mismatch — discarding response (TC-9c §10.3.2)",
            node_id,
        )
        return "response_hash_mismatch"
    return None


def _build_cip_aggregation(
    policy: str,
    replicas_dispatched: int,
    replicas_responded: int,
    selected_worker_id: str | None,
    quorum: int | None,
) -> dict[str, Any]:
    """Build trace.cip_aggregation dict for coordinator RESPONSE (S.12 §4.3 CIP-AGG-01)."""
    agg: dict[str, Any] = {
        "policy": policy,
        "replicas_dispatched": replicas_dispatched,
        "replicas_responded": replicas_responded,
        "selected_worker_id": selected_worker_id,
    }
    if policy == "majority_vote":
        # majority_vote MUST include cip_vote_count and cip_quorum_threshold (S.12 §4.3)
        agg["cip_vote_count"] = replicas_responded
        agg["cip_quorum_threshold"] = quorum if quorum is not None else max(1, (replicas_dispatched // 2) + 1)
    elif quorum is not None:
        agg["cip_quorum_threshold"] = quorum
    return agg


class FallbackChain:
    """Serial fallback — for parallel redundancy see ResultAggregator."""

    def __init__(
        self,
        router: TaskRouter,
        replay_cache: ReplayCache | None = None,
        directory_url: str = "",
        node_token: str = "",
    ) -> None:
        self._router = router
        self._replay_cache = replay_cache
        self._directory_url = directory_url
        self._node_token = node_token

    def _schedule_award(self, response: dict[str, Any], cip_envelope: dict[str, Any]) -> None:
        """TC-9d: fire best-effort background credit award when reply carries cip_receipt."""
        if self._replay_cache is None or not self._directory_url:
            return
        raw_receipt = response.get("cip_receipt")
        if raw_receipt:
            asyncio.create_task(_fire_award(
                raw_receipt=raw_receipt,
                expected_session_key=cip_envelope.get("cip_session_key"),
                replay_cache=self._replay_cache,
                directory_url=self._directory_url,
                node_token=self._node_token,
            ))

    async def execute(
        self,
        nodes: list[dict[str, Any]],
        task_id: UUID,
        intent: str,
        payload: dict[str, Any],
        timeout_ms: int,
        cip_envelope: dict[str, Any] | None = None,
        cip_policy: str = "best_of_n",
        cip_replicas: int = 1,
        cip_quorum: int | None = None,
    ) -> dict[str, Any]:
        """Try `nodes` in order; return first success, or an exhaustion error.

        Empty input → IICP-E033 (specific spec code, iter-321 WQ-030): discover
        returned 0 candidates after filtering. Distinct from runtime exhaustion
        (no_available_node) — operator next-step is to verify intent URN or
        wait for providers, not retry the same call.

        CircuitOpenError is logged at DEBUG (it's an expected fast-skip), all
        other exceptions at WARN (they reflect real failures). The final
        error response carries `error.code = no_available_node` plus the
        exception class name from the last failed attempt — enough for
        debugging without leaking exception text to clients.

        Same task_id is reused across fallback attempts on purpose: the
        adapter is idempotent (ADR-010), so a duplicate that races with a
        successful retry returns 409 rather than executing twice.

        cip_policy/cip_replicas/cip_quorum are only used when cip_envelope is
        not None — they drive the trace.cip_aggregation object in the coordinator
        RESPONSE (S.12 §4.3, CIP-AGG-01). FallbackChain performs serial dispatch
        so replicas_responded is always 0 or 1.
        """
        # WQ-030: distinguish "no nodes ever returned from discover" (IICP-E033)
        # from "all returned nodes failed during routing" (no_available_node).
        # The first is an intent / capability mismatch; the second is a runtime
        # failure of otherwise-eligible providers.
        if not nodes:
            err: dict[str, Any] = {
                "task_id": str(task_id),
                "status": "error",
                "result": None,
                "metrics": {"latency_ms": 0},
                "error": {
                    "code": "IICP-E033",
                    "message": (
                        f"No nodes serve intent '{intent}' — directory was reachable and returned 0 candidates "
                        "after filtering. Verify your intent URN matches a registered capability; check the "
                        "/nodes page on iicp.network for current providers; or wait for new providers to join."
                    ),
                },
            }
            if cip_envelope is not None:
                err["trace"] = {"cip_aggregation": _build_cip_aggregation(
                    cip_policy, cip_replicas, 0, None, cip_quorum,
                )}
            return err

        last_error: str = "no_nodes_available"

        for node in nodes:
            node_id = node.get("node_id", "unknown")
            try:
                response = await self._router.route(
                    node, task_id, intent, payload, timeout_ms, cip_envelope=cip_envelope
                )
                # CIP-BIND-01: S.12 §10.4 MUST — coordinator MUST discard worker responses
                # whose trace.cip_session_key does not match the dispatched key.
                # Missing key is treated as mismatch (worker did not echo it).
                if cip_envelope is not None:
                    expected_key = cip_envelope.get("cip_session_key")
                    if expected_key is not None:
                        actual_key = (response.get("trace") or {}).get("cip_session_key")
                        if actual_key != expected_key:
                            logger.warning(
                                "node %s returned mismatched cip_session_key — discarding response (S.12 §10.4)",
                                node_id,
                            )
                            last_error = "cip_session_key_mismatch"
                            continue

                # TC-9c §10.3: verify response_hash when a cip_receipt is present.
                if cip_envelope is not None:
                    hash_err = _verify_receipt_hash(response, node_id)
                    if hash_err is not None:
                        last_error = hash_err
                        continue

                # S.12 §4.3 CIP-AGG-01: coordinator MUST include cip_aggregation in RESPONSE trace
                if cip_envelope is not None:
                    trace = dict(response.get("trace") or {})
                    trace["cip_aggregation"] = _build_cip_aggregation(
                        cip_policy, cip_replicas, 1, node.get("node_id"), cip_quorum,
                    )
                    response = {**response, "trace": trace}
                    # TC-9d: CIP-CR1-WIRE — submit credit award (§7 ADR-012)
                    self._schedule_award(response, cip_envelope)
                return response
            except CircuitOpenError:
                logger.debug("skipping node %s — circuit open", node_id)
                last_error = "circuit_open"
            except Exception as exc:
                logger.warning("node %s failed: %s", node_id, type(exc).__name__)
                last_error = type(exc).__name__

        exhausted: dict[str, Any] = {
            "task_id": str(task_id),
            "status": "error",
            "result": None,
            "metrics": {"latency_ms": 0},
            "error": {
                "code": "no_available_node",
                "message": f"All nodes exhausted. Last error: {last_error}",
            },
        }
        if cip_envelope is not None:
            exhausted["trace"] = {"cip_aggregation": _build_cip_aggregation(
                cip_policy, cip_replicas, 0, None, cip_quorum,
            )}
        return exhausted
