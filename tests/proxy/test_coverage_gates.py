"""D5: Coverage gate tests — edge cases for proxy routing components."""
from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from iicp_client.proxy.routing.circuit_breaker import CircuitBreaker, CircuitOpenError
from iicp_client.proxy.routing.fallback import FallbackChain
from iicp_client.proxy.routing.retry import RetryManager


def test_circuit_breaker_isolates_by_node():
    """Failures on node-A do not open the circuit for node-B."""
    cb = CircuitBreaker(threshold=2, reset_s=30)
    cb.record_failure("node-a")
    cb.record_failure("node-a")
    # node-a circuit is open
    with pytest.raises(CircuitOpenError):
        cb.check("node-a")
    # node-b circuit must still be closed
    cb.check("node-b")  # must not raise


async def test_retry_retries_on_429():
    """RetryManager retries when backend returns 429 (rate limit)."""
    calls = []

    async def fn():
        calls.append(1)
        if len(calls) < 2:
            resp = httpx.Response(429, request=httpx.Request("POST", "http://x"))
            raise httpx.HTTPStatusError("429", request=resp.request, response=resp)
        return {"ok": True}

    result = await RetryManager(max_retries=3, base_ms=1).run(fn)
    assert result == {"ok": True}
    assert len(calls) == 2


async def test_retry_retries_on_503():
    """RetryManager retries when backend returns 503 (service unavailable)."""
    calls = []

    async def fn():
        calls.append(1)
        if len(calls) < 2:
            resp = httpx.Response(503, request=httpx.Request("POST", "http://x"))
            raise httpx.HTTPStatusError("503", request=resp.request, response=resp)
        return {"ok": True}

    result = await RetryManager(max_retries=3, base_ms=1).run(fn)
    assert result == {"ok": True}
    assert len(calls) == 2


async def test_fallback_chain_all_nodes_fail_returns_structured_error():
    """PROXY-ROUTE-04: All retries exhausted → structured error dict (no raw exception)."""
    mock_router = AsyncMock()
    mock_router.route.side_effect = Exception("connection refused")

    chain = FallbackChain(router=mock_router)
    nodes = [
        {"node_id": "node-1", "endpoint": "http://node1:8080"},
        {"node_id": "node-2", "endpoint": "http://node2:8080"},
    ]
    task_id = uuid4()
    result = await chain.execute(
        nodes, task_id, "urn:iicp:intent:llm:chat:v1", {"messages": []}, 5000
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "no_available_node"
    assert "All nodes exhausted" in result["error"]["message"]
    assert str(task_id) == result["task_id"]


def test_circuit_breaker_resets_after_success():
    """Circuit breaker failure count resets to zero on a successful call."""
    cb = CircuitBreaker(threshold=3, reset_s=30)
    cb.record_failure("node-x")
    cb.record_failure("node-x")
    cb.record_success("node-x")
    # After success, one more failure should NOT open (count reset to 0)
    cb.record_failure("node-x")
    cb.check("node-x")  # must not raise — circuit still closed


def test_circuit_breaker_threshold_of_one_opens_immediately():
    """A threshold of 1 means the circuit opens after the very first failure."""
    cb = CircuitBreaker(threshold=1, reset_s=30)
    cb.record_failure("node-instant")
    with pytest.raises(CircuitOpenError):
        cb.check("node-instant")


async def test_retry_manager_succeeds_on_first_try():
    """RetryManager returns the result immediately when the first call succeeds."""
    async def always_ok():
        return {"result": "ok"}

    result = await RetryManager(max_retries=3, base_ms=1).run(always_ok)
    assert result == {"result": "ok"}


async def test_retry_manager_exhausts_raises():
    """RetryManager raises the last exception when all retries are exhausted."""
    async def always_fail():
        raise RuntimeError("permanent failure")

    with pytest.raises(RuntimeError, match="permanent failure"):
        await RetryManager(max_retries=2, base_ms=1).run(always_fail)
