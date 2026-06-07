# SPDX-License-Identifier: Apache-2.0
"""Translate between Ollama API and IICP task formats.

WHY Ollama-compat at all: Ollama exposes /api/chat and /api/generate at port 11434.
IICP proxy also defaults to port 11434. Any tool built against the Ollama API (Open
WebUI, Continue.dev, LobeChat, Jan, aider, Obsidian AI, etc.) works against IICP
proxy zero-config. This is the largest adoption surface of any single inbound adapter.

WHY /api/generate maps to messages (not a raw completion endpoint): IICP task shape is
message-based (spec §3 CALL format). A single prompt is wrapped as a user message so
both paths converge on the same IICP wire format. The adapter node handles prompting.

WHY options.num_predict maps to max_tokens: Ollama's num_predict is the Ollama name
for what OpenAI calls max_tokens. Both cap generation length in tokens.

Spec: spec/iicp-core.md §3 (CALL/RESPONSE). ADR: ADR-001 (Client Plane), ADR-005.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4


def to_iicp_task(body: dict[str, Any]) -> tuple[UUID, str, dict[str, Any]]:
    """Return (task_id, intent, payload) from an Ollama /api/chat or /api/generate body.

    /api/chat supplies a messages list; /api/generate supplies a single prompt string.
    Both map to the same IICP intent (urn:iicp:intent:llm:chat:v1).
    """
    task_id = uuid4()
    intent = "urn:iicp:intent:llm:chat:v1"

    # /api/generate: single prompt string → wrap as a user message
    messages: list[dict[str, Any]] = body.get("messages") or [
        {"role": "user", "content": body.get("prompt", "")}
    ]

    raw_opts = body.get("options")
    options: dict[str, Any] = raw_opts if isinstance(raw_opts, dict) else {}

    payload: dict[str, Any] = {
        "messages": messages,
        "model": body.get("model"),
        "temperature": options.get("temperature"),
        "max_tokens": options.get("num_predict"),
        "stream": body.get("stream", False),
    }
    return task_id, intent, payload


def to_ollama_response(
    iicp_response: dict[str, Any],
    model: str = "iicp",
) -> dict[str, Any]:
    """Translate an IICP task response to Ollama /api/chat response format."""
    result = iicp_response.get("result") or {}
    choices: list[dict[str, Any]] = result.get("choices") or [{}]
    message = (choices[0].get("message") or {}) if choices else {}
    usage: dict[str, Any] = result.get("usage") or {}

    return {
        "model": model,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "message": {
            "role": message.get("role", "assistant"),
            "content": message.get("content", ""),
        },
        "done": True,
        "done_reason": "stop",
        "total_duration": 0,
        "eval_count": usage.get("completion_tokens", 0),
        "prompt_eval_count": usage.get("prompt_tokens", 0),
    }


def to_ollama_generate_response(
    iicp_response: dict[str, Any],
    model: str = "iicp",
) -> dict[str, Any]:
    """Translate an IICP response to Ollama /api/generate format (response string field)."""
    result = iicp_response.get("result") or {}
    choices: list[dict[str, Any]] = result.get("choices") or [{}]
    message = (choices[0].get("message") or {}) if choices else {}
    usage: dict[str, Any] = result.get("usage") or {}

    return {
        "model": model,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "response": message.get("content", ""),
        "done": True,
        "done_reason": "stop",
        "total_duration": 0,
        "eval_count": usage.get("completion_tokens", 0),
        "prompt_eval_count": usage.get("prompt_tokens", 0),
    }
