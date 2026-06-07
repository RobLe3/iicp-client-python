# SPDX-License-Identifier: Apache-2.0
"""OpenAI-compatible FastAPI endpoint for the proxy."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from iicp_client.proxy.cip.dispatch import (
    REALTIME_QOS,
    CIPInsufficientCredits,
    CIPNoEligibleWorkers,
    compute_cip_envelope,
    resolve_consumer_balance,
)
from iicp_client.proxy.openai_compat.translator import to_iicp_task, to_openai_response
from iicp_client.proxy.otel_tracer import proxy_route_span

logger = logging.getLogger(__name__)


def _billing_constraints(body: dict) -> dict:
    """Extract ADR-019 pricing/quality constraints from the billing block."""
    billing = body.get("billing") or {}
    if not isinstance(billing, dict):
        return {}
    out: dict = {}
    for key in ("max_multiplier", "min_quality_score", "max_credits"):
        val = billing.get(key)
        if val is not None:
            try:
                out[key] = float(val)
            except (TypeError, ValueError):
                pass
    # billing.priority="premium" accepted for backward compat; not used for selection (ADR-019 §3)
    return out


async def _resolve_nodes(
    intent: str,
    peer_cache: Any,
    directory: Any,
    selector: Any,
    **constraints: Any,
) -> list[dict[str, Any]]:
    raw: list[dict[str, Any]] = []
    if peer_cache is not None:
        raw = await peer_cache.get_nodes(intent) or []
    if not raw:
        try:
            raw = await directory.discover(intent=intent)
            if peer_cache is not None:
                await peer_cache.fetch_and_cache(intent)
        except Exception as exc:
            logger.warning("Directory discover failed: %s", exc)
    return selector.select(raw, **constraints)


def _format_completion_response(response: dict, body: dict) -> JSONResponse:
    if response.get("status") == "error":
        err = response.get("error", {})
        return JSONResponse(
            status_code=502,
            content={
                "error": {"code": err.get("code", "proxy_error"), "message": "Upstream error"}
            },
        )
    model = body.get("model", "iicp")
    return JSONResponse(content=to_openai_response(response, model))


def create_compat_app() -> FastAPI:
    app = FastAPI(title="IICP Proxy — OpenAI Compat")

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> JSONResponse:
        body: dict[str, Any] = await request.json()
        fallback_chain = request.app.state.fallback_chain
        directory = request.app.state.directory
        selector = request.app.state.selector
        peer_cache = getattr(request.app.state, "peer_cache", None)
        aggregator = getattr(request.app.state, "aggregator", None)
        cip_config = getattr(request.app.state, "cip_config", None)
        session_tracker = getattr(request.app.state, "cip_budget_tracker", None)
        node_token = getattr(request.app.state, "node_token", None)

        task_id, intent, payload = to_iicp_task(body)
        timeout_ms = int(body.get("timeout_ms", 30000))
        qos: str | None = body.get("qos")

        billing = body.get("billing")
        if isinstance(billing, dict):
            payload = {**payload, "_billing": billing}

        constraints = _billing_constraints(body)

        with proxy_route_span(str(task_id), intent):
            nodes = await _resolve_nodes(intent, peer_cache, directory, selector, **constraints)
            consumer_balance = await resolve_consumer_balance(directory, node_token, cip_config)
            try:
                cip_envelope = compute_cip_envelope(
                    nodes, body, cip_config, str(task_id), qos, session_tracker,
                    consumer_balance=consumer_balance,
                )
            except CIPInsufficientCredits as exc:
                return JSONResponse(
                    status_code=402,
                    content={"error": {
                        "code": exc.error_code,
                        "message": "Insufficient S-Credit balance for remote dispatch",
                    }},
                )
            except CIPNoEligibleWorkers as exc:
                return JSONResponse(
                    status_code=503,
                    content={"error": {
                        "code": exc.error_code,
                        "message": "No eligible CIP workers available for remote dispatch",
                    }},
                )
            cip_block = body.get("cip") if isinstance(body.get("cip"), dict) else {}

            # Phase 3.4: parallel redundancy for realtime QoS
            if qos in REALTIME_QOS and aggregator is not None and len(nodes) > 1:
                response = await aggregator.execute(nodes, task_id, intent, payload, timeout_ms)
            else:
                response = await fallback_chain.execute(
                    nodes, task_id, intent, payload, timeout_ms,
                    cip_envelope=cip_envelope,
                    cip_policy=cip_block.get("policy", "best_of_n"),
                    cip_replicas=int(cip_block.get("replicas", 1)),
                    cip_quorum=cip_block.get("quorum"),
                )

        return _format_completion_response(response, body)

    return app
