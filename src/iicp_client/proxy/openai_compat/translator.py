# SPDX-License-Identifier: Apache-2.0
"""Translate between OpenAI-compat and IICP task formats."""
from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4


def to_iicp_task(
    openai_body: dict[str, Any],
    timeout_ms: int = 30000,
) -> tuple[UUID, str, dict[str, Any]]:
    """Return (task_id, intent, payload) from an OpenAI chat completions request."""
    task_id = uuid4()
    intent = "urn:iicp:intent:llm:chat:v1"
    payload = {
        "messages": openai_body.get("messages", []),
        "model": openai_body.get("model"),
        "temperature": openai_body.get("temperature"),
        "max_tokens": openai_body.get("max_tokens"),
        "stream": openai_body.get("stream", False),
    }
    return task_id, intent, payload


def to_openai_response(iicp_response: dict[str, Any], model: str = "iicp") -> dict[str, Any]:
    """Translate an IICP task response to OpenAI chat completions format."""
    result = iicp_response.get("result") or {}
    choices = result.get("choices", [])
    usage = result.get("usage", {})

    return {
        "id": f"chatcmpl-{iicp_response.get('task_id', 'unknown')}",
        "object": "chat.completion",
        "model": model,
        "choices": choices,
        "usage": usage,
    }
