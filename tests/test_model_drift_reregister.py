# SPDX-License-Identifier: Apache-2.0
"""Behavior tests — model-list drift re-registration (#494).

These tests fail if _maybe_reregister_on_model_drift() is removed or its
re-registration logic is broken. They are intentionally narrow: each verifies
exactly one observable behavior per the #494 acceptance criteria.
"""
from __future__ import annotations

import pytest
import respx
from httpx import Response

from iicp_client.node import IicpNode, NodeConfig


def _cfg(**kw) -> NodeConfig:
    defaults = dict(
        node_id="drift-node-1",
        endpoint="http://node.local:8080",
        intent="urn:iicp:intent:llm:chat:v1",
        model="phi3:mini",
        capabilities=["llama3.2:1b"],
        directory_url="http://mock-dir",
        backend_url="http://mock-backend",
    )
    defaults.update(kw)
    return NodeConfig(**defaults)


@pytest.mark.asyncio
@respx.mock
async def test_reregister_when_model_list_drifts():
    """Behavior: when the live backend model list differs from registered,
    register() is called again with the new list.
    This test fails if drift detection or re-registration is removed (#494).
    """
    # Register with phi3:mini + llama3.2:1b
    register_calls = []

    def capture_register(request):
        register_calls.append(request)
        return Response(200, json={"node_token": "tok-drift-1"})

    respx.post("http://mock-dir/v1/register").mock(side_effect=capture_register)
    # Backend now only has phi3:mini (llama3.2:1b drifted away)
    respx.get("http://mock-backend/api/tags").mock(
        return_value=Response(200, json={"models": [{"name": "phi3:mini"}]})
    )

    node = IicpNode(_cfg())
    # Simulate a prior registration (sets _registered_models)
    node._cfg.model = "phi3:mini"
    node._cfg.capabilities = ["llama3.2:1b"]
    node._registered_models = frozenset(["phi3:mini", "llama3.2:1b"])

    await node._maybe_reregister_on_model_drift()

    # register() must have been called with the new (drifted) model set
    assert len(register_calls) == 1, "re-register must fire when models drift"
    import json
    body = json.loads(register_calls[0].content)
    caps = body.get("capabilities", [])
    registered_models = {m for c in caps for m in c.get("models", [])}
    assert registered_models == {"phi3:mini"}, (
        f"re-register must use live model set; got {registered_models}"
    )


@pytest.mark.asyncio
@respx.mock
async def test_no_reregister_when_model_list_unchanged():
    """Behavior: when health probe returns the same models as registered,
    register() must NOT be called.
    """
    respx.post("http://mock-dir/v1/register").mock(
        return_value=Response(200, json={"node_token": "tok-same"})
    )
    respx.get("http://mock-backend/api/tags").mock(
        return_value=Response(200, json={"models": [{"name": "phi3:mini"}, {"name": "llama3.2:1b"}]})
    )

    node = IicpNode(_cfg())
    node._registered_models = frozenset(["llama3.2:1b", "phi3:mini"])  # same set

    await node._maybe_reregister_on_model_drift()

    # No registration call should have been made
    assert not any(r.request.url.path == "/v1/register" for r in respx.calls), (
        "must not re-register when models are unchanged"
    )


@pytest.mark.asyncio
@respx.mock
async def test_no_reregister_when_backend_returns_empty():
    """Behavior: when the backend health probe returns an empty list (backend offline),
    register() must NOT fire — prevents spurious re-registration during downtime.
    """
    respx.post("http://mock-dir/v1/register").mock(
        return_value=Response(200, json={"node_token": "tok-empty"})
    )
    respx.get("http://mock-backend/api/tags").mock(
        return_value=Response(200, json={"models": []})
    )

    node = IicpNode(_cfg())
    node._registered_models = frozenset(["phi3:mini"])

    await node._maybe_reregister_on_model_drift()

    assert not any(r.request.url.path == "/v1/register" for r in respx.calls), (
        "must not re-register when backend returns empty model list (transient downtime)"
    )


@pytest.mark.asyncio
async def test_no_reregister_when_no_backend_url():
    """Behavior: without a backend_url, drift detection is skipped entirely
    (backward compat for nodes without a backend probe).
    """
    cfg = _cfg(backend_url="")
    node = IicpNode(cfg)
    node._registered_models = frozenset(["phi3:mini"])

    # Should complete without any HTTP calls (no backend_url set)
    await node._maybe_reregister_on_model_drift()
    # If we reached here without error, the guard worked correctly.
