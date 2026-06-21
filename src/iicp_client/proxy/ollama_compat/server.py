# SPDX-License-Identifier: Apache-2.0
"""Ollama-compatible routes added to the proxy's FastAPI application.

WHY add_ollama_routes rather than a separate sub-app: the proxy's OpenAI-compat
sub-app is already mounted at "/" in main.py. Mounting a second sub-app at "/"
would shadow it. Adding routes directly to the same app avoids the collision and
reuses all shared state (fallback_chain, directory, selector, peer_cache) set by
the main app's lifespan.

WHY /api/tags returns a static list: Ollama clients call /api/tags on startup to
enumerate available models. IICP node selection is intent-based (not model-based)
so there is no real model list. The static entry "iicp" signals that the proxy is
present and prompts the client to continue. Named-model routing (e.g. "llama3") is
forwarded transparently as-is in the IICP payload — adapters handle model selection.

WHY fake-streaming via a single terminal NDJSON chunk: IICP task execution is a
synchronous request-response cycle. True token-by-token streaming requires adapter-side
SSE (Phase 4 stretch goal). Fake streaming sends the complete response as one NDJSON
line with done=true — this satisfies Open WebUI, Continue.dev, and other clients that
default to stream=True. Clients iterating over chunks receive the full reply in the
first chunk and immediately see done=true to terminate the loop.

Spec: spec/iicp-core.md §3. ADR: ADR-001, ADR-005. Issues: #278, #280.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from iicp_client.proxy.cip.dispatch import (
    CIPInsufficientCredits,
    CIPNoEligibleWorkers,
    compute_cip_envelope,
    resolve_consumer_balance,
)
from iicp_client.proxy.ollama_compat.translator import (
    to_iicp_task,
    to_ollama_generate_response,
    to_ollama_response,
)
from iicp_client.proxy.otel_tracer import proxy_route_span

logger = logging.getLogger(__name__)

_OLLAMA_VERSION = "0.1.0"


def _ndjson_stream(data: dict[str, Any]) -> Iterator[bytes]:
    """Single terminal NDJSON chunk — full response with done=true.

    Ollama streaming clients read line-by-line; the first (and only) line they
    receive carries the complete content and done=true, so the loop terminates.
    """
    yield (json.dumps(data, ensure_ascii=False) + "\n").encode()


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": code + ": " + message},
    )


async def _execute_iicp(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Run an IICP task through the proxy routing stack."""
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

    return response


def add_ollama_routes(app: FastAPI) -> None:
    """Register Ollama-compatible routes on an existing FastAPI app."""

    @app.get("/api/version", include_in_schema=False)
    async def api_version() -> JSONResponse:
        return JSONResponse({"version": _OLLAMA_VERSION})

    @app.get("/api/tags", include_in_schema=False)
    async def api_tags() -> JSONResponse:
        """Static model list — IICP proxy surfaces as a single 'iicp' model."""
        return JSONResponse({
            "models": [
                {
                    "name": "iicp",
                    "model": "iicp",
                    "modified_at": "2026-01-01T00:00:00Z",
                    "size": 0,
                    "digest": "",
                    "details": {
                        "format": "iicp",
                        "family": "iicp",
                        "parameter_size": "varies",
                        "quantization_level": "varies",
                    },
                }
            ]
        })

    @app.post("/api/chat", include_in_schema=False)
    async def api_chat(request: Request) -> Response:
        """Ollama /api/chat — maps to IICP CALL with message list.

        stream=True (Ollama default): returns application/x-ndjson with one terminal chunk.
        stream=False: returns application/json.
        Errors always return application/json regardless of stream value.
        """
        body: dict[str, Any] = await request.json()
        stream: bool = body.get("stream", True)  # Ollama protocol default is True
        try:
            response = await _execute_iicp(request, body)
        except CIPInsufficientCredits as exc:
            return _error_response(402, exc.error_code, "Insufficient S-Credit balance for remote dispatch")
        except CIPNoEligibleWorkers as exc:
            return _error_response(503, exc.error_code, "No eligible CIP workers available for remote dispatch")

        if response.get("status") == "error":
            err = response.get("error", {})
            return _error_response(502, err.get("code", "proxy_error"), "Upstream error")

        model = body.get("model", "iicp")
        data = to_ollama_response(response, model)
        if stream:
            return StreamingResponse(_ndjson_stream(data), media_type="application/x-ndjson")
        return JSONResponse(content=data)

    @app.post("/api/generate", include_in_schema=False)
    async def api_generate(request: Request) -> Response:
        """Ollama /api/generate — single prompt maps to a user message IICP CALL.

        stream=True (Ollama default): returns application/x-ndjson with one terminal chunk.
        stream=False: returns application/json.
        Errors always return application/json regardless of stream value.
        """
        body: dict[str, Any] = await request.json()
        stream: bool = body.get("stream", True)  # Ollama protocol default is True
        try:
            response = await _execute_iicp(request, body)
        except CIPInsufficientCredits as exc:
            return _error_response(402, exc.error_code, "Insufficient S-Credit balance for remote dispatch")
        except CIPNoEligibleWorkers as exc:
            return _error_response(503, exc.error_code, "No eligible CIP workers available for remote dispatch")

        if response.get("status") == "error":
            err = response.get("error", {})
            return _error_response(502, err.get("code", "proxy_error"), "Upstream error")

        model = body.get("model", "iicp")
        data = to_ollama_generate_response(response, model)
        if stream:
            return StreamingResponse(_ndjson_stream(data), media_type="application/x-ndjson")
        return JSONResponse(content=data)
