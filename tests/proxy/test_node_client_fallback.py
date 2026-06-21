"""Tests for NodeClient HTTP→IICP fallback logic (spec v0.7.0 dual-endpoint).

Tests the fallback chain when HTTP endpoint fails and transport_endpoint is available.
"""
from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
import respx
from httpx import Response

from iicp_client.proxy.clients.node import NodeClient


@respx.mock
async def test_http_endpoint_succeeds_no_fallback():
    """When HTTP succeeds, fallback should not be attempted."""
    node_token = "test-token"
    endpoint = "http://node1:8080"
    transport_endpoint = "iicp://node1:9484"

    task_id = uuid4()
    intent = "urn:iicp:intent:llm:chat:v1"
    payload = {"messages": []}
    timeout_ms = 5000

    # Mock successful HTTP response
    respx.post(f"{endpoint}/v1/task").mock(
        return_value=Response(200, json={"status": "success", "result": {"text": "hello"}})
    )

    # Ensure transport endpoint is NOT called
    respx.post(f"{transport_endpoint}/v1/task").mock(side_effect=Exception("Should not be called"))

    client = NodeClient(endpoint, node_token, transport_endpoint=transport_endpoint)
    result = await client.submit_task(task_id, intent, payload, timeout_ms)

    assert result["status"] == "success"


@respx.mock
async def test_http_fails_fallback_to_transport_succeeds():
    """When HTTP fails with ConnectError, should retry on transport_endpoint."""
    node_token = "test-token"
    endpoint = "http://node1:8080"
    transport_endpoint = "iicp://node1:9484"

    task_id = uuid4()
    intent = "urn:iicp:intent:llm:chat:v1"
    payload = {"messages": []}
    timeout_ms = 5000

    # Mock HTTP failure
    respx.post(f"{endpoint}/v1/task").mock(side_effect=httpx.ConnectError("Connection refused"))

    # Mock successful transport response
    respx.post(f"{transport_endpoint}/v1/task").mock(
        return_value=Response(200, json={"status": "success", "result": {"text": "fallback success"}})
    )

    client = NodeClient(endpoint, node_token, transport_endpoint=transport_endpoint)
    result = await client.submit_task(task_id, intent, payload, timeout_ms)

    assert result["status"] == "success"
    assert result["result"]["text"] == "fallback success"


@respx.mock
async def test_http_fails_no_transport_endpoint_raises():
    """When HTTP fails and no transport_endpoint available, should raise."""
    node_token = "test-token"
    endpoint = "http://node1:8080"

    task_id = uuid4()
    intent = "urn:iicp:intent:llm:chat:v1"
    payload = {"messages": []}
    timeout_ms = 5000

    # Mock HTTP failure
    respx.post(f"{endpoint}/v1/task").mock(side_effect=httpx.ConnectError("Connection refused"))

    client = NodeClient(endpoint, node_token, transport_endpoint=None)

    with pytest.raises(httpx.ConnectError):
        await client.submit_task(task_id, intent, payload, timeout_ms)


@respx.mock
async def test_http_fails_transport_also_fails_raises():
    """When both HTTP and transport fail, should raise from transport."""
    node_token = "test-token"
    endpoint = "http://node1:8080"
    transport_endpoint = "iicp://node1:9484"

    task_id = uuid4()
    intent = "urn:iicp:intent:llm:chat:v1"
    payload = {"messages": []}
    timeout_ms = 5000

    # Mock both endpoints failing
    respx.post(f"{endpoint}/v1/task").mock(side_effect=httpx.ConnectError("HTTP down"))
    respx.post(f"{transport_endpoint}/v1/task").mock(side_effect=httpx.TimeoutException("Transport timeout"))

    client = NodeClient(endpoint, node_token, transport_endpoint=transport_endpoint)

    with pytest.raises(httpx.TimeoutException):
        await client.submit_task(task_id, intent, payload, timeout_ms)


@respx.mock
async def test_transport_fallback_only_attempted_once():
    """Fallback to transport_endpoint should only be attempted once per client instance."""
    node_token = "test-token"
    endpoint = "http://node1:8080"
    transport_endpoint = "iicp://node1:9484"

    task_id1 = uuid4()
    task_id2 = uuid4()
    intent = "urn:iicp:intent:llm:chat:v1"
    payload = {"messages": []}
    timeout_ms = 5000

    # Mock HTTP always failing
    respx.post(f"{endpoint}/v1/task").mock(side_effect=httpx.ConnectError("HTTP down"))

    # Mock transport: succeeds on first attempt, would fail on second if called
    transport_attempts = []
    def transport_mock(*args, **kwargs):
        transport_attempts.append(1)
        if len(transport_attempts) == 1:
            return Response(200, json={"status": "success", "result": {"text": f"success {len(transport_attempts)}"}})
        raise Exception("Should not retry transport endpoint")

    respx.post(f"{transport_endpoint}/v1/task").mock(side_effect=transport_mock)

    client = NodeClient(endpoint, node_token, transport_endpoint=transport_endpoint)

    # First submission should succeed via fallback
    result1 = await client.submit_task(task_id1, intent, payload, timeout_ms)
    assert result1["status"] == "success"

    # Second submission should fail on HTTP and NOT retry transport again
    # (because _transport_attempted is already True)
    with pytest.raises(httpx.ConnectError):
        await client.submit_task(task_id2, intent, payload, timeout_ms)

    # Verify transport was only called once total
    assert len(transport_attempts) == 1


@respx.mock
async def test_http_status_error_triggers_fallback():
    """HTTP 503 errors should also trigger fallback to transport."""
    node_token = "test-token"
    endpoint = "http://node1:8080"
    transport_endpoint = "iicp://node1:9484"

    task_id = uuid4()
    intent = "urn:iicp:intent:llm:chat:v1"
    payload = {"messages": []}
    timeout_ms = 5000

    # Mock HTTP with 503
    respx.post(f"{endpoint}/v1/task").mock(
        return_value=Response(503, json={"error": "Service unavailable"})
    )

    # Mock successful transport response
    respx.post(f"{transport_endpoint}/v1/task").mock(
        return_value=Response(200, json={"status": "success", "result": {"text": "recovered"}})
    )

    client = NodeClient(endpoint, node_token, transport_endpoint=transport_endpoint)
    result = await client.submit_task(task_id, intent, payload, timeout_ms)

    assert result["status"] == "success"
    assert result["result"]["text"] == "recovered"

@respx.mock
async def test_submit_task_forwards_source_node_id_for_self_query_neutrality():
    """#525/G1b: proxy/coordinator dispatch identifies the querying node for credit neutrality."""
    node_token = "test-token"
    endpoint = "http://node1:8080"
    task_id = uuid4()

    route = respx.post(f"{endpoint}/v1/task").mock(
        return_value=Response(200, json={"status": "success", "result": {"text": "ok"}})
    )

    client = NodeClient(endpoint, node_token)
    result = await client.submit_task(
        task_id,
        "urn:iicp:intent:llm:chat:v1",
        {"messages": []},
        5000,
        source_node_id="consumer-node-1",
    )

    assert result["status"] == "success"
    sent = json.loads(route.calls[0].request.content)
    assert sent["source_node_id"] == "consumer-node-1"
