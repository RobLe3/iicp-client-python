"""Tests for RetryManager."""
from __future__ import annotations

import httpx
import pytest

from iicp_client.proxy.routing.retry import CapacityExceededError, RetryManager, _is_capacity_exceeded


async def test_retry_succeeds_on_first_try():
    calls = []

    async def fn():
        calls.append(1)
        return {"ok": True}

    result = await RetryManager(max_retries=3, base_ms=1).run(fn)
    assert result == {"ok": True}
    assert len(calls) == 1


async def test_retry_retries_on_timeout():
    """PROXY-ROUTE-03 / PROXY-TIMEOUT-01: Retry on node timeout, next attempt within backoff."""
    calls = []

    async def fn():
        calls.append(1)
        if len(calls) < 3:
            raise httpx.TimeoutException("timeout")
        return {"ok": True}

    result = await RetryManager(max_retries=3, base_ms=1).run(fn)
    assert result == {"ok": True}
    assert len(calls) == 3


async def test_retry_exhausts_and_raises():
    """PROXY-TIMEOUT-02: All nodes exhausted → structured error raised."""
    async def fn():
        raise httpx.ConnectError("refused")

    with pytest.raises(httpx.ConnectError):
        await RetryManager(max_retries=2, base_ms=1).run(fn)


async def test_retry_does_not_retry_on_4xx():
    async def fn():
        resp = httpx.Response(401, request=httpx.Request("POST", "http://x"))
        raise httpx.HTTPStatusError("401", request=resp.request, response=resp)

    with pytest.raises(httpx.HTTPStatusError):
        await RetryManager(max_retries=3, base_ms=1).run(fn)


# QOS-ADMIT-02: capacity_exceeded 429 raises CapacityExceededError immediately (no backoff)

def test_is_capacity_exceeded_detects_correct_body():
    resp = httpx.Response(
        429,
        json={"error": {"code": "capacity_exceeded", "qos_class": "realtime"}},
        request=httpx.Request("POST", "http://x"),
    )
    exc = httpx.HTTPStatusError("429", request=resp.request, response=resp)
    assert _is_capacity_exceeded(exc) is True


def test_is_capacity_exceeded_ignores_generic_429():
    resp = httpx.Response(
        429,
        json={"error": {"code": "rate_limited"}},
        request=httpx.Request("POST", "http://x"),
    )
    exc = httpx.HTTPStatusError("429", request=resp.request, response=resp)
    assert _is_capacity_exceeded(exc) is False


def test_is_capacity_exceeded_ignores_non_429():
    resp = httpx.Response(503, request=httpx.Request("POST", "http://x"))
    exc = httpx.HTTPStatusError("503", request=resp.request, response=resp)
    assert _is_capacity_exceeded(exc) is False


async def test_capacity_exceeded_raises_immediately_no_retry():
    """QOS-ADMIT-02: capacity_exceeded 429 → CapacityExceededError, no exponential backoff."""
    calls = []

    async def fn():
        calls.append(1)
        resp = httpx.Response(
            429,
            json={"error": {"code": "capacity_exceeded", "qos_class": "realtime", "retry_after_ms": 2000}},
            request=httpx.Request("POST", "http://x"),
        )
        raise httpx.HTTPStatusError("429", request=resp.request, response=resp)

    with pytest.raises(CapacityExceededError):
        await RetryManager(max_retries=3, base_ms=1).run(fn)

    # Must raise on first attempt — no retry loop
    assert len(calls) == 1, "capacity_exceeded must not retry same node"
