# SPDX-License-Identifier: Apache-2.0
"""Anthropic Messages API routes added to the proxy's FastAPI application.

WHY /v1/messages alongside /v1/chat/completions: Anthropic clients call POST /v1/messages.
OpenAI clients call POST /v1/chat/completions. Both paths exist under /v1/ with distinct
route names — no collision. The proxy registers both and routes IICP tasks identically
through the same fallback_chain.

WHY /v1/models is a static list: Anthropic SDKs call GET /v1/models on startup to verify
the base URL is valid. IICP proxy returns a minimal static entry so SDK initialization
succeeds. Actual model selection is intent-based on the mesh, not name-based.

WHY fake SSE streaming via six typed events: Anthropic SDK stream=True requires
text/event-stream with message_start, content_block_start, content_block_delta,
content_block_stop, message_delta, and message_stop events. IICP task execution is
synchronous (single request-response cycle), so we emit the full response as a single
content_block_delta event. SDK streaming iterators receive the complete text in the
first delta and immediately see message_stop, satisfying the protocol without token-level
granularity. True streaming requires adapter-side SSE (Phase 4 stretch goal, #280).

Spec: spec/iicp-core.md §3. ADR: ADR-001, ADR-005. Issues: #279, #280.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from iicp_client.proxy.anthropic_compat.translator import to_anthropic_response, to_iicp_task
from iicp_client.proxy.cip.dispatch import (
    CIPInsufficientCredits,
    CIPNoEligibleWorkers,
    compute_cip_envelope,
    resolve_consumer_balance,
)
from iicp_client.proxy.otel_tracer import proxy_route_span

logger = logging.getLogger(__name__)

def _sse_events(
    iicp_response: dict[str, Any],
    task_id: str,
    model: str,
) -> Iterator[bytes]:
    """Yield Anthropic SSE events for a complete IICP response.

    Emits the six required event types (message_start, content_block_start,
    content_block_delta, content_block_stop, message_delta, message_stop) with
    the full response text in a single content_block_delta. Anthropic SDK
    streaming iterators collect all deltas and reassemble them — receiving the
    full text in one delta is equivalent to receiving it word-by-word.
    """
    result = iicp_response.get("result") or {}
    choices: list[dict[str, Any]] = result.get("choices") or [{}]
    message = (choices[0].get("message") or {}) if choices else {}
    usage: dict[str, Any] = result.get("usage") or {}
    text = message.get("content", "")
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)

    def _event(name: str, data: dict[str, Any]) -> bytes:
        return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()

    yield _event("message_start", {
        "type": "message_start",
        "message": {
            "id": f"msg_{task_id or 'iicp'}",
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": 0},
        },
    })
    yield _event("content_block_start", {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    })
    yield _event("content_block_delta", {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": text},
    })
    yield _event("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield _event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    })
    yield _event("message_stop", {"type": "message_stop"})


# Static model list — Anthropic SDK calls GET /v1/models to validate base_url
_MODELS_RESPONSE = {
    "data": [
        {
            "id": "iicp",
            "object": "model",
            "created": 1700000000,
            "owned_by": "iicp",
        }
    ],
    "object": "list",
}


async def _execute_iicp(request: Request, body: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Run an IICP task through the proxy routing stack. Returns (response, str_task_id)."""
    fallback_chain = request.app.state.fallback_chain
    directory = request.app.state.directory
    selector = request.app.state.selector
    peer_cache = getattr(request.app.state, "peer_cache", None)
    cip_config = getattr(request.app.state, "cip_config", None)
    session_tracker = getattr(request.app.state, "cip_budget_tracker", None)
    node_token = getattr(request.app.state, "node_token", None)
    source_node_id = getattr(request.app.state, "node_id", None)

    task_id, intent, payload = to_iicp_task(body)
    timeout_ms = int(body.get("timeout_ms", 30000))

    raw: list[dict[str, Any]] = []
    with proxy_route_span(str(task_id), intent):
        if peer_cache is not None:
            raw = await peer_cache.get_nodes(intent) or []
        if not raw:
            try:
                raw = await directory.discover(intent=intent)
                if peer_cache is not None:
                    await peer_cache.fetch_and_cache(intent)
            except Exception as exc:
                logger.warning("Directory discover failed: %s", exc)

        nodes = selector.select(raw)
        consumer_balance = await resolve_consumer_balance(directory, node_token, cip_config)
        cip_envelope = compute_cip_envelope(
            nodes, body, cip_config, str(task_id),
            session_tracker=session_tracker, consumer_balance=consumer_balance,
        )
        cip_block = body.get("cip") if isinstance(body.get("cip"), dict) else {}
        response: dict[str, Any] = await fallback_chain.execute(
            nodes, task_id, intent, payload, timeout_ms,
            cip_envelope=cip_envelope,
            cip_policy=cip_block.get("policy", "best_of_n"),
            cip_replicas=int(cip_block.get("replicas", 1)),
            cip_quorum=cip_block.get("quorum"),
            source_node_id=source_node_id,
        )

    return response, str(task_id)


def add_anthropic_routes(app: FastAPI) -> None:
    """Register Anthropic Messages API-compatible routes on an existing FastAPI app."""

    @app.get("/v1/models", include_in_schema=False)
    async def list_models() -> JSONResponse:
        """Static model list — satisfies Anthropic SDK base_url validation."""
        return JSONResponse(_MODELS_RESPONSE)

    @app.post("/v1/messages", include_in_schema=False)
    async def messages(request: Request) -> Response:
        """Anthropic /v1/messages — translates to IICP CALL and returns Messages API shape.

        stream=True: returns text/event-stream with six typed SSE events.
        stream=False (Anthropic SDK default): returns application/json.
        Errors always return application/json regardless of stream value.
        """
        body: dict[str, Any] = await request.json()
        stream: bool = body.get("stream", False)  # Anthropic SDK default is False
        try:
            response, task_id = await _execute_iicp(request, body)
        except CIPInsufficientCredits as exc:
            return JSONResponse(
                status_code=402,
                content={
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": exc.error_code + ": Insufficient S-Credit balance for remote dispatch",
                    },
                },
            )
        except CIPNoEligibleWorkers as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": exc.error_code + ": No eligible CIP workers available for remote dispatch",
                    },
                },
            )

        if response.get("status") == "error":
            err = response.get("error", {})
            return JSONResponse(
                status_code=502,
                content={
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": err.get("code", "proxy_error") + ": Upstream error",
                    },
                },
            )

        model = body.get("model", "iicp")
        if stream:
            return StreamingResponse(
                _sse_events(response, task_id, model),
                media_type="text/event-stream",
            )
        return JSONResponse(content=to_anthropic_response(response, model, task_id))
