"""Tests for PeerCache."""
import httpx
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from iicp_client.proxy.network.peer_cache import PeerCache
from tests.proxy.ticket_helpers import public_key_hex, signed_ticket


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


@pytest.mark.ticketed_dispatch
@respx.mock
async def test_fetch_and_cache_prefers_verified_ticketed_routes():
    private_key = Ed25519PrivateKey.generate()
    intent = "urn:iicp:intent:llm:chat:v1"
    ticket_endpoint = "http://dir.test/v1/dispatch/ticket"
    requests = [
        httpx.Response(201, json={
            "ticket": signed_ticket(
                private_key,
                issuer="http://dir.test",
                node_id="node-11111111",
                intent=intent,
                jti="111111111111111111111111",
            ),
            "ticket_id_prefix": "ticket-one",
            "node_id": "node-11111111",
            "route": {"endpoint": "http://node-one.test"},
        }),
        httpx.Response(404, json={"error": {"code": "no_route_available"}}),
    ]
    ticket_route = respx.post(ticket_endpoint).mock(side_effect=requests)
    key_route = respx.get("http://dir.test/v1/directory-key").mock(return_value=httpx.Response(200, json={
        "public_key": public_key_hex(private_key),
        "algorithm": "ed25519",
    }))
    legacy_route = respx.get("http://dir.test/v1/discover").mock(return_value=httpx.Response(500))

    cache = PeerCache(directory_url="http://dir.test", ttl_s=30.0)
    result = await cache.fetch_and_cache(intent, limit=2)

    assert result == [{
        "endpoint": "http://node-one.test",
        "node_id": "node-11111111",
        "dispatch_ticket_id_prefix": "ticket-one",
    }]
    assert ticket_route.call_count == 2
    assert key_route.call_count == 1
    assert legacy_route.call_count == 0


@pytest.mark.ticketed_dispatch
@respx.mock
async def test_peer_cache_unverifiable_ticket_fails_closed():
    private_key = Ed25519PrivateKey.generate()
    respx.post("http://dir.test/v1/dispatch/ticket").mock(return_value=httpx.Response(201, json={
        "ticket": "not-a-ticket",
        "node_id": "node-11111111",
        "route": {"endpoint": "http://node-one.test"},
    }))
    respx.get("http://dir.test/v1/directory-key").mock(return_value=httpx.Response(200, json={
        "public_key": public_key_hex(private_key),
        "algorithm": "ed25519",
    }))
    legacy_route = respx.get("http://dir.test/v1/discover").mock(return_value=httpx.Response(200, json={
        "nodes": [{"node_id": "legacy"}],
    }))

    cache = PeerCache(directory_url="http://dir.test", ttl_s=30.0)
    assert await cache.fetch_and_cache("urn:iicp:intent:llm:chat:v1") == []
    assert legacy_route.call_count == 0


@pytest.mark.ticketed_dispatch
@respx.mock
async def test_peer_cache_auto_mode_has_explicit_legacy_rollback():
    respx.post("http://dir.test/v1/dispatch/ticket").mock(return_value=httpx.Response(404, json={}))
    legacy_route = respx.get("http://dir.test/v1/discover").mock(return_value=httpx.Response(200, json={
        "nodes": [{"node_id": "legacy"}],
    }))

    cache = PeerCache(directory_url="http://dir.test", ttl_s=30.0)
    assert await cache.fetch_and_cache("urn:iicp:intent:llm:chat:v1") == [{"node_id": "legacy"}]
    assert legacy_route.calls.last.request.url.params["view"] == "dispatch"


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
