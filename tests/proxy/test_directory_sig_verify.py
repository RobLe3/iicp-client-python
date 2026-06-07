"""DirectoryClient end-to-end signature verification (P6-4.2b-ii).

Verifies the proxy rejects replica responses with invalid/missing sigs
and accepts replica responses with valid sigs. Integrated test using the
ReplicaRegistry + DidResolver + replica_sig_verifier together.
"""
from __future__ import annotations

import base64

import httpx
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from httpx import Response

from iicp_client.proxy.clients.did_resolver import DidResolver
from iicp_client.proxy.clients.directory import DirectoryClient
from iicp_client.proxy.clients.replica_registry import ReplicaRegistry
from iicp_client.proxy.clients.replica_sig_verifier import signing_input

SEED = "https://iicp.network"
REPLICA = "https://replica.example.com"
INTENT = "urn:iicp:intent:llm:chat:v1"


def _registry_doc(replicas: list[dict]) -> dict:
    return {
        "schema_version": "2",
        "genesis_seed": "did:web:iicp.network",
        "replicas": replicas,
        "updated_at": "2026-05-26",
    }


def _replica_entry(tier: str = "medium") -> dict:
    return {
        "replica_id": "rep-" + "a" * 32,
        "did": "did:web:replica.example.com",
        "endpoint": REPLICA,
        "trust_tier": tier,
        "registered_at": "2026-05-26T12:00:00Z",
    }


def _did_doc(pub_raw: bytes) -> dict:
    return {
        "@context": ["https://www.w3.org/ns/did/v1"],
        "id": "did:web:replica.example.com",
        "verificationMethod": [{
            "publicKeyJwk": {
                "kty": "OKP", "crv": "Ed25519",
                "x": base64.urlsafe_b64encode(pub_raw).decode().rstrip("="),
            },
        }],
    }


def _redirect_to_replica() -> Response:
    return Response(
        307,
        headers={
            "Location": f"{REPLICA}/v1/discover?intent={INTENT}",
            "X-IICP-Replica-Trust": "medium",
            "X-IICP-Redirect-Reason": "load",
            "Retry-After": "5",
        },
    )


def _signed_discover(priv: Ed25519PrivateKey, body: bytes, snapshot_seq: int, query: str) -> Response:
    """Build a signed replica discover response.

    The signing input MUST match what httpx sends — the actual request path
    is /v1/discover, query is the canonical form. We sign exactly what the
    server-side middleware would sign.
    """
    msg = signing_input("GET", "/v1/discover", query, snapshot_seq, body)
    sig = priv.sign(msg).hex()
    return Response(
        200,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-IICP-Replica-DID": "did:web:replica.example.com",
            "X-IICP-Replica-Sig": sig,
            "X-IICP-Snapshot-Seq": str(snapshot_seq),
        },
    )


@respx.mock
async def test_valid_replica_sig_accepted():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    respx.get(f"{SEED}/.well-known/iicp-replicas.json").mock(
        return_value=Response(200, json=_registry_doc([_replica_entry()]))
    )
    respx.get(f"{REPLICA}/.well-known/did.json").mock(
        return_value=Response(200, json=_did_doc(pub))
    )
    respx.get(f"{SEED}/v1/discover").mock(return_value=_redirect_to_replica())
    body = b'{"nodes":[{"node_id":"n1"}],"count":1}'
    respx.get(f"{REPLICA}/v1/discover").mock(
        return_value=_signed_discover(priv, body, 42, "intent=urn%3Aiicp%3Aintent%3Allm%3Achat%3Av1&limit=5")
    )

    reg = ReplicaRegistry(SEED)
    await reg.refresh()
    client = DirectoryClient(SEED, registry=reg, did_resolver=DidResolver())
    nodes = await client.discover(intent=INTENT)
    assert len(nodes) == 1
    assert nodes[0]["node_id"] == "n1"


@respx.mock
async def test_missing_sig_header_rejected():
    pub = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    respx.get(f"{SEED}/.well-known/iicp-replicas.json").mock(
        return_value=Response(200, json=_registry_doc([_replica_entry()]))
    )
    respx.get(f"{REPLICA}/.well-known/did.json").mock(
        return_value=Response(200, json=_did_doc(pub))
    )
    respx.get(f"{SEED}/v1/discover").mock(return_value=_redirect_to_replica())
    # Replica returns nodes BUT no X-IICP-Replica-Sig header — proxy must reject
    respx.get(f"{REPLICA}/v1/discover").mock(
        return_value=Response(200, json={"nodes": [{"node_id": "attacker"}]})
    )

    reg = ReplicaRegistry(SEED)
    await reg.refresh()
    client = DirectoryClient(SEED, registry=reg, did_resolver=DidResolver())

    # After rejection, proxy retries seed; seed returns 307 again → eventually
    # exceeds max-chain → HTTPStatusError
    with pytest.raises(httpx.HTTPStatusError):
        await client.discover(intent=INTENT)


