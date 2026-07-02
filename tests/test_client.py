"""Unit tests for IicpClient (ADR-016 SDK-01..SDK-06)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx

from iicp_client import (
    ChatMessage,
    ChatOptions,
    ClientConfig,
    DiscoverOptions,
    IicpClient,
    IicpError,
    TaskAuth,
    TaskRequest,
)
from iicp_client.node import IicpNode, NodeConfig

DIRECTORY = "https://iicp.test"
NODE = "https://1.2.3.4:9484"
DISCOVER_URL = f"{DIRECTORY}/v1/discover"
TASK_URL = f"{NODE}/v1/task"
NODE_KEYED_FALLBACK = "https://5.6.7.8:9484"
TASK_URL_KEYED_FALLBACK = f"{NODE_KEYED_FALLBACK}/v1/task"

CX_KEY_FIXTURE = {
    "algorithm": "X25519",
    "encoding": "base64url",
    "key": "-LKZgrZEnFMr9ctB3uQDKsME07ZzS4Ce-SapFAePul0",
    "key_id": "cx-fixture",
}

GOOD_NODES = {
    "nodes": [
        {
            "node_id": "node-abc",
            "endpoint": NODE,
            "score": 0.95,
            "available": True,
            "region": "eu-west",
            # ADR-044 / ADR-043 fields (directory v1.10.0+)
            "health_label": "healthy",
            "exposure_mode": "ipv4_public_direct",
            "transport": ["https", "iicp-native"],
            "directory_observed_reachable": True,
            "route_evidence": "directory_observed",
            "routing_hint": "https_direct",
            "browser_usable": True,
            "cx_public_key": CX_KEY_FIXTURE,
            "node_policy_manifest": {
                "jurisdiction": "DE",
                "training_use": "none",
                "evidence": "self_attested",
            },
        }
    ]
}


# ---------------------------------------------------------------------------
# Construction / validation (SDK-03, SDK-04, SDK-05)
# ---------------------------------------------------------------------------


def test_sdk04_rejects_oversized_timeout():
    with pytest.raises(ValueError, match="timeout_ms must be"):
        IicpClient(ClientConfig(timeout_ms=120_001))


def test_sdk03_rejects_invalid_intent_urn():
    client = IicpClient(ClientConfig(directory_url=DIRECTORY))
    with pytest.raises(IicpError) as exc_info:
        client.submit(TaskRequest(intent="bad-intent", payload={}))
    assert exc_info.value.code == "IICP-E001"
    assert not exc_info.value.retryable


def test_policy_refuses_prohibited_intent_before_discovery(respx_mock):
    client = IicpClient(ClientConfig(directory_url=DIRECTORY))
    with pytest.raises(IicpError) as exc_info:
        client.submit(TaskRequest(intent="urn:iicp:intent:social-scoring:score:v1", payload={}))
    assert exc_info.value.code == "IICP-POLICY-001"
    assert not exc_info.value.retryable
    assert len(respx_mock.calls) == 0


def test_sdk03_accepts_valid_intent_urn(respx_mock):
    respx_mock.get(DISCOVER_URL).mock(return_value=httpx.Response(200, json={"nodes": []}))
    client = IicpClient(ClientConfig(directory_url=DIRECTORY))
    with pytest.raises(IicpError) as exc_info:
        client.submit(TaskRequest(intent="urn:iicp:intent:llm:chat:v1", payload={}))
    assert exc_info.value.code == "IICP-E006"  # no nodes — URN was valid


# ---------------------------------------------------------------------------
# discover() (happy path + no-node case)
# ---------------------------------------------------------------------------


@respx.mock
def test_discover_returns_node_list():
    respx.get(DISCOVER_URL).mock(return_value=httpx.Response(200, json=GOOD_NODES))
    client = IicpClient(ClientConfig(directory_url=DIRECTORY))
    result = client.discover("urn:iicp:intent:llm:chat:v1")
    assert len(result.nodes) == 1
    assert result.nodes[0].node_id == "node-abc"
    assert result.nodes[0].score == 0.95
    # ADR-044 — health_label + exposure_mode parsed from discover
    assert result.nodes[0].health_label == "healthy"
    assert result.nodes[0].exposure_mode == "ipv4_public_direct"
    # #397 — transport parsed from discover
    assert result.nodes[0].transport == ["https", "iicp-native"]
    # Additive route-signal split parsed from discover
    assert result.nodes[0].directory_observed_reachable is True
    assert result.nodes[0].route_evidence == "directory_observed"
    assert result.nodes[0].routing_hint == "https_direct"
    assert result.nodes[0].browser_usable is True
    assert result.nodes[0].node_policy_manifest["jurisdiction"] == "DE"


@respx.mock
def test_discover_health_fields_default_none_against_old_directory():
    # A directory predating v1.10.0 omits the fields; parsing must not break.
    legacy = {"nodes": [{"node_id": "n1", "endpoint": NODE, "score": 0.5, "available": True, "region": "eu"}]}
    respx.get(DISCOVER_URL).mock(return_value=httpx.Response(200, json=legacy))
    client = IicpClient(ClientConfig(directory_url=DIRECTORY))
    result = client.discover("urn:iicp:intent:llm:chat:v1")
    assert result.nodes[0].health_label is None
    assert result.nodes[0].exposure_mode is None


@respx.mock
def test_discover_browser_usable_only_filters_http_ipv6_nodes():
    respx.get(DISCOVER_URL).mock(return_value=httpx.Response(200, json={
        "nodes": [
            {
                "node_id": "n-ipv6",
                "endpoint": "http://[2a0a:a543:df54::8ae]:9484",
                "score": 0.9,
                "available": True,
                "region": "eu",
                "routing_hint": "http_ipv6",
                "browser_usable": False,
            },
            {
                "node_id": "n-https",
                "endpoint": "https://relay.example.com",
                "score": 0.8,
                "available": True,
                "region": "eu",
                "routing_hint": "relay_service",
                "browser_usable": True,
            },
        ]
    }))
    client = IicpClient(ClientConfig(directory_url=DIRECTORY))
    result = client.discover(
        "urn:iicp:intent:llm:chat:v1",
        DiscoverOptions(browser_usable_only=True),
    )
    assert [n.node_id for n in result.nodes] == ["n-https"]


@respx.mock
def test_discover_empty_returns_empty_node_list():
    respx.get(DISCOVER_URL).mock(return_value=httpx.Response(200, json={"nodes": []}))
    client = IicpClient(ClientConfig(directory_url=DIRECTORY))
    result = client.discover("urn:iicp:intent:llm:chat:v1")
    assert result.nodes == []


# ---------------------------------------------------------------------------
# submit() — SDK-01: retry on transient errors
# ---------------------------------------------------------------------------


@respx.mock
def test_submit_happy_path():
    respx.get(DISCOVER_URL).mock(return_value=httpx.Response(200, json=GOOD_NODES))
    respx.post(TASK_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "task_id": "t-123",
                "status": "success",
                "result": {"answer": 42},
                "usage": {"total_tokens": 100},
            },
        )
    )
    client = IicpClient(ClientConfig(directory_url=DIRECTORY))
    resp = client.submit(
        TaskRequest(
            intent="urn:iicp:intent:llm:chat:v1",
            payload={"messages": []},
        )
    )
    assert resp.status == "success"
    assert resp.result == {"answer": 42}
    assert resp.metrics.node_id == "node-abc"
    assert resp.metrics.tokens_used == 100


@respx.mock
async def test_submit_sdk01_retries_transient(monkeypatch):
    """Transient 503 triggers a retry; second attempt succeeds."""

    async def _noop_sleep(_: float) -> None:
        pass

    monkeypatch.setattr("asyncio.sleep", _noop_sleep)
    respx.get(DISCOVER_URL).mock(return_value=httpx.Response(200, json=GOOD_NODES))
    respx.post(TASK_URL).mock(
        side_effect=[
            httpx.Response(503, json={"code": "IICP-E005", "message": "overload"}),
            httpx.Response(
                200,
                json={"task_id": "t-2", "status": "success", "result": {}, "usage": {}},
            ),
        ]
    )
    client = IicpClient(ClientConfig(directory_url=DIRECTORY, max_retries=3))
    resp = await client.submit_async(TaskRequest(intent="urn:iicp:intent:llm:chat:v1", payload={}))
    assert resp.status == "success"


@respx.mock
def test_submit_non_retryable_raises_immediately():
    respx.get(DISCOVER_URL).mock(return_value=httpx.Response(200, json=GOOD_NODES))
    respx.post(TASK_URL).mock(return_value=httpx.Response(401, json={"code": "IICP-E002", "message": "unauthorized"}))
    client = IicpClient(ClientConfig(directory_url=DIRECTORY))
    with pytest.raises(IicpError) as exc_info:
        client.submit(TaskRequest(intent="urn:iicp:intent:llm:chat:v1", payload={}))
    assert exc_info.value.http_status == 401
    assert not exc_info.value.retryable


# ---------------------------------------------------------------------------
# chat() — SDK-02: OpenAI-compatible output shape
# ---------------------------------------------------------------------------


@respx.mock
def test_chat_sdk02_openai_compat_shape():
    respx.get(DISCOVER_URL).mock(return_value=httpx.Response(200, json=GOOD_NODES))
    respx.post(TASK_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "task_id": "t-chat-1",
                "status": "success",
                "result": {
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "Hello!"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                    "model": "llama3",
                },
                "usage": {"total_tokens": 8},
            },
        )
    )
    client = IicpClient(ClientConfig(directory_url=DIRECTORY))
    resp = client.chat(
        [ChatMessage(role="user", content="Hi")],
        ChatOptions(model="llama3"),
    )
    assert resp.choices[0].message.content == "Hello!"
    assert resp.choices[0].finish_reason == "stop"
    assert resp.usage.total_tokens == 8
    assert resp.model == "llama3"
    assert resp.iicp_node_id == "node-abc"


# ---------------------------------------------------------------------------
# P0a (#360): mandatory encryption — no opt-out
# ---------------------------------------------------------------------------

def _cx_key(key_id: str = "cx-1"):
    import base64

    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    pub = X25519PrivateKey.generate().public_key().public_bytes_raw()
    return {"algorithm": "X25519", "key": base64.urlsafe_b64encode(pub).rstrip(b"=").decode(), "key_id": key_id}


_OK_TASK = {
    "task_id": "t", "status": "success",
    "result": {"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
               "usage": {"total_tokens": 1}, "model": "m"},
    "usage": {"total_tokens": 1},
}


@respx.mock
def test_p0a_always_encrypts_even_with_optout_off():
    """The client encrypts whenever the node advertises a cx_public_key — even with
    use_confidentiality=False. The opt-out no longer disables encryption (no opt-out)."""
    import json

    nodes = {"nodes": [dict(GOOD_NODES["nodes"][0], cx_public_key=_cx_key())]}
    respx.get(DISCOVER_URL).mock(return_value=httpx.Response(200, json=nodes))
    route = respx.post(TASK_URL).mock(return_value=httpx.Response(200, json=_OK_TASK))
    # use_confidentiality=False MUST NOT turn encryption off.
    client = IicpClient(ClientConfig(directory_url=DIRECTORY, use_confidentiality=False))
    client.chat([ChatMessage(role="user", content="secret")], ChatOptions(model="m"))
    body = json.loads(route.calls.last.request.content)
    assert "iicp_conf" in body, "client must encrypt when the node advertises a key"
    assert "payload" not in body, "plaintext payload must be absent when encrypting"
    assert body["iicp_conf"]["recipient_key_id"] == "cx-1"


@respx.mock
def test_p0a_encrypts_with_directory_public_key_alias():
    """Deprecated directory public_key alias still encrypts as cx_public_key."""
    import json

    nodes = {"nodes": [dict(GOOD_NODES["nodes"][0], cx_public_key=None, public_key=_cx_key())]}
    respx.get(DISCOVER_URL).mock(return_value=httpx.Response(200, json=nodes))
    route = respx.post(TASK_URL).mock(return_value=httpx.Response(200, json=_OK_TASK))
    client = IicpClient(ClientConfig(directory_url=DIRECTORY))
    client.chat([ChatMessage(role="user", content="secret")], ChatOptions(model="m"))
    body = json.loads(route.calls.last.request.content)
    assert "iicp_conf" in body, "client must encrypt when directory exposes deprecated public_key alias"
    assert "payload" not in body
    assert body["iicp_conf"]["recipient_key_id"] == "cx-1"


@respx.mock
def test_p0a_prefers_canonical_cx_public_key_when_both_fields_present():
    """A transitional directory may expose both names; prefer canonical cx_public_key."""
    import json

    nodes = {"nodes": [dict(
        GOOD_NODES["nodes"][0],
        cx_public_key=_cx_key("cx-canonical"),
        public_key=_cx_key("cx-alias"),
    )]}
    respx.get(DISCOVER_URL).mock(return_value=httpx.Response(200, json=nodes))
    route = respx.post(TASK_URL).mock(return_value=httpx.Response(200, json=_OK_TASK))
    client = IicpClient(ClientConfig(directory_url=DIRECTORY))
    client.chat([ChatMessage(role="user", content="secret")], ChatOptions(model="m"))
    body = json.loads(route.calls.last.request.content)
    assert "iicp_conf" in body
    assert "payload" not in body
    assert body["iicp_conf"]["recipient_key_id"] == "cx-canonical"


@respx.mock
def test_p0a_skips_keyless_node_and_uses_keyed_candidate():
    """A reachable but keyless high-score node must not receive plaintext when a keyed route exists."""
    import json

    nodes = {"nodes": [
        dict(GOOD_NODES["nodes"][0], node_id="keyless", score=0.99, cx_public_key=None),
        dict(
            GOOD_NODES["nodes"][0],
            node_id="keyed",
            endpoint=NODE_KEYED_FALLBACK,
            score=0.50,
            cx_public_key=_cx_key("cx-keyed"),
        ),
    ]}
    respx.get(DISCOVER_URL).mock(return_value=httpx.Response(200, json=nodes))
    keyless_route = respx.post(TASK_URL).mock(
        return_value=httpx.Response(500, json={"message": "should not be called"})
    )
    keyed_route = respx.post(TASK_URL_KEYED_FALLBACK).mock(return_value=httpx.Response(200, json=_OK_TASK))
    client = IicpClient(ClientConfig(directory_url=DIRECTORY, routing_epsilon=0.0))
    client.chat([ChatMessage(role="user", content="secret")], ChatOptions(model="m"))
    assert keyless_route.call_count == 0
    assert keyed_route.call_count == 1
    body = json.loads(keyed_route.calls.last.request.content)
    assert "iicp_conf" in body and "payload" not in body
    assert body["iicp_conf"]["recipient_key_id"] == "cx-keyed"


@respx.mock
def test_p0a_no_key_is_refused_by_default():
    """A node advertising no key must not receive plaintext by default."""

    nodes = {"nodes": [dict(GOOD_NODES["nodes"][0], cx_public_key=None)]}
    respx.get(DISCOVER_URL).mock(return_value=httpx.Response(200, json=nodes))
    route = respx.post(TASK_URL).mock(return_value=httpx.Response(200, json=_OK_TASK))
    client = IicpClient(ClientConfig(directory_url=DIRECTORY))
    with pytest.raises(IicpError) as exc_info:
        client.chat([ChatMessage(role="user", content="hi")], ChatOptions(model="m"))
    assert exc_info.value.code == "IICP-CX-REQUIRED"
    assert route.call_count == 0


@respx.mock
def test_p0a_no_key_plaintext_requires_explicit_env_opt_in(monkeypatch):
    """Plaintext fallback remains available only as an explicit transition/debug escape hatch."""
    import json

    monkeypatch.setenv("IICP_CX_ALLOW_PLAINTEXT", "1")
    nodes = {"nodes": [dict(GOOD_NODES["nodes"][0], cx_public_key=None)]}
    respx.get(DISCOVER_URL).mock(return_value=httpx.Response(200, json=nodes))
    route = respx.post(TASK_URL).mock(return_value=httpx.Response(200, json=_OK_TASK))
    client = IicpClient(ClientConfig(directory_url=DIRECTORY))
    client.chat([ChatMessage(role="user", content="hi")], ChatOptions(model="m"))
    body = json.loads(route.calls.last.request.content)
    assert "payload" in body and "iicp_conf" not in body


# ---------------------------------------------------------------------------
# SDK-06: node_token must not appear in IicpError message
# ---------------------------------------------------------------------------


@respx.mock
def test_sdk06_node_token_not_in_error():
    respx.get(DISCOVER_URL).mock(return_value=httpx.Response(200, json=GOOD_NODES))
    respx.post(TASK_URL).mock(return_value=httpx.Response(400, json={"message": "bad request"}))
    secret = "super-secret-token"
    client = IicpClient(ClientConfig(directory_url=DIRECTORY))
    with pytest.raises(IicpError) as exc_info:
        client.submit(
            TaskRequest(
                intent="urn:iicp:intent:llm:chat:v1",
                payload={},
                auth=TaskAuth(node_token=secret),
            )
        )
    err = exc_info.value
    assert secret not in err.message
    assert secret not in str(err)
    assert secret not in repr(err)


@respx.mock
def test_discover_passes_min_reputation_and_model():
    """SDK-04 parity: DiscoverOptions.min_reputation + model are sent as query params."""
    route = respx.get(DISCOVER_URL).mock(return_value=httpx.Response(200, json=GOOD_NODES))
    client = IicpClient(ClientConfig(directory_url=DIRECTORY))
    client.discover(
        "urn:iicp:intent:llm:chat:v1",
        DiscoverOptions(min_reputation=0.7, model="phi3:mini"),
    )
    url = str(route.calls[0].request.url)
    assert "min_reputation=0.7" in url
    assert "model=phi3%3Amini" in url or "model=phi3:mini" in url


# ---------------------------------------------------------------------------
# SDK-06: W3C traceparent propagation
# ---------------------------------------------------------------------------


@respx.mock
def test_sdk06_traceparent_sent_on_discover():
    """SDK-06: every outbound request carries a W3C traceparent header."""
    route = respx.get(DISCOVER_URL).mock(return_value=httpx.Response(200, json=GOOD_NODES))
    client = IicpClient(ClientConfig(directory_url=DIRECTORY))
    client.discover("urn:iicp:intent:llm:chat:v1")
    header = route.calls[0].request.headers.get("traceparent", "")
    # format: 00-<32hex>-<16hex>-01
    parts = header.split("-")
    assert len(parts) == 4, f"bad traceparent: {header!r}"
    assert parts[0] == "00"
    assert len(parts[1]) == 32
    assert len(parts[2]) == 16
    assert parts[3] == "01"


@respx.mock
def test_node_register_payload_spec_compliant(monkeypatch, tmp_path):
    """iter-1411: register payload matches spec/iicp-dir.md §3.1 — capabilities is an
    array of {intent, models, max_tokens} objects, not a flat intent string."""
    route = respx.post("https://iicp.test/v1/register").mock(
        return_value=httpx.Response(201, json={"node_token": "tok-1", "node_id": "n-1"})
    )
    monkeypatch.setenv("IICP_CX_KEY_DIR", str(tmp_path / "cx"))
    node = IicpNode(
        NodeConfig(
            node_id="n-1",
            endpoint="https://provider.example.com:8080",
            intent="urn:iicp:intent:llm:chat:v1",
            model="llama-3-8b",
            region="eu-central",
            directory_url="https://iicp.test",
            max_concurrent=2,
            tokens_per_min=2000,
            max_tokens=8192,
        )
    )
    asyncio.run(node.register())

    payload = json.loads(route.calls[0].request.content)
    assert payload["endpoint"] == "https://provider.example.com:8080"
    assert payload["region"] == "eu-central"
    assert payload["limits"] == {"max_concurrent": 2, "tokens_per_min": 2000}
    assert payload["capabilities"] == [
        {
            "intent": "urn:iicp:intent:llm:chat:v1",
            "models": ["llama-3-8b"],
            "max_tokens": 8192,
            # #408/ADR-046 — capability now declares input modalities (text-only here).
            "input_modalities": ["text"],
        }
    ]
    assert "transport_endpoint" not in payload  # not set → not sent
    assert payload["cx_public_key"]["algorithm"] == "X25519"
    assert payload["cx_public_key"]["encoding"] == "base64url"
    assert payload["cx_public_key"]["key_id"].startswith("cx-")
    assert "intent" not in payload  # spec rejects flat intent at top level


@respx.mock
def test_node_register_attaches_operator_delegation_when_set():
    """ADR-045 Phase A (#407) — when an operator delegation is configured, the
    SDK attaches it to the register payload (the directory then verifies it)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from iicp_client.delegation import issue_delegation, verify_delegation

    op = Ed25519PrivateKey.generate()
    delegation = issue_delegation(op, "n-1", ttl_seconds=3600)
    assert verify_delegation(delegation, "n-1")  # well-formed for this node

    route = respx.post("https://iicp.test/v1/register").mock(
        return_value=httpx.Response(201, json={"node_token": "tok-1", "node_id": "n-1"})
    )
    node = IicpNode(
        NodeConfig(
            node_id="n-1",
            endpoint="https://provider.example.com:8080",
            intent="urn:iicp:intent:llm:chat:v1",
            model="llama-3-8b",
            directory_url="https://iicp.test",
            operator_delegation=delegation,
        )
    )
    asyncio.run(node.register())

    payload = json.loads(route.calls[0].request.content)
    assert payload["operator_delegation"] == delegation


@respx.mock
def test_node_register_includes_backend_when_set():
    """#414 — the detected backend server flavor is advertised at register."""
    route = respx.post("https://iicp.test/v1/register").mock(
        return_value=httpx.Response(201, json={"node_token": "tok-1", "node_id": "n-b"})
    )
    node = IicpNode(
        NodeConfig(
            node_id="n-b",
            endpoint="https://provider.example.com:8080",
            intent="urn:iicp:intent:llm:chat:v1",
            model="llama-3-8b",
            backend="ollama",
            directory_url="https://iicp.test",
        )
    )
    asyncio.run(node.register())
    payload = json.loads(route.calls[0].request.content)
    assert payload["backend"] == "ollama"


def test_detect_backend_flavor_backend_type_authoritative():
    """#414 — non-OpenAI dialects classify by backend_type without probing."""
    from iicp_client.cli import _detect_backend_flavor

    assert _detect_backend_flavor("http://x", "", "anthropic") == "anthropic"
    assert _detect_backend_flavor("http://x", "", "vllm") == "vllm"
    assert _detect_backend_flavor("http://x", "", "llamacpp") == "llamacpp"


@respx.mock
def test_node_register_omits_operator_delegation_when_absent():
    route = respx.post("https://iicp.test/v1/register").mock(
        return_value=httpx.Response(201, json={"node_token": "t", "node_id": "n-2"})
    )
    node = IicpNode(
        NodeConfig(
            node_id="n-2",
            endpoint="https://p.example.com:8080",
            intent="urn:iicp:intent:llm:chat:v1",
            model="m",
            directory_url="https://iicp.test",
        )
    )
    asyncio.run(node.register())
    payload = json.loads(route.calls[0].request.content)
    assert "operator_delegation" not in payload  # back-compat: absent unless set


@respx.mock
def test_heartbeat_answers_liveness_challenge():
    """ADR-047 Part A (#411) — the node HMACs the directory's nonce with its
    node_hmac_key and returns it on the next beat (no challenge_response on the
    first beat, since there's no prior nonce)."""
    import hashlib
    import hmac as _hmac

    route = respx.post("https://iicp.test/v1/heartbeat").mock(
        return_value=httpx.Response(200, json={"ok": True, "challenge": "nonce-abc"})
    )
    node = IicpNode(
        NodeConfig(
            node_id="n-1",
            endpoint="https://p.example.com:8080",
            intent="urn:iicp:intent:llm:chat:v1",
            model="m",
            directory_url="https://iicp.test",
            node_hmac_key="secret-key",
        )
    )
    asyncio.run(node.heartbeat("tok"))  # beat 1 → captures nonce, no answer yet
    asyncio.run(node.heartbeat("tok"))  # beat 2 → answers the nonce

    p1 = json.loads(route.calls[0].request.content)
    p2 = json.loads(route.calls[1].request.content)
    assert "challenge_response" not in p1
    expected = _hmac.new(b"secret-key", b"nonce-abc", hashlib.sha256).hexdigest()
    assert p2["challenge_response"] == expected


@respx.mock
def test_heartbeat_payload_includes_available_true():
    """The heartbeat body MUST carry an explicit `available: true` boolean (not only
    the `status: "available"` string). The directory keys discover eligibility off the
    `available` field; sending it lets a node that briefly went dormant be restored on
    the next beat, robust even against directory builds older than v1.10.17."""
    route = respx.post("https://iicp.test/v1/heartbeat").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    node = IicpNode(
        NodeConfig(
            node_id="n-1",
            endpoint="https://p.example.com:8080",
            intent="urn:iicp:intent:llm:chat:v1",
            model="m",
            directory_url="https://iicp.test",
        )
    )
    asyncio.run(node.heartbeat("tok"))
    payload = json.loads(route.calls[0].request.content)
    assert payload["available"] is True
    assert payload["status"] == "available"


@respx.mock
def test_heartbeat_payload_reports_unavailable_during_tunnel_recovery():
    route = respx.post("https://iicp.test/v1/heartbeat").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    node = IicpNode(
        NodeConfig(
            node_id="n-1",
            endpoint="https://p.example.com:8080",
            intent="urn:iicp:intent:llm:chat:v1",
            model="m",
            directory_url="https://iicp.test",
        )
    )
    node.set_runtime_available(False)
    asyncio.run(node.heartbeat("tok"))
    payload = json.loads(route.calls[0].request.content)
    assert payload["available"] is False
    assert payload["status"] == "recovering"


@respx.mock
def test_node_register_includes_transport_endpoint_when_set():
    """spec/iicp-dir.md v0.7.0: when transport_endpoint is configured, the SDK MUST
    advertise it so clients can prefer native IICP binary transport over HTTP."""
    route = respx.post("https://iicp.test/v1/register").mock(
        return_value=httpx.Response(201, json={"node_token": "tok-2", "node_id": "n-2"})
    )
    node = IicpNode(
        NodeConfig(
            node_id="n-2",
            endpoint="https://provider.example.com:8080",
            intent="urn:iicp:intent:llm:chat:v1",
            model="qwen2.5:0.5b",
            directory_url="https://iicp.test",
            transport_endpoint="iicp://provider.example.com:9484",
        )
    )
    asyncio.run(node.register())

    payload = json.loads(route.calls[0].request.content)
    assert payload["transport_endpoint"] == "iicp://provider.example.com:9484"
    # endpoint is the HTTP control plane; transport_endpoint is the native data plane
    assert payload["endpoint"].startswith("http")


@respx.mock
def test_node_register_legacy_capabilities_list_folds_into_models():
    """Back-compat: pre-iter-1411 callers passed `capabilities: list[str]` as
    extra model names. The new payload shape folds them into the models array
    so existing operator configs keep working without an API break."""
    route = respx.post("https://iicp.test/v1/register").mock(
        return_value=httpx.Response(201, json={"node_token": "tok-3", "node_id": "n-3"})
    )
    node = IicpNode(
        NodeConfig(
            node_id="n-3",
            endpoint="https://provider.example.com:8080",
            intent="urn:iicp:intent:llm:chat:v1",
            model="llama-3-8b",
            capabilities=["mistral-7b", "phi-3-mini"],
            directory_url="https://iicp.test",
        )
    )
    asyncio.run(node.register())

    payload = json.loads(route.calls[0].request.content)
    assert set(payload["capabilities"][0]["models"]) == {"llama-3-8b", "mistral-7b", "phi-3-mini"}


@respx.mock
def test_node_register_includes_nat_observability_when_set():
    """iter-1426: transport_method / nat_type / transport_metadata appear in the
    register payload when set on NodeConfig (e.g. via apply_nat_profile)."""
    route = respx.post("https://iicp.test/v1/register").mock(
        return_value=httpx.Response(201, json={"node_token": "tok-nat", "node_id": "n-nat"})
    )
    node = IicpNode(
        NodeConfig(
            node_id="n-nat",
            endpoint="https://provider.example.com:8080",
            intent="urn:iicp:intent:llm:chat:v1",
            model="qwen2.5:0.5b",
            directory_url="https://iicp.test",
            transport_endpoint="iicp://provider.example.com:9484",
            transport_method="upnp_mapped",
            nat_type="full_cone",
            transport_metadata={"tier": 1, "detection_log_tail": ["upnp ok"]},
        )
    )
    asyncio.run(node.register())

    payload = json.loads(route.calls[0].request.content)
    assert payload["transport_method"] == "upnp_mapped"
    assert payload["nat_type"] == "full_cone"
    assert payload["transport_metadata"] == {"tier": 1, "detection_log_tail": ["upnp ok"]}


@respx.mock
def test_apply_nat_profile_populates_fields_from_nat_profile():
    """iter-1426: IicpNode.apply_nat_profile(profile) sets transport_endpoint +
    observability fields from a detect_nat() result. After apply, register()
    must include them."""
    from iicp_client.nat_detection import NatProfile

    profile = NatProfile(
        tier=1,
        transport_method="upnp_mapped",
        public_endpoint="http://203.0.113.5:8080",
        transport_endpoint="iicp://203.0.113.5:9484",
        detection_log=["tier-1: UPnP mapped 8080 → http://203.0.113.5:8080"],
    )
    route = respx.post("https://iicp.test/v1/register").mock(
        return_value=httpx.Response(201, json={"node_token": "tok-applied", "node_id": "n-applied"})
    )
    node = IicpNode(
        NodeConfig(
            node_id="n-applied",
            endpoint="http://placeholder.example.com:8080",  # overridden by apply_nat_profile
            intent="urn:iicp:intent:llm:chat:v1",
            model="q",
            directory_url="https://iicp.test",
        )
    )
    node.apply_nat_profile(profile)
    asyncio.run(node.register())

    payload = json.loads(route.calls[0].request.content)
    # endpoint overridden by the discovered public URL
    assert payload["endpoint"] == "http://203.0.113.5:8080"
    assert payload["transport_endpoint"] == "iicp://203.0.113.5:9484"
    assert payload["transport_method"] == "upnp_mapped"
    assert payload["nat_type"] == "unknown"  # set by helper when none provided
    assert payload["transport_metadata"]["tier"] == 1
    assert payload["transport_metadata"]["detection_log_tail"] == ["tier-1: UPnP mapped 8080 → http://203.0.113.5:8080"]


@respx.mock
def test_apply_nat_profile_unreachable_does_not_overwrite_endpoint():
    """iter-1426: a tier-4 (unreachable) profile must NOT silently overwrite
    a previously-set endpoint with nothing — operator might still have a
    valid manual endpoint configured."""
    from iicp_client.nat_detection import NatProfile

    profile = NatProfile(
        tier=4,
        transport_method="unreachable",
        public_endpoint=None,
        operator_guidance="install upnpclient",
    )
    route = respx.post("https://iicp.test/v1/register").mock(
        return_value=httpx.Response(201, json={"node_token": "tok-keep", "node_id": "n-keep"})
    )
    node = IicpNode(
        NodeConfig(
            node_id="n-keep",
            endpoint="https://manual-endpoint.example.com:8080",
            intent="urn:iicp:intent:llm:chat:v1",
            model="q",
            directory_url="https://iicp.test",
        )
    )
    node.apply_nat_profile(profile)
    asyncio.run(node.register())

    payload = json.loads(route.calls[0].request.content)
    # endpoint preserved — apply_nat_profile only overwrites on reachable
    assert payload["endpoint"] == "https://manual-endpoint.example.com:8080"
    # transport_method "unreachable" filtered out — not surfaced to directory
    assert "transport_method" not in payload


@respx.mock
def test_sdk06_traceparent_shared_across_submit():
    """SDK-06: discover + node POST share the same trace-id within one submit()."""
    disc_route = respx.get(DISCOVER_URL).mock(return_value=httpx.Response(200, json=GOOD_NODES))
    task_route = respx.post(TASK_URL).mock(
        return_value=httpx.Response(200, json={"task_id": "t1", "status": "success", "result": {}})
    )
    client = IicpClient(ClientConfig(directory_url=DIRECTORY))
    client.submit(TaskRequest(intent="urn:iicp:intent:llm:chat:v1", payload={}))
    disc_tp = disc_route.calls[0].request.headers.get("traceparent", "")
    task_tp = task_route.calls[0].request.headers.get("traceparent", "")
    # both must have the same trace-id (index 1)
    assert disc_tp.split("-")[1] == task_tp.split("-")[1], f"trace-id mismatch: discover={disc_tp!r} task={task_tp!r}"


# ---------------------------------------------------------------------------
# ε-greedy provider selection (R4 / #486)
# ---------------------------------------------------------------------------

_MULTI_NODE_IPS = ["1.2.3.1", "1.2.3.2", "1.2.3.3", "1.2.3.4", "1.2.3.5"]
_MULTI_NODES = {
    "nodes": [
        {"node_id": f"node-{i:02d}", "endpoint": f"https://{ip}:9484",
         "score": round(1.0 - (i - 1) * 0.1, 1), "available": True, "region": "eu-west",
         "cx_public_key": CX_KEY_FIXTURE}
        for i, ip in enumerate(_MULTI_NODE_IPS, start=1)
    ]
}


def _make_multi_node_mock():
    """Build respx node mocks for all 5 test nodes."""
    return [
        respx.post(f"https://{ip}:9484/v1/task").mock(
            return_value=httpx.Response(200, json={"task_id": "t1", "status": "success", "result": {}})
        )
        for ip in _MULTI_NODE_IPS
    ]


@respx.mock
def test_epsilon_greedy_explore_picks_non_top_node():
    """With ε=1.0 (always explore), first candidate is NOT always node-01 (R4 / #486).

    This test fails without the ε-greedy implementation because with ε=0 the client
    always picks node-01 (directory-sorted top), never exploring lower-ranked nodes.
    """
    respx.get(f"{DIRECTORY}/v1/discover").mock(return_value=httpx.Response(200, json=_MULTI_NODES))
    task_routes = _make_multi_node_mock()
    cfg = ClientConfig(directory_url=DIRECTORY, routing_epsilon=1.0)  # always explore
    client = IicpClient(cfg)

    # With ε=1.0 and 5 nodes, run 20 submits; statistically >99% chance we hit a non-top-1 node.
    for _ in range(20):
        client.submit(TaskRequest(intent="urn:iicp:intent:llm:chat:v1", payload={}))

    hit_node_ids = {f"node-{i+1:02d}" for i, r in enumerate(task_routes) if r.call_count > 0}

    # With 20 draws from 5 nodes at ε=1.0, probability of hitting only node-01 = (1/5)^20 ≈ 10^-14
    assert len(hit_node_ids) > 1, (
        f"ε-greedy not working: only called {hit_node_ids} — exploration never fired"
    )


@respx.mock
def test_epsilon_zero_always_picks_top_node():
    """With ε=0.0 (no exploration), always picks the directory-sorted top node (R4 / #486).

    This test verifies the greedy (non-explore) path works correctly.
    """
    respx.get(f"{DIRECTORY}/v1/discover").mock(return_value=httpx.Response(200, json=_MULTI_NODES))
    task_routes = _make_multi_node_mock()
    cfg = ClientConfig(directory_url=DIRECTORY, routing_epsilon=0.0)
    client = IicpClient(cfg)

    for _ in range(5):
        client.submit(TaskRequest(intent="urn:iicp:intent:llm:chat:v1", payload={}))

    # node-01 (highest score) should have been called 5 times; others 0
    assert task_routes[0].call_count == 5, f"top node call_count={task_routes[0].call_count}, expected 5"
    for r in task_routes[1:]:
        assert r.call_count == 0, f"lower-ranked node called {r.call_count} times with ε=0.0"


@respx.mock
def test_epsilon_env_override(monkeypatch):
    """IICP_ROUTING_EPSILON env var overrides config default (R4 / #486)."""
    monkeypatch.setenv("IICP_ROUTING_EPSILON", "0.0")
    respx.get(f"{DIRECTORY}/v1/discover").mock(return_value=httpx.Response(200, json=_MULTI_NODES))
    task_routes = _make_multi_node_mock()
    # Default config — epsilon=0.05, but env sets 0.0
    client = IicpClient(ClientConfig(directory_url=DIRECTORY))
    assert client._cfg.routing_epsilon == 0.0

    for _ in range(5):
        client.submit(TaskRequest(intent="urn:iicp:intent:llm:chat:v1", payload={}))

    assert task_routes[0].call_count == 5, "env IICP_ROUTING_EPSILON=0.0 should force top-only selection"


@respx.mock
def test_node_register_payload_includes_relay_capable():
    """relay_capable=True must appear in the register payload (0.7.45).
    Pre-fix: relay_capable was never sent to the directory → it wouldn't show
    up in /v1/discover, making the flag useless. Post-fix: it's included."""
    route = respx.post("https://iicp.test/v1/register").mock(
        return_value=httpx.Response(201, json={"node_token": "tok-1", "node_id": "n-1"})
    )
    node = IicpNode(
        NodeConfig(
            node_id="n-1",
            endpoint="https://relay.example.com:9484",
            intent="urn:iicp:intent:llm:chat:v1",
            model="llama-3-8b",
            directory_url="https://iicp.test",
            relay_capable=True,
            relay_accept_port=9490,
        )
    )
    asyncio.run(node.register())
    payload = json.loads(route.calls[0].request.content)
    assert payload["relay_capable"] is True, "relay_capable must be in register payload"
    assert payload["relay_accept_port"] == 9490, "relay_accept_port must be in register payload"


@respx.mock
def test_node_register_payload_omits_relay_capable_when_false():
    """relay_capable=False (default) must NOT add relay_capable to the payload."""
    route = respx.post("https://iicp.test/v1/register").mock(
        return_value=httpx.Response(201, json={"node_token": "tok-1", "node_id": "n-1"})
    )
    node = IicpNode(
        NodeConfig(
            node_id="n-1",
            endpoint="https://provider.example.com:9484",
            intent="urn:iicp:intent:llm:chat:v1",
            model="llama-3-8b",
            directory_url="https://iicp.test",
        )
    )
    asyncio.run(node.register())
    payload = json.loads(route.calls[0].request.content)
    assert "relay_capable" not in payload, "relay_capable must not appear when not set"


def test_routing_strategy_env_overrides(monkeypatch):
    monkeypatch.setenv("IICP_ROUTING_STRATEGY", "softmax_top_k")
    monkeypatch.setenv("IICP_ROUTING_TOP_K", "2")
    monkeypatch.setenv("IICP_ROUTING_SOFTMAX_TAU", "0.02")
    client = IicpClient(ClientConfig(directory_url=DIRECTORY))
    assert client._cfg.routing_strategy == "softmax_top_k"
    assert client._cfg.routing_top_k == 2
    assert client._cfg.routing_softmax_tau == 0.02
