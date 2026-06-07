"""Tests for proxy-side DID resolver (P6-4.2b-ii)."""
from __future__ import annotations

import base64

import httpx
import respx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from iicp_client.proxy.clients.did_resolver import DidResolver

BASE = "https://replica.example.com"


def _did_doc_with_pub(pub_raw: bytes) -> dict:
    return {
        "@context": ["https://www.w3.org/ns/did/v1"],
        "id": "did:web:replica.example.com",
        "verificationMethod": [{
            "id": "did:web:replica.example.com#key-1",
            "type": "JsonWebKey2020",
            "controller": "did:web:replica.example.com",
            "publicKeyJwk": {
                "kty": "OKP",
                "crv": "Ed25519",
                "x": base64.urlsafe_b64encode(pub_raw).decode().rstrip("="),
            },
        }],
    }


@respx.mock
async def test_extracts_valid_ed25519_key():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    respx.get(f"{BASE}/.well-known/did.json").mock(
        return_value=httpx.Response(200, json=_did_doc_with_pub(pub))
    )
    r = DidResolver()
    key = await r.public_key(BASE)
    assert key == pub
    assert len(key) == 32


@respx.mock
async def test_returns_none_on_404():
    respx.get(f"{BASE}/.well-known/did.json").mock(return_value=httpx.Response(404))
    assert await DidResolver().public_key(BASE) is None


@respx.mock
async def test_skips_placeholder_key():
    doc = _did_doc_with_pub(b"\x00" * 32)
    doc["verificationMethod"][0]["publicKeyJwk"]["x"] = "GENESIS_KEY_PENDING"
    respx.get(f"{BASE}/.well-known/did.json").mock(return_value=httpx.Response(200, json=doc))
    assert await DidResolver().public_key(BASE) is None


@respx.mock
async def test_skips_wrong_curve():
    doc = _did_doc_with_pub(b"\x00" * 32)
    doc["verificationMethod"][0]["publicKeyJwk"]["crv"] = "P-256"
    respx.get(f"{BASE}/.well-known/did.json").mock(return_value=httpx.Response(200, json=doc))
    assert await DidResolver().public_key(BASE) is None


@respx.mock
async def test_caches_result():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    route = respx.get(f"{BASE}/.well-known/did.json").mock(
        return_value=httpx.Response(200, json=_did_doc_with_pub(pub))
    )
    r = DidResolver()
    await r.public_key(BASE)
    await r.public_key(BASE)
    await r.public_key(BASE)
    assert route.call_count == 1, "must hit network only once within TTL"


@respx.mock
async def test_negative_cache_for_failure():
    # 404 → None — must also be cached so we don't pound a missing endpoint
    route = respx.get(f"{BASE}/.well-known/did.json").mock(return_value=httpx.Response(404))
    r = DidResolver()
    await r.public_key(BASE)
    await r.public_key(BASE)
    assert route.call_count == 1


@respx.mock
async def test_forget_clears_cache():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    route = respx.get(f"{BASE}/.well-known/did.json").mock(
        return_value=httpx.Response(200, json=_did_doc_with_pub(pub))
    )
    r = DidResolver()
    await r.public_key(BASE)
    r.forget(BASE)
    await r.public_key(BASE)
    assert route.call_count == 2