@respx.mock
async def test_tampered_body_rejected():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    respx.get(f"{SEED}/.well-known/iicp-replicas.json").mock(
        return_value=Response(200, json=_registry_doc([_replica_entry()]))
    )
    respx.get(f"{REPLICA}/.well-known/did.json").mock(
        return_value=Response(200, json=_did_doc(pub))
    )
    respx.get(f"{SEED}/v1/discover").mock(return_value=_redirect_to_replica())
    # Sign one body, return a different one (mid-flight tamper)
    signed_for = b'{"nodes":[{"node_id":"legitimate"}]}'
    msg = signing_input("GET", "/v1/discover", "intent=urn%3Aiicp%3Aintent%3Allm%3Achat%3Av1&limit=5", 42, signed_for)
    sig = priv.sign(msg).hex()
    tampered = b'{"nodes":[{"node_id":"attacker-injection"}]}'
    respx.get(f"{REPLICA}/v1/discover").mock(
        return_value=Response(
            200, content=tampered,
            headers={
                "Content-Type": "application/json",
                "X-IICP-Replica-DID": "did:web:replica.example.com",
                "X-IICP-Replica-Sig": sig,
                "X-IICP-Snapshot-Seq": "42",
            },
        )
    )

    reg = ReplicaRegistry(SEED)
    await reg.refresh()
    client = DirectoryClient(SEED, registry=reg, did_resolver=DidResolver())

    with pytest.raises(httpx.HTTPStatusError):
        await client.discover(intent=INTENT)


@respx.mock
async def test_did_unresolvable_rejected():
    priv = Ed25519PrivateKey.generate()
    respx.get(f"{SEED}/.well-known/iicp-replicas.json").mock(
        return_value=Response(200, json=_registry_doc([_replica_entry()]))
    )
    # DID document 404 → public_key returns None → reject
    respx.get(f"{REPLICA}/.well-known/did.json").mock(return_value=Response(404))
    respx.get(f"{SEED}/v1/discover").mock(return_value=_redirect_to_replica())
    body = b'{"nodes":[]}'
    respx.get(f"{REPLICA}/v1/discover").mock(
        return_value=_signed_discover(priv, body, 42, "intent=urn%3Aiicp%3Aintent%3Allm%3Achat%3Av1&limit=5")
    )

    reg = ReplicaRegistry(SEED)
    await reg.refresh()
    client = DirectoryClient(SEED, registry=reg, did_resolver=DidResolver())

    with pytest.raises(httpx.HTTPStatusError):
        await client.discover(intent=INTENT)


@respx.mock
async def test_no_verifier_passes_through_unsigned_replica_response():
    # Backwards compat: without a did_resolver, proxy doesn't verify (P6-4.2a
    # behavior unchanged — useful for incremental adoption)
    respx.get(f"{SEED}/.well-known/iicp-replicas.json").mock(
        return_value=Response(200, json=_registry_doc([_replica_entry()]))
    )
    respx.get(f"{SEED}/v1/discover").mock(return_value=_redirect_to_replica())
    respx.get(f"{REPLICA}/v1/discover").mock(
        return_value=Response(200, json={"nodes": [{"node_id": "unverified-but-accepted"}]})
    )

    reg = ReplicaRegistry(SEED)
    await reg.refresh()
    client = DirectoryClient(SEED, registry=reg)  # no did_resolver
    nodes = await client.discover(intent=INTENT)
    assert len(nodes) == 1
    assert nodes[0]["node_id"] == "unverified-but-accepted"


@respx.mock
async def test_seed_response_skips_sig_verify():
    # Seed itself doesn't sign — its responses are TLS+DNS trusted. The
    # verifier MUST only fire for replica (non-seed) responses.
    respx.get(f"{SEED}/.well-known/iicp-replicas.json").mock(
        return_value=Response(200, json=_registry_doc([]))
    )
    respx.get(f"{SEED}/v1/discover").mock(
        return_value=Response(200, json={"nodes": [{"node_id": "seed-direct"}]})
    )

    reg = ReplicaRegistry(SEED)
    await reg.refresh()
    client = DirectoryClient(SEED, registry=reg, did_resolver=DidResolver())
    nodes = await client.discover(intent=INTENT)
    assert nodes[0]["node_id"] == "seed-direct", "seed responses must skip sig verify"
