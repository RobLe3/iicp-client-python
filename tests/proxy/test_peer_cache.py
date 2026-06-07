"""Tests for PeerCache."""
import httpx
import pytest
import respx

from iicp_client.proxy.network.peer_cache import PeerCache


@pytest.fixture
def cache():
    return PeerCache(directory_url="http://dir.test", ttl_s=30.0)


@pytest.fixture
def nodes():
    return [
        {"node_id": "aaa", "endpoint": "http://n1:8080", "available": True, "score": 0.9},
        {"node_id": "bbb", "endpoint": "http://n2:8080", "available": True, "score": 0.7},
    ]


async def test_get_nodes_returns_none_when_cache_empty(cache):
    result = await cache.get_nodes("urn:iicp:intent:llm:chat:v1")
    assert result is None


@respx.mock
async def test_fetch_and_cache_stores_result(cache, nodes):
    respx.get("http://dir.test/v1/discover").mock(
        return_value=httpx.Response(200, json={"nodes": nodes, "count": 2})
    )
    result = await cache.fetch_and_cache("urn:iicp:intent:llm:chat:v1")
    assert len(result) == 2
    cached = await cache.get_nodes("urn:iicp:intent:llm:chat:v1")
    assert cached is not None
    assert len(cached) == 2


@respx.mock
async def test_fetch_and_cache_handles_directory_failure(cache):
    respx.get("http://dir.test/v1/discover").mock(
        side_effect=httpx.ConnectError("refused")
    )
    result = await cache.fetch_and_cache("urn:iicp:intent:llm:chat:v1")
    assert result == []


@respx.mock
async def test_cache_respects_ttl(cache, nodes):
    import time
    respx.get("http://dir.test/v1/discover").mock(
        return_value=httpx.Response(200, json={"nodes": nodes, "count": 2})
    )
    await cache.fetch_and_cache("urn:iicp:intent:llm:chat:v1")

    # Manually expire the cache
    intent = "urn:iicp:intent:llm:chat:v1"
    old_nodes, _ = cache._cache[intent]
    cache._cache[intent] = (old_nodes, time.monotonic() - 999)

    stale = await cache.get_nodes(intent)
    assert stale is None


async def test_stop_does_not_raise_when_not_started(cache):
    cache.stop()  # should not raise
