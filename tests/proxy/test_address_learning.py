"""Tests for Implicit Address Learning (DIR-ADDR-02, DIR-ADDR-05, proxy client)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from iicp_client.proxy.address_state import AddressState
from iicp_client.proxy.clients.directory import DirectoryClient, check_observed_ip_vs_endpoint

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(base_url: str = "https://dir.example.com") -> DirectoryClient:
    return DirectoryClient(base_url=base_url, timeout_ms=5000)


def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


def _patch_method(method: str, response: MagicMock):
    mock_instance = AsyncMock()
    setattr(mock_instance, method, AsyncMock(return_value=response))
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    return patch("iicp_client.proxy.clients.directory.httpx.AsyncClient", return_value=mock_ctx)


# ---------------------------------------------------------------------------
# AddressState
# ---------------------------------------------------------------------------

def test_address_state_defaults_to_none():
    state = AddressState()
    assert state.observed_source_ip is None
    assert state.node_id is None
    assert state.endpoint is None


def test_update_from_ack_sets_ip_and_node_id():
    state = AddressState()
    state.update_from_ack({
        "node_id": "abc-123",
        "node_token": "tok",
        "observed_source_ip": "203.0.113.10",
        "directory": "https://iicp.network",
    })
    assert state.observed_source_ip == "203.0.113.10"
    assert state.node_id == "abc-123"
    assert state.endpoint is None  # not in ACK


def test_update_from_me_sets_all_fields():
    state = AddressState()
    state.update_from_me({
        "node_id": "abc-123",
        "observed_source_ip": "203.0.113.42",
        "endpoint": "https://node.example.com",
    })
    assert state.observed_source_ip == "203.0.113.42"
    assert state.node_id == "abc-123"
    assert state.endpoint == "https://node.example.com"


def test_update_from_me_handles_missing_keys():
    state = AddressState()
    state.update_from_me({})
    assert state.observed_source_ip is None
    assert state.node_id is None
    assert state.endpoint is None


# ---------------------------------------------------------------------------
# DirectoryClient.me()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_me_sends_bearer_token():
    ack = {
        "node_id": "abc-123",
        "observed_source_ip": "203.0.113.42",
        "endpoint": "https://node.example.com",
    }
    mock_instance = AsyncMock()
    mock_instance.get = AsyncMock(return_value=_mock_response(ack))
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("iicp_client.proxy.clients.directory.httpx.AsyncClient", return_value=mock_ctx):
        result = await _make_client().me("my-secret-token")

    call_kwargs = mock_instance.get.call_args.kwargs
    assert call_kwargs["headers"]["Authorization"] == "Bearer my-secret-token"
    assert result["observed_source_ip"] == "203.0.113.42"


@pytest.mark.asyncio
async def test_me_returns_full_body():
    body = {
        "node_id": "abc-123",
        "observed_source_ip": "203.0.113.42",
        "endpoint": "https://node.example.com",
    }
    with _patch_method("get", _mock_response(body)):
        result = await _make_client().me("tok")
    assert result == body


@pytest.mark.asyncio
async def test_me_raises_on_401():
    with _patch_method("get", _mock_response({}, status_code=401)):
        with pytest.raises(httpx.HTTPStatusError):
            await _make_client().me("bad-token")


# ---------------------------------------------------------------------------
# DirectoryClient.register()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_posts_payload_and_returns_ack():
    ack = {
        "node_id": "abc-123",
        "node_token": "tok",
        "observed_source_ip": "203.0.113.42",
        "directory": "https://iicp.network",
    }
    mock_instance = AsyncMock()
    mock_instance.post = AsyncMock(return_value=_mock_response(ack, 201))
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    payload = {"endpoint": "https://node.example.com", "region": "eu-central"}
    with patch("iicp_client.proxy.clients.directory.httpx.AsyncClient", return_value=mock_ctx):
        result = await _make_client().register(payload)

    mock_instance.post.assert_awaited_once()
    assert result["observed_source_ip"] == "203.0.113.42"


# ---------------------------------------------------------------------------
# check_observed_ip_vs_endpoint()
# ---------------------------------------------------------------------------

def test_returns_true_when_ip_matches(monkeypatch):
    monkeypatch.setattr(
        "iicp_client.proxy.clients.directory.socket.gethostbyname",
        lambda host: "203.0.113.42",
    )
    result = check_observed_ip_vs_endpoint("203.0.113.42", "https://node.example.com")
    assert result is True


def test_returns_false_and_warns_when_ip_mismatches(monkeypatch, caplog):
    import logging
    monkeypatch.setattr(
        "iicp_client.proxy.clients.directory.socket.gethostbyname",
        lambda host: "10.0.0.1",
    )
    with caplog.at_level(logging.WARNING, logger="iicp_client.proxy.clients.directory"):
        result = check_observed_ip_vs_endpoint("203.0.113.42", "https://node.example.com")

    assert result is False
    assert "does not match endpoint host" in caplog.text


def test_returns_true_on_empty_ip():
    assert check_observed_ip_vs_endpoint("", "https://node.example.com") is True


def test_returns_true_on_empty_endpoint():
    assert check_observed_ip_vs_endpoint("203.0.113.42", "") is True


def test_returns_true_on_resolve_exception(monkeypatch):
    monkeypatch.setattr(
        "iicp_client.proxy.clients.directory.socket.gethostbyname",
        lambda host: (_ for _ in ()).throw(OSError("no resolve")),
    )
    # Should not raise, should return True (non-fatal)
    result = check_observed_ip_vs_endpoint("203.0.113.42", "https://node.example.com")
    assert result is True
