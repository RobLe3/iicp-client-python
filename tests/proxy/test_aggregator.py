"""Tests for Phase 3.4 ResultAggregator — parallel redundancy mode."""
import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from iicp_client.proxy.routing.aggregator import ResultAggregator

TASK_ID = UUID("12345678-0000-0000-0000-000000000001")
INTENT = "urn:iicp:intent:llm:chat:v1"
PAYLOAD = {"messages": [{"role": "user", "content": "hi"}]}
TIMEOUT = 5000

NODE_A = {"node_id": "aaa", "endpoint": "http://a:8080"}
NODE_B = {"node_id": "bbb", "endpoint": "http://b:8080"}
NODE_C = {"node_id": "ccc", "endpoint": "http://c:8080"}

SUCCESS = {"task_id": str(TASK_ID), "status": "success", "result": {"data": "ok"}, "metrics": {}}
ERROR = {"task_id": str(TASK_ID), "status": "error", "result": None, "metrics": {}, "error": {}}


@pytest.fixture
def router():
    return MagicMock()


@pytest.fixture
def aggregator(router):
    return ResultAggregator(router=router, fan_out=3)


async def test_aggregator_returns_first_success(aggregator, router):
    router.route = AsyncMock(return_value=SUCCESS)
    result = await aggregator.execute([NODE_A, NODE_B], TASK_ID, INTENT, PAYLOAD, TIMEOUT)
    assert result["status"] == "success"


async def test_aggregator_returns_error_when_all_fail(aggregator, router):
    router.route = AsyncMock(return_value=ERROR)
    result = await aggregator.execute([NODE_A, NODE_B], TASK_ID, INTENT, PAYLOAD, TIMEOUT)
    assert result["status"] == "error"


async def test_aggregator_succeeds_if_one_succeeds(aggregator, router):
    call_count = 0

    async def route(node, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if node["node_id"] == "aaa":
            await asyncio.sleep(0.05)
            return ERROR
        return SUCCESS

    router.route = route
    result = await aggregator.execute([NODE_A, NODE_B], TASK_ID, INTENT, PAYLOAD, TIMEOUT)
    assert result["status"] == "success"


async def test_aggregator_empty_nodes_returns_error(aggregator, router):
    """WQ-030: empty discover → IICP-E033 (specific, distinct from no_available_node)."""
    router.route = AsyncMock(return_value=SUCCESS)
    result = await aggregator.execute([], TASK_ID, INTENT, PAYLOAD, TIMEOUT)
    assert result["status"] == "error"
    assert result["error"]["code"] == "IICP-E033"
    assert INTENT in result["error"]["message"]


async def test_aggregator_respects_fan_out_limit(aggregator, router):
    called_nodes = []

    async def route(node, *args, **kwargs):
        called_nodes.append(node["node_id"])
        return SUCCESS

    router.route = route
    await aggregator.execute([NODE_A, NODE_B, NODE_C], TASK_ID, INTENT, PAYLOAD, TIMEOUT)
    assert len(called_nodes) <= 3
