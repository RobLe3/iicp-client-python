"""Tests for ReplicaRegistry — proxy-side trusted-replicas client (P6-4.2a)."""
from __future__ import annotations

import httpx
import respx

from iicp_client.proxy.clients.replica_registry import ReplicaRegistry


def _valid_doc(replicas: list[dict] | None = None) -> dict:
    return {
        "@context": "https://iicp.network/ns/replicas/v1",
        "schema_version": "2",
        "genesis_seed": "did:web:iicp.network",
        "version": "2",
        "updated_at": "2026-05-26",
        "replicas": replicas or [],
    }


def _entry(endpoint: str = "https://r1.test", tier: str = "low", **overrides) -> dict:
    return {
        "replica_id": "rep-" + "a" * 32,
        "did": "did:web:r1.test",
        "endpoint": endpoint,
        "trust_tier": tier,
        "registered_at": "2026-05-26T12:00:00Z",
        **overrides,
    }


@respx.mock
async def test_refresh_loads_valid_entries():
    respx.get("https://iicp.network/.well-known/iicp-replicas.json").mock(
        return_value=httpx.Response(200, json=_valid_doc([_entry()]))
    )
    reg = ReplicaRegistry("https://iicp.network")
    count = await reg.refresh()
    assert count == 1
    assert reg.entry_count == 1


@respx.mock
async def test_refresh_skips_entries_missing_required_fields():
    # missing trust_tier + registered_at
    bad = {"replica_id": "rep-x", "did": "did:web:x", "endpoint": "https://x.test"}
    good = _entry()
    respx.get("https://iicp.network/.well-known/iicp-replicas.json").mock(
        return_value=httpx.Response(200, json=_valid_doc([bad, good]))
    )
    reg = ReplicaRegistry("https://iicp.network")
    count = await reg.refresh()
    assert count == 1  # only good entry kept


@respx.mock
async def test_refresh_skips_invalid_trust_tier():
    respx.get("https://iicp.network/.well-known/iicp-replicas.json").mock(
        return_value=httpx.Response(200, json=_valid_doc([_entry(tier="ultra-mega")]))
    )
    reg = ReplicaRegistry("https://iicp.network")
    count = await reg.refresh()
    assert count == 0


@respx.mock
async def test_refresh_skips_non_https_endpoints():
    respx.get("https://iicp.network/.well-known/iicp-replicas.json").mock(
        return_value=httpx.Response(200, json=_valid_doc([_entry(endpoint="http://insecure.test")]))
    )
    reg = ReplicaRegistry("https://iicp.network")
    count = await reg.refresh()
    assert count == 0


@respx.mock
async def test_refresh_rejects_wrong_schema_version():
    doc = _valid_doc([_entry()])
    doc["schema_version"] = "1"
    respx.get("https://iicp.network/.well-known/iicp-replicas.json").mock(
        return_value=httpx.Response(200, json=doc)
    )
    reg = ReplicaRegistry("https://iicp.network")
    count = await reg.refresh()
    assert count == -1


@respx.mock
async def test_refresh_failure_keeps_previous_cache():
    # First refresh succeeds
    respx.get("https://iicp.network/.well-known/iicp-replicas.json").mock(
        return_value=httpx.Response(200, json=_valid_doc([_entry()]))
    )
    reg = ReplicaRegistry("https://iicp.network")
    await reg.refresh()
    assert reg.entry_count == 1

    # Second refresh: 503 should not wipe the cache
    respx.get("https://iicp.network/.well-known/iicp-replicas.json").mock(
        return_value=httpx.Response(503, text="upstream down")
    )
    count = await reg.refresh()
    assert count == -1
    assert reg.entry_count == 1, "degraded mode: previous cache preserved on fetch failure"


@respx.mock
async def test_lookup_returns_entry_for_matching_endpoint():
    respx.get("https://iicp.network/.well-known/iicp-replicas.json").mock(
        return_value=httpx.Response(200, json=_valid_doc([_entry(endpoint="https://r-eu.test", tier="high")]))
    )
    reg = ReplicaRegistry("https://iicp.network")
    await reg.refresh()
    # lookup with a full URL (path/query) — registry should match on host only
    entry = await reg.lookup("https://r-eu.test/v1/discover?intent=foo")
    assert entry is not None
    assert entry["trust_tier"] == "high"


@respx.mock
async def test_lookup_returns_none_for_unknown_endpoint():
    respx.get("https://iicp.network/.well-known/iicp-replicas.json").mock(
        return_value=httpx.Response(200, json=_valid_doc([]))
    )
    reg = ReplicaRegistry("https://iicp.network")
    await reg.refresh()
    entry = await reg.lookup("https://unknown.test/v1/discover")
    assert entry is None


@respx.mock
async def test_trust_tier_of_returns_low_for_unknown():
    respx.get("https://iicp.network/.well-known/iicp-replicas.json").mock(
        return_value=httpx.Response(200, json=_valid_doc([]))
    )
    reg = ReplicaRegistry("https://iicp.network")
    await reg.refresh()
    # Untrusted endpoints default to 'low' — safe-by-default
    assert reg.trust_tier_of("https://attacker.example.com") == "low"


@respx.mock
async def test_trust_tier_of_returns_published_tier_for_known():
    respx.get("https://iicp.network/.well-known/iicp-replicas.json").mock(
        return_value=httpx.Response(200, json=_valid_doc([
            _entry(endpoint="https://r-high.test", tier="high"),
            _entry(
                endpoint="https://r-med.test", tier="medium", did="did:web:r-med.test", replica_id="rep-" + "b" * 32
            ),
        ]))
    )
    reg = ReplicaRegistry("https://iicp.network")
    await reg.refresh()
    assert reg.trust_tier_of("https://r-high.test") == "high"
    assert reg.trust_tier_of("https://r-med.test") == "medium"


@respx.mock
async def test_lookup_triggers_refresh_when_stale():
    # Use a very short TTL so the second lookup forces a refresh
    route = respx.get("https://iicp.network/.well-known/iicp-replicas.json").mock(
        return_value=httpx.Response(200, json=_valid_doc([]))
    )
    reg = ReplicaRegistry("https://iicp.network", ttl_s=0.001)
    await reg.refresh()
    assert route.call_count == 1
    # Sleep past TTL so the second lookup re-fetches
    import asyncio
    await asyncio.sleep(0.005)
    await reg.lookup("https://anything.test")
    assert route.call_count == 2
