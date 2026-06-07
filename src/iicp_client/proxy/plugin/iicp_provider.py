# SPDX-License-Identifier: Apache-2.0
"""IicpModelProvider — OpenAI-compatible model-provider that routes via the IICP mesh.

Host assistants (e.g. OpenClaw, Moltbot) configure IICP as a model endpoint:

  [plugin.model_provider]
  enabled = true
  listen_port = 11434   # Ollama-compatible
  model_name = "iicp"

The provider translates OpenAI chat requests into IICP tasks, routes them through
the configured fallback chain, and returns OpenAI-compatible responses.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from iicp_client.proxy.openai_compat.translator import to_openai_response

logger = logging.getLogger(__name__)


class IicpModelProvider:
    """Drop-in OpenAI-compatible model provider that routes via the IICP mesh (CIP-PL1).

    Stateless request handler — dependencies injected at construction time so the
    same instance can serve concurrent requests without re-initialising routing.
    """

    def __init__(
        self,
        directory: Any,
        selector: Any,
        fallback_chain: Any,
        model_name: str = "iicp",
    ) -> None:
        self._directory = directory
        self._selector = selector
        self._fallback_chain = fallback_chain
        self._model_name = model_name

    async def chat_completions(
        self,
        body: dict[str, Any],
        timeout_ms: int = 30000,
    ) -> dict[str, Any]:
        """Route an OpenAI chat completions request through the IICP mesh.

        Returns an OpenAI-compatible response dict.
        Raises if no nodes are available or all upstreams fail.
        """
        task_id = uuid4()
        intent = "urn:iicp:intent:llm:chat:v1"
        payload: dict[str, Any] = {
            "messages": body.get("messages", []),
            "model": body.get("model"),
            "temperature": body.get("temperature"),
            "max_tokens": body.get("max_tokens"),
        }

        try:
            raw_nodes = await self._directory.discover(intent=intent)
        except Exception as exc:
            logger.warning("IicpModelProvider: directory discover failed: %s", exc)
            raw_nodes = []

        nodes = self._selector.select(raw_nodes)

        response = await self._fallback_chain.execute(
            nodes, task_id, intent, payload, timeout_ms
        )

        model = body.get("model") or self._model_name
        return to_openai_response(response, model)
