# SPDX-License-Identifier: Apache-2.0
"""Translate between Anthropic Messages API and IICP task formats.

WHY Anthropic-compat: The Anthropic Python/TypeScript SDKs (client.messages.create)
are used by a growing segment of AI developers post-Claude-4. Adding a single
base_url override routes those apps through the IICP mesh without further code changes.

WHY system is extracted separately: Anthropic places the system prompt as a top-level
string, not inside the messages list. IICP task format uses OpenAI-style messages.
We convert the Anthropic system prompt to a {"role": "system", "content": "..."} entry
at the front of the messages list so adapter nodes receive a standard message array.

WHY content blocks are flattened to a string: Anthropic supports typed content blocks
([{"type": "text", "text": "..."}]) instead of a plain string. IICP task messages use
plain string content. Non-text block types (tool_use, tool_result, image) are omitted
with a placeholder — the proxy is a text completion gateway, not a multimodal router.

WHY max_tokens is required (but treated as optional here): Anthropic requires max_tokens;
IICP does not mandate it. We forward it as-is — adapter nodes apply their own token cap
if max_tokens is absent. Rejecting requests without max_tokens would break the Anthropic
SDK default path unnecessarily.

Spec: spec/iicp-core.md §3 (CALL/RESPONSE). ADR: ADR-001, ADR-005. Issue: #279.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4


def _flatten_content(content: Any) -> str:
    """Flatten Anthropic typed content blocks or plain strings to a plain string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return " ".join(parts)
    return ""


def to_iicp_task(body: dict[str, Any]) -> tuple[UUID, str, dict[str, Any]]:
    """Return (task_id, intent, payload) from an Anthropic /v1/messages body.

    Handles system prompt extraction, content block flattening, and max_tokens forwarding.
    """
    task_id = uuid4()
    intent = "urn:iicp:intent:llm:chat:v1"

    messages: list[dict[str, Any]] = []

    # Anthropic top-level system prompt → prepend as system message
    system = body.get("system")
    if system:
        messages.append({"role": "system", "content": _flatten_content(system)})

    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user")
        content = _flatten_content(msg.get("content", ""))
        messages.append({"role": role, "content": content})

    payload: dict[str, Any] = {
        "messages": messages,
        "model": body.get("model"),
        "max_tokens": body.get("max_tokens"),
        "temperature": body.get("temperature"),
        "stream": body.get("stream", False),
    }
    return task_id, intent, payload


def to_anthropic_response(
    iicp_response: dict[str, Any],
    model: str = "iicp",
    task_id: str = "",
) -> dict[str, Any]:
    """Translate an IICP task response to Anthropic Messages API response format."""
    result = iicp_response.get("result") or {}
    choices: list[dict[str, Any]] = result.get("choices") or [{}]
    message = (choices[0].get("message") or {}) if choices else {}
    usage: dict[str, Any] = result.get("usage") or {}

    return {
        "id": f"msg_{task_id or 'iicp'}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [
            {"type": "text", "text": message.get("content", "")},
        ],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }
