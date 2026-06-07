"""DirectoryClient ↔ ReplicaRegistry cross-check on 307 redirects (P6-4.2a follow-on).

Verifies the proxy honors the registry as the source of truth for replica trust:
- Without a registry, behaves as before (seed-claimed trust used verbatim).
- With a registry, refuses redirects to endpoints absent from the registry.
- With a registry, downgrades trust when the seed over-claims vs. the registry.
"""
from __future__ import annotations

import httpx
import pytest
import respx
from httpx import Response

from iicp_client.proxy.clients.directory import DirectoryClient
from iicp_client.proxy.clients.replica_registry import ReplicaRegistry

SEED = "https://iicp.network"
REPLICA = "https://replica.example.com"
INTENT = "urn:iicp:intent:llm:chat:v1"


def _redirect(target: str = REPLICA, trust: str = "low") -> Response:
    return Response(
        307,
        headers={
            "Location": f"{target}/v1/discover?intent={INTENT}",
            "X-IICP-Replica-Trust": trust,
            "X-IICP-Redirect-Reason": "load",
            "Retry-After": "5",
        },
    )


def _registry_doc(replicas: list[dict] | None = None) -> dict:
    return {
        "@context": "https://iicp.network/ns/replicas/v1",
        "schema_version": "2",
        "genesis_seed": "did:web:iicp.network",
        "replicas": replicas or [],
        "updated_at": "2026-05-26",
    }


def _replica_entry(endpoint: str = REPLICA, tier: str = "medium", **overrides) -> dict:
    return {
        "replica_id": "rep-" + "a" * 32,
        "did": "did:web:replica.example.com",
        "endpoint": endpoint,
        "trust_tier": tier,
        "registered_at": "2026-05-26T12:00:00Z",
        **overrides,
    }


# ---------------------------------------------------------------------------
# Without a registry → previous behavior preserved
# ---------------------------------------------------------------------------

@respx.mock
async def test_no_registry_preserves_redirect_following():
    respx.get(f"{SEED}/v1/discover").mock(return_value=_redirect())
    respx.get(f"{REPLICA}/v1/discover").mock(return_value=Response(200, json={"nodes": [{"node_id": "n1"}]}))

    client = DirectoryClient(SEED)
    nodes = await client.discover(intent=INTENT)
    assert len(nodes) == 1


# ---------------------------------------------------------------------------
# With a registry, target known + tier matches → follow normally
# ---------------------------------------------------------------------------

@respx.mock
async def test_registry_known_replica_followed():
    respx.get(f"{SEED}/.well-known/iicp-replicas.json").mock(
        return_value=Response(200, json=_registry_doc([_replica_entry(tier="medium")]))
    )
    respx.get(f"{SEED}/v1/discover").mock(return_value=_redirect(trust="medium"))
    respx.get(f"{REPLICA}/v1/discover").mock(return_value=Response(200, json={"nodes": [{"node_id": "n1"}]}))

    reg = ReplicaRegistry(SEED)
    await reg.refresh()
    client = DirectoryClient(SEED, registry=reg)
    nodes = await client.discover(intent=INTENT)
    assert len(nodes) == 1


# ---------------------------------------------------------------------------
# With a registry, target NOT in registry → refuse redirect, retry on seed
# ---------------------------------------------------------------------------

@respx.mock
async def test_registry_unknown_replica_refused():
    respx.get(f"{SEED}/.well-known/iicp-replicas.json").mock(
        return_value=Response(200, json=_registry_doc([]))  # empty registry
    )
    seed_route = respx.get(f"{SEED}/v1/discover").mock(return_value=_redirect())
    # Replica MUST never be called when registry doesn't list it
    replica_route = respx.get(f"{REPLICA}/v1/discover").mock(
        return_value=Response(200, json={"nodes": [{"node_id": "should-not-appear"}]})
    )

    reg = ReplicaRegistry(SEED)
    await reg.refresh()
    client = DirectoryClient(SEED, registry=reg)

    # discover should fail with redirect_loop (proxy keeps retrying seed, gets 307s)
    with pytest.raises(httpx.HTTPStatusError):
        await client.discover(intent=INTENT)
    assert replica_route.call_count == 0, "Untrusted replica MUST NOT be contacted"
    # Seed was hit multiple times (the redirect loop)
    assert seed_route.call_count >= 2


# ---------------------------------------------------------------------------
# Seed over-claim → trust downgraded to registry-published tier
# ---------------------------------------------------------------------------

@respx.mock
async def test_seed_overclaim_downgrades_to_registry_tier():
    respx.get(f"{SEED}/.well-known/iicp-replicas.json").mock(
        return_value=Response(200, json=_registry_doc([_replica_entry(tier="low")]))
    )
    # Seed claims 'high', registry says 'low' → effective should be 'low'
    respx.get(f"{SEED}/v1/discover").mock(return_value=_redirect(trust="high"))
    respx.get(f"{REPLICA}/v1/discover").mock(return_value=Response(200, json={"nodes": [{"node_id": "n1"}]}))

    reg = ReplicaRegistry(SEED)
    await reg.refresh()
    client = DirectoryClient(SEED, registry=reg)
    nodes = await client.discover(intent=INTENT)
    # Redirect is followed, but the effective trust (in the span) is 'low'
    # We can't easily inspect the span from outside; the assertion that the
    # call succeeded proves the registry didn't refuse the redirect (target IS
    # known), and the warning log proves downgrade fired (not asserted here
    # because Caplog setup varies; the unit test for _reconcile_trust covers
    # the value directly).
    assert len(nodes) == 1


# ---------------------------------------------------------------------------
# Unit test for _reconcile_trust directly
# ---------------------------------------------------------------------------

@respx.mock
async def test_reconcile_trust_returns_none_when_unknown():
    respx.get(f"{SEED}/.well-known/iicp-replicas.json").mock(
        return_value=Response(200, json=_registry_doc([]))
    )
    reg = ReplicaRegistry(SEED)
    await reg.refresh()
    client = DirectoryClient(SEED, registry=reg)

    class _StubSpan:
        def set_attribute(self, *a, **kw): pass

    assert client._reconcile_trust("https://unknown.test", "high", _StubSpan()) is None


@respx.mock
async def test_reconcile_trust_returns_lower_of_seed_and_registry():
    respx.get(f"{SEED}/.well-known/iicp-replicas.json").mock(
        return_value=Response(200, json=_registry_doc([
            _replica_entry(endpoint="https://r-low.test", tier="low"),
            _replica_entry(
                endpoint="https://r-high.test", tier="high", did="did:web:r-high", replica_id="rep-" + "b" * 32
            ),
        ]))
    )
    reg = ReplicaRegistry(SEED)
    await reg.refresh()
    client = DirectoryClient(SEED, registry=reg)

    class _StubSpan:
        def set_attribute(self, *a, **kw): pass

    # Seed claims high, registry says low → low wins
    assert client._reconcile_trust("https://r-low.test", "high", _StubSpan()) == "low"
    # Seed claims low, registry says high → seed-claimed (low) wins (lower)
    assert client._reconcile_trust("https://r-high.test", "low", _StubSpan()) == "low"
    # Both medium → medium
    assert client._reconcile_trust("https://r-high.test", "medium", _StubSpan()) == "medium"
