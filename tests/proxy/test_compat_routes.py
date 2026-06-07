"""HTTP endpoint smoke tests for Ollama-compat and Anthropic-compat routes (#278/#279).

Tests the static info endpoints and the error propagation path through each surface.
Translator-level logic is covered by test_ollama_compat.py / test_anthropic_compat.py.
These tests verify routes are registered, shapes are correct, and error formats diverge
as specified (Ollama plain-string error vs Anthropic typed error object).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from iicp_client.proxy.config import ProxyConfig
from iicp_client.proxy.main import create_app


@pytest.fixture(scope="module")
def client():
    cfg = ProxyConfig(
        directory_url="http://127.0.0.1:19999",  # unreachable — forces IICP-E033 path
        node_token_env="IICP_NODE_TOKEN",
        host="127.0.0.1",
        port=9483,
    )
    # Context manager form triggers lifespan so app.state is populated before handlers run.
    with TestClient(create_app(cfg), raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# Ollama static endpoints
# ---------------------------------------------------------------------------

def test_api_version_returns_200(client: TestClient) -> None:
    """GET /api/version returns 200 with a version field."""
    r = client.get("/api/version")
    assert r.status_code == 200
    assert "version" in r.json()


def test_api_tags_returns_200(client: TestClient) -> None:
    """GET /api/tags returns 200 with a models array."""
    r = client.get("/api/tags")
    assert r.status_code == 200
    data = r.json()
    assert "models" in data
    assert isinstance(data["models"], list)


def test_api_tags_contains_iicp_model(client: TestClient) -> None:
    """GET /api/tags static list includes the 'iicp' sentinel model."""
    r = client.get("/api/tags")
    names = [m["name"] for m in r.json()["models"]]
    assert "iicp" in names


# ---------------------------------------------------------------------------
# Anthropic static endpoints
# ---------------------------------------------------------------------------

def test_v1_models_returns_200(client: TestClient) -> None:
    """GET /v1/models returns 200 with a data array (Anthropic SDK validation)."""
    r = client.get("/v1/models")
    assert r.status_code == 200
    data = r.json()
    assert "data" in data
    assert isinstance(data["data"], list)


def test_v1_models_contains_iicp_model(client: TestClient) -> None:
    """GET /v1/models static list includes the 'iicp' sentinel model."""
    r = client.get("/v1/models")
    ids = [m["id"] for m in r.json()["data"]]
    assert "iicp" in ids


# ---------------------------------------------------------------------------
# Ollama routing endpoints — IICP-E033 error path (no nodes available)
# ---------------------------------------------------------------------------

def test_api_chat_returns_502_when_no_nodes(client: TestClient) -> None:
    """POST /api/chat with unreachable directory returns 502 (IICP-E033 path)."""
    r = client.post(
        "/api/chat",
        json={"model": "iicp", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 502


def test_api_chat_error_is_plain_string(client: TestClient) -> None:
    """Ollama error format: {'error': '<string>'} not the Anthropic typed object."""
    r = client.post(
        "/api/chat",
        json={"model": "iicp", "messages": [{"role": "user", "content": "hi"}]},
    )
    body = r.json()
    assert "error" in body
    assert isinstance(body["error"], str)


def test_api_generate_returns_502_when_no_nodes(client: TestClient) -> None:
    """POST /api/generate with unreachable directory returns 502."""
    r = client.post("/api/generate", json={"model": "iicp", "prompt": "hello"})
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# Anthropic routing endpoint — IICP-E033 error path
# ---------------------------------------------------------------------------

def test_v1_messages_returns_502_when_no_nodes(client: TestClient) -> None:
    """POST /v1/messages with unreachable directory returns 502."""
    r = client.post(
        "/v1/messages",
        json={
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 502


def test_v1_messages_error_is_typed_object(client: TestClient) -> None:
    """Anthropic error format: {'type': 'error', 'error': {'type': ..., 'message': ...}}."""
    r = client.post(
        "/v1/messages",
        json={
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    body = r.json()
    assert body.get("type") == "error"
    assert "error" in body
    assert "type" in body["error"]
    assert "message" in body["error"]


# ---------------------------------------------------------------------------
# Streaming error paths — stream=True with unreachable directory still returns
# 502 JSON (errors are never streamed; only success responses are streamed).
# ---------------------------------------------------------------------------

def test_api_chat_stream_true_error_returns_502(client: TestClient) -> None:
    """stream=True with no nodes: error path still returns 502 JSON (not a stream)."""
    r = client.post(
        "/api/chat",
        json={"model": "iicp", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert r.status_code == 502
    # Error response is always plain JSON regardless of stream flag
    assert isinstance(r.json()["error"], str)


def test_api_generate_stream_true_error_returns_502(client: TestClient) -> None:
    """stream=True on /api/generate with no nodes returns 502."""
    r = client.post("/api/generate", json={"model": "iicp", "prompt": "hi", "stream": True})
    assert r.status_code == 502


def test_v1_messages_stream_true_error_returns_502(client: TestClient) -> None:
    """stream=True on /v1/messages with no nodes returns 502 JSON (not SSE)."""
    r = client.post(
        "/v1/messages",
        json={
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert r.status_code == 502
    body = r.json()
    assert body.get("type") == "error"


def test_api_chat_stream_false_returns_json(client: TestClient) -> None:
    """stream=False on /api/chat returns application/json error (not NDJSON)."""
    r = client.post(
        "/api/chat",
        json={"model": "iicp", "messages": [{"role": "user", "content": "hi"}], "stream": False},
    )
    assert r.status_code == 502
    assert "error" in r.json()


# ---------------------------------------------------------------------------
# Streaming happy paths — stream=True with a successful IICP response returns
# the correct Content-Type and body structure.
# ---------------------------------------------------------------------------

_SUCCESS_RESPONSE = {
    "status": "success",
    "result": {
        "choices": [{"message": {"role": "assistant", "content": "Hello from IICP!"}}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 5},
    },
}


def test_api_chat_stream_true_success_returns_ndjson(client: TestClient) -> None:
    """stream=True with a successful response returns application/x-ndjson."""
    from unittest.mock import AsyncMock, patch

    with patch(
        "iicp_client.proxy.ollama_compat.server._execute_iicp",
        new=AsyncMock(return_value=_SUCCESS_RESPONSE),
    ):
        r = client.post(
            "/api/chat",
            json={"model": "iicp", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        )
    assert r.status_code == 200
    assert "application/x-ndjson" in r.headers.get("content-type", "")
    import json
    line = r.text.strip()
    data = json.loads(line)
    assert data.get("done") is True
    assert "message" in data


def test_api_generate_stream_true_success_returns_ndjson(client: TestClient) -> None:
    """stream=True on /api/generate with a successful response returns application/x-ndjson."""
    from unittest.mock import AsyncMock, patch

    with patch(
        "iicp_client.proxy.ollama_compat.server._execute_iicp",
        new=AsyncMock(return_value=_SUCCESS_RESPONSE),
    ):
        r = client.post(
            "/api/generate",
            json={"model": "iicp", "prompt": "hi", "stream": True},
        )
    assert r.status_code == 200
    assert "application/x-ndjson" in r.headers.get("content-type", "")
    import json
    data = json.loads(r.text.strip())
    assert data.get("done") is True
    assert "response" in data


def test_v1_messages_stream_true_success_returns_sse(client: TestClient) -> None:
    """stream=True on /v1/messages with a successful response returns text/event-stream."""
    from unittest.mock import AsyncMock, patch

    with patch(
        "iicp_client.proxy.anthropic_compat.server._execute_iicp",
        new=AsyncMock(return_value=(_SUCCESS_RESPONSE, "test-task-id")),
    ):
        r = client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")
    # The body must contain all six required SSE event types
    body = r.text
    for event_name in (
        "message_start", "content_block_start", "content_block_delta",
        "content_block_stop", "message_delta", "message_stop",
    ):
        assert event_name in body, f"SSE event '{event_name}' missing from response"
