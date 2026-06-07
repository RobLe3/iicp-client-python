# SPDX-License-Identifier: Apache-2.0
"""CIP dispatch wiring tests for Anthropic compat surface (CIP-CALL-01).

Verifies that _execute_iicp() in proxy.anthropic_compat.server correctly reads
cip_config and cip_budget_tracker from app.state and forwards them to
compute_cip_envelope(). Tests cover the app-state plumbing path only —
CIP decision logic is covered by test_openai_cip_dispatch.py.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from iicp_client.proxy.anthropic_compat.server import _execute_iicp
from iicp_client.proxy.cip.coordinator import CIPDispatchConfig, CIPStrategy
from iicp_client.proxy.cip.strategies import SessionBudgetTracker

_SUCCESS = {
    "status": "success",
    "result": {
        "choices": [{"message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    },
}
_NODES = [{"node_id": "n-1", "allow_remote_inference": True}]
_BODY = {"messages": [{"role": "user", "content": "hi"}], "model": "claude-3-5-sonnet-20241022"}


def _make_request(*, cip_config=None, cip_budget_tracker=None, nodes=None):
    effective_nodes = nodes if nodes is not None else _NODES
    fallback_chain = MagicMock()
    fallback_chain.execute = AsyncMock(return_value=_SUCCESS)
    directory = MagicMock()
    directory.discover = AsyncMock(return_value=effective_nodes)
    selector = MagicMock()
    selector.select = MagicMock(return_value=effective_nodes)
    state = SimpleNamespace(
        fallback_chain=fallback_chain,
        directory=directory,
        selector=selector,
        cip_config=cip_config,
        cip_budget_tracker=cip_budget_tracker,
    )
    request = MagicMock()
    request.app = SimpleNamespace(state=state)
    return request


async def test_cip_config_none_passes_none_to_compute():
    """When cip_config is absent, compute_cip_envelope is called with None (3rd arg)."""
    request = _make_request(cip_config=None)
    with patch("iicp_client.proxy.anthropic_compat.server.compute_cip_envelope", return_value=None) as mock_cip, \
         patch("iicp_client.proxy.anthropic_compat.server.proxy_route_span"):
        await _execute_iicp(request, _BODY)

    mock_cip.assert_called_once()
    assert mock_cip.call_args.args[2] is None


async def test_cip_config_forwarded_as_third_positional_arg():
    """cip_config set on app.state is forwarded as the 3rd positional arg."""
    cfg = CIPDispatchConfig(enabled=True, strategy=CIPStrategy.REMOTE_FIRST)
    request = _make_request(cip_config=cfg)
    with patch("iicp_client.proxy.anthropic_compat.server.compute_cip_envelope", return_value=None) as mock_cip, \
         patch("iicp_client.proxy.anthropic_compat.server.proxy_route_span"):
        await _execute_iicp(request, _BODY)

    assert mock_cip.call_args.args[2] is cfg


async def test_session_tracker_forwarded_as_kwarg():
    """cip_budget_tracker on app.state is passed as session_tracker kwarg."""
    cfg = CIPDispatchConfig(enabled=True, strategy=CIPStrategy.REMOTE_FIRST)
    tracker = SessionBudgetTracker(session_credit_budget=10.0)
    request = _make_request(cip_config=cfg, cip_budget_tracker=tracker)
    with patch("iicp_client.proxy.anthropic_compat.server.compute_cip_envelope", return_value=None) as mock_cip, \
         patch("iicp_client.proxy.anthropic_compat.server.proxy_route_span"):
        await _execute_iicp(request, _BODY)

    assert mock_cip.call_args.kwargs.get("session_tracker") is tracker


async def test_envelope_from_compute_cip_forwarded_to_execute():
    """Envelope returned by compute_cip_envelope is passed to fallback_chain.execute."""
    cfg = CIPDispatchConfig(enabled=True, strategy=CIPStrategy.REMOTE_FIRST)
    request = _make_request(cip_config=cfg)
    fake_envelope = {"cip_role": "worker", "cip_session_key": "a" * 64, "cip_parent_task_id": "t-1"}
    with patch("iicp_client.proxy.anthropic_compat.server.compute_cip_envelope", return_value=fake_envelope), \
         patch("iicp_client.proxy.anthropic_compat.server.proxy_route_span"):
        await _execute_iicp(request, _BODY)

    fc = request.app.state.fallback_chain
    assert fc.execute.call_args.kwargs.get("cip_envelope") is fake_envelope


async def test_no_tracker_passes_none_session_tracker():
    """When cip_budget_tracker is absent, session_tracker=None is passed."""
    cfg = CIPDispatchConfig(enabled=True, strategy=CIPStrategy.REMOTE_FIRST)
    request = _make_request(cip_config=cfg, cip_budget_tracker=None)
    with patch("iicp_client.proxy.anthropic_compat.server.compute_cip_envelope", return_value=None) as mock_cip, \
         patch("iicp_client.proxy.anthropic_compat.server.proxy_route_span"):
        await _execute_iicp(request, _BODY)

    assert mock_cip.call_args.kwargs.get("session_tracker") is None
