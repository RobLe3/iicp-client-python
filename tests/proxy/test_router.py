"""Unit tests for TaskRouter — circuit breaker integration + retry wiring."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest

from iicp_client.proxy.routing.circuit_breaker import CircuitBreaker, CircuitOpenError
from iicp_client.proxy.routing.retry import RetryManager
from iicp_client.proxy.routing.router import TaskRouter

TASK_ID = uuid4()
INTENT = "urn:iicp:intent:llm:chat:v1"
PAYLOAD = {"messages": [{"role": "user", "content": "test"}]}
TIMEOUT_MS = 5000

NODE = {
    "node_id": "test-node-1",
    "endpoint": "http://1.2.3.4:8020",
    "region": "eu-central",
    "available": True,
}


def _make_router(
    max_retries: int = 2,
    threshold: int = 5,
    node_token: str = "test-token",
) -> TaskRouter:
    retry = RetryManager(max_retries=max_retries, base_ms=1)
    circuit = CircuitBreaker(threshold=threshold, reset_s=30)
    return TaskRouter(node_token=node_token, retry=retry, circuit=circuit)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_returns_result_on_success():
    """PROXY-ROUTE-01: Router discovers nodes via directory and routes; returns backend result on success."""
    expected = {"status": "success", "result": {"content": "ok"}}
    router = _make_router()

    with patch(
        "iicp_client.proxy.routing.router.NodeClient.submit_task",
        new=AsyncMock(return_value=expected),
    ):
        result = await router.route(NODE, TASK_ID, INTENT, PAYLOAD, TIMEOUT_MS)

    assert result == expected


@pytest.mark.asyncio
async def test_route_records_success_on_circuit_breaker():
    """Successful route resets failure count on circuit breaker."""
    router = _make_router(threshold=2)
    # Seed one failure first
    router._circuit.record_failure(NODE["node_id"])

    with patch(
        "iicp_client.proxy.routing.router.NodeClient.submit_task",
        new=AsyncMock(return_value={"status": "success"}),
    ):
        await router.route(NODE, TASK_ID, INTENT, PAYLOAD, TIMEOUT_MS)

    # After success, circuit should allow (failures reset)
    router._circuit.check(NODE["node_id"])  # should not raise


# ---------------------------------------------------------------------------
# Circuit breaker integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_raises_circuit_open_when_breaker_tripped():
    """Router raises CircuitOpenError immediately when circuit is open."""
    router = _make_router(threshold=1)
    router._circuit.record_failure(NODE["node_id"])

    with pytest.raises(CircuitOpenError) as exc_info:
        await router.route(NODE, TASK_ID, INTENT, PAYLOAD, TIMEOUT_MS)
    assert exc_info.value.node_id == NODE["node_id"]


@pytest.mark.asyncio
async def test_route_records_failure_on_exception():
    """Router records failure on circuit breaker when NodeClient raises."""
    router = _make_router(threshold=5)

    with patch(
        "iicp_client.proxy.routing.router.NodeClient.submit_task",
        new=AsyncMock(side_effect=httpx.ConnectError("backend down")),
    ):
        with pytest.raises(httpx.ConnectError):
            await router.route(NODE, TASK_ID, INTENT, PAYLOAD, TIMEOUT_MS)

    # Failure count should be > 0 — check by exhausting threshold
    assert router._circuit._failures.get(NODE["node_id"], 0) > 0


# ---------------------------------------------------------------------------
# Retry integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_retries_on_transient_error():
    """Router retries on httpx.ConnectError (retriable per RetryManager) then succeeds."""
    success_result = {"status": "success"}
    call_count = 0

    async def flaky_submit(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise httpx.ConnectError("transient connect error")
        return success_result

    router = _make_router(max_retries=3)
    with patch("iicp_client.proxy.routing.router.NodeClient.submit_task", new=flaky_submit):
        result = await router.route(NODE, TASK_ID, INTENT, PAYLOAD, TIMEOUT_MS)

    assert result == success_result
    assert call_count == 2  # Failed once, succeeded on retry


@pytest.mark.asyncio
async def test_route_raises_after_all_retries_exhausted():
    """Router propagates httpx.ConnectError after all retries fail."""
    router = _make_router(max_retries=2)

    with patch(
        "iicp_client.proxy.routing.router.NodeClient.submit_task",
        new=AsyncMock(side_effect=httpx.ConnectError("persistent failure")),
    ):
        with pytest.raises(httpx.ConnectError):
            await router.route(NODE, TASK_ID, INTENT, PAYLOAD, TIMEOUT_MS)


# ---------------------------------------------------------------------------
# Token forwarding
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_uses_configured_node_token():
    """Router creates NodeClient with the configured node_token."""
    captured_token: list[str] = []

    class CapturingClient:
        def __init__(self, endpoint: str, token: str, transport_endpoint: str | None = None):
            captured_token.append(token)

        async def submit_task(self, *args, **kwargs) -> dict:
            return {"status": "success"}

    router = _make_router(node_token="my-secret-token")
    with patch("iicp_client.proxy.routing.router.NodeClient", CapturingClient):
        await router.route(NODE, TASK_ID, INTENT, PAYLOAD, TIMEOUT_MS)

    assert captured_token == ["my-secret-token"]


# ---------------------------------------------------------------------------
# CIP-CALL-01: cip envelope passthrough (S.12 §4.1, §10.4)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_passes_cip_envelope_to_submit_task():
    """CIP-CALL-01: cip_envelope provided to route() must reach NodeClient.submit_task().

    When the coordinator dispatches a CIP sub-task, it constructs a cip envelope
    (cip_role='worker', cip_session_key) and passes it to route(). The router
    MUST forward it to NodeClient.submit_task() so the worker adapter receives
    the full S.12 §4.1 CALL body and produces a cip_receipt.
    """
    captured_kwargs: list[dict] = []

    async def capturing_submit(self, task_id, intent, payload, timeout_ms, **kwargs):
        captured_kwargs.append(kwargs)
        return {"status": "success"}

    cip_env = {"cip_role": "worker", "cip_session_key": "sess-abc-123", "cip_parent_task_id": str(TASK_ID)}
    router = _make_router()
    with patch("iicp_client.proxy.routing.router.NodeClient.submit_task", new=capturing_submit):
        await router.route(NODE, TASK_ID, INTENT, PAYLOAD, TIMEOUT_MS, cip_envelope=cip_env)

    assert len(captured_kwargs) == 1
    assert captured_kwargs[0]["cip_envelope"] == cip_env


@pytest.mark.asyncio
async def test_route_omits_cip_envelope_when_not_provided():
    """CIP-CALL-01: when cip_envelope is None (non-CIP dispatch), it is not forwarded."""
    captured_kwargs: list[dict] = []

    async def capturing_submit(self, task_id, intent, payload, timeout_ms, **kwargs):
        captured_kwargs.append(kwargs)
        return {"status": "success"}

    router = _make_router()
    with patch("iicp_client.proxy.routing.router.NodeClient.submit_task", new=capturing_submit):
        await router.route(NODE, TASK_ID, INTENT, PAYLOAD, TIMEOUT_MS)

    # cip_envelope=None must not inject anything; it may be present as None
    cip_val = captured_kwargs[0].get("cip_envelope")
    assert cip_val is None


@pytest.mark.asyncio
async def test_router_ssrf_guard_rejects_private_endpoint():
    """SSRF guard: router must raise ValueError for non-routable node endpoints (#388)."""
    router = _make_router()
    ssrf_node = {
        "node_id": "malicious-node",
        "endpoint": "http://192.168.1.100:8080",
        "region": "eu-central",
        "available": True,
    }
    with pytest.raises(ValueError, match="not publicly routable"):
        await router.route(ssrf_node, TASK_ID, INTENT, PAYLOAD, TIMEOUT_MS)


@pytest.mark.asyncio
async def test_router_ssrf_guard_rejects_metadata_endpoint():
    """SSRF guard: AWS metadata endpoint must be rejected (#388)."""
    router = _make_router()
    metadata_node = {
        "node_id": "metadata-node",
        "endpoint": "http://169.254.169.254/latest/meta-data/",
        "region": "eu-central",
        "available": True,
    }
    with pytest.raises(ValueError, match="not publicly routable"):
        await router.route(metadata_node, TASK_ID, INTENT, PAYLOAD, TIMEOUT_MS)
