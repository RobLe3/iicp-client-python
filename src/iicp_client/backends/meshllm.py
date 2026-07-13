# SPDX-License-Identifier: Apache-2.0
"""MeshLLM backend handler.

MeshLLM exposes a local OpenAI-compatible API at ``http://localhost:9337/v1``.
IICP deliberately uses only that HTTP boundary: MeshLLM's peer discovery,
topology and control plane remain private to its operator.

The stable MeshLLM profile serves chat only.  The upstream experimental
``model=mesh`` ensemble must be enabled explicitly by the CLI before this
handler is constructed.
"""

from __future__ import annotations

from typing import Any

from iicp_client.backends.base import TaskHandler, build_openai_dialect_handler

_CHAT_INTENT = "urn:iicp:intent:llm:chat:v1"


def meshllm_handler(
    *,
    base_url: str = "http://localhost:9337/v1",
    model: str | None = None,
    api_key: str = "",
    timeout_s: float = 30.0,
) -> TaskHandler:
    """Build a chat-only handler for MeshLLM's local OpenAI-compatible API."""
    openai_handler = build_openai_dialect_handler(
        engine="meshllm",
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout_s=timeout_s,
    )

    async def handler(task: dict[str, Any]) -> dict[str, Any]:
        if task.get("intent") != _CHAT_INTENT:
            return {
                "error_code": 400,
                "error_message": "MeshLLM stable backend supports llm:chat:v1 only",
            }
        return await openai_handler(task)

    return handler
