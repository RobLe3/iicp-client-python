"""Tests for IicpModelProvider — CIP-PL1 proxy plugin (#82)."""
from __future__ import annotations

import pytest

from iicp_client.proxy.config import ProxyConfig
from iicp_client.proxy.plugin.iicp_provider import IicpModelProvider


class _MockDirectory:
    def __init__(self, nodes: list):
        self._nodes = nodes

    async def discover(self, intent: str) -> list:
        return self._nodes


class _MockSelector:
    def select(self, nodes, **kwargs):
        return nodes


class _MockChain:
    def __init__(self, response: dict):
        self._response = response

    async def execute(self, nodes, task_id, intent, payload, timeout_ms):
        return self._response


def _make_provider(nodes=None, response=None):
    response = response or {
        "task_id": "test-id",
        "status": "success",
        "result": {
            "choices": [{"message": {"role": "assistant", "content": "Hello!"}}],
            "usage": {"total_tokens": 10},
        },
    }
    return IicpModelProvider(
        directory=_MockDirectory(nodes or [{"node_id": "n1", "available": True}]),
        selector=_MockSelector(),
        fallback_chain=_MockChain(response),
        model_name="iicp",
    )


@pytest.mark.anyio
async def test_plugin_translates_openai_request_to_iicp_task():
    """chat_completions() must accept OpenAI body and return an OpenAI-shaped response."""
    provider = _make_provider()
    body = {
        "model": "iicp",
        "messages": [{"role": "user", "content": "Hi"}],
    }
    result = await provider.chat_completions(body)
    assert result["object"] == "chat.completion"
    assert result["choices"][0]["message"]["content"] == "Hello!"


@pytest.mark.anyio
async def test_plugin_uses_body_model_name_in_response():
    """chat_completions() must use body.model as the model field in the response."""
    provider = _make_provider()
    result = await provider.chat_completions({"model": "my-model", "messages": []})
    assert result["model"] == "my-model"


@pytest.mark.anyio
async def test_plugin_falls_back_to_default_model_name_when_absent():
    """When body has no model field, provider's model_name is used."""
    provider = _make_provider()
    result = await provider.chat_completions({"messages": []})
    assert result["model"] == "iicp"


@pytest.mark.anyio
async def test_plugin_handles_directory_failure_gracefully():
    """chat_completions() must not raise when directory discover fails — routes with empty list."""
    class _FailDir:
        async def discover(self, intent):
            raise ConnectionError("directory down")

    provider = IicpModelProvider(
        directory=_FailDir(),
        selector=_MockSelector(),
        fallback_chain=_MockChain({
            "task_id": "x",
            "status": "success",
            "result": {"choices": [], "usage": {}},
        }),
        model_name="iicp",
    )
    result = await provider.chat_completions({"messages": []})
    assert result["object"] == "chat.completion"


def test_plugin_config_defaults_to_disabled():
    """Plugin must be disabled by default (CIP-PL1 opt-in)."""
    cfg = ProxyConfig()
    assert cfg.plugin_enabled is False


def test_plugin_config_listen_port_default():
    """Plugin default listen port is 9482 — reserved IICP proxy band, distinct from
    the main proxy (9483) and the Ollama backend (11434) so all three coexist (#475)."""
    cfg = ProxyConfig()
    assert cfg.plugin_listen_port == 9482
