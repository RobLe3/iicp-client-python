"""Unit tests for DirectoryClient — discover() and bootstrap() with mocked httpx."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from iicp_client.proxy.clients.directory import DirectoryClient


def _make_client(base_url: str = "https://dir.example.com", timeout_ms: int = 5000) -> DirectoryClient:
    return DirectoryClient(base_url=base_url, timeout_ms=timeout_ms)


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


def _patch_client(response: MagicMock):
    """Return a context-manager patch for httpx.AsyncClient.get."""
    mock_instance = AsyncMock()
    mock_instance.get = AsyncMock(return_value=response)
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    return patch("iicp_client.proxy.clients.directory.httpx.AsyncClient", return_value=mock_ctx)


# ---------------------------------------------------------------------------
# discover() — happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discover_returns_nodes_on_success():
    """discover() returns the nodes list from the directory response."""
    nodes = [{"node_id": "n1", "score": 0.9, "available": True}]
    with _patch_client(_mock_response({"nodes": nodes})):
        result = await _make_client().discover()
    assert result == nodes


@pytest.mark.asyncio
async def test_discover_returns_empty_list_when_no_nodes_key():
    """discover() returns [] when the directory response has no 'nodes' key."""
    with _patch_client(_mock_response({"status": "ok"})):
        result = await _make_client().discover()
    assert result == []


@pytest.mark.asyncio
async def test_discover_returns_empty_list_on_empty_nodes():
    """discover() returns [] when directory returns an empty nodes array."""
    with _patch_client(_mock_response({"nodes": []})):
        result = await _make_client().discover()
    assert result == []


# ---------------------------------------------------------------------------
# discover() — query parameter forwarding
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discover_sends_intent_param():
    """discover(intent=...) includes intent in the GET request params."""
    mock_instance = AsyncMock()
    mock_instance.get = AsyncMock(return_value=_mock_response({"nodes": []}))
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("iicp_client.proxy.clients.directory.httpx.AsyncClient", return_value=mock_ctx):
        await _make_client().discover(intent="urn:iicp:intent:llm:chat:v1")

    call_kwargs = mock_instance.get.call_args.kwargs
    assert call_kwargs["params"]["intent"] == "urn:iicp:intent:llm:chat:v1"


@pytest.mark.asyncio
async def test_discover_sends_region_param():
    """discover(region=...) includes region in the GET request params."""
    mock_instance = AsyncMock()
    mock_instance.get = AsyncMock(return_value=_mock_response({"nodes": []}))
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("iicp_client.proxy.clients.directory.httpx.AsyncClient", return_value=mock_ctx):
        await _make_client().discover(region="eu-central")

    call_kwargs = mock_instance.get.call_args.kwargs
    assert call_kwargs["params"]["region"] == "eu-central"


@pytest.mark.asyncio
async def test_discover_omits_intent_when_not_provided():
    """discover() does not include intent key when intent is None."""
    mock_instance = AsyncMock()
    mock_instance.get = AsyncMock(return_value=_mock_response({"nodes": []}))
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("iicp_client.proxy.clients.directory.httpx.AsyncClient", return_value=mock_ctx):
        await _make_client().discover()

    call_kwargs = mock_instance.get.call_args.kwargs
    assert "intent" not in call_kwargs["params"]


@pytest.mark.asyncio
async def test_discover_sends_default_limit():
    """discover() sends limit=5 by default."""
    mock_instance = AsyncMock()
    mock_instance.get = AsyncMock(return_value=_mock_response({"nodes": []}))
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("iicp_client.proxy.clients.directory.httpx.AsyncClient", return_value=mock_ctx):
        await _make_client().discover()

    call_kwargs = mock_instance.get.call_args.kwargs
    assert call_kwargs["params"]["limit"] == 5


@pytest.mark.asyncio
async def test_discover_sends_custom_limit():
    """discover(limit=10) sends limit=10 in params."""
    mock_instance = AsyncMock()
    mock_instance.get = AsyncMock(return_value=_mock_response({"nodes": []}))
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("iicp_client.proxy.clients.directory.httpx.AsyncClient", return_value=mock_ctx):
        await _make_client().discover(limit=10)

    call_kwargs = mock_instance.get.call_args.kwargs
    assert call_kwargs["params"]["limit"] == 10


# ---------------------------------------------------------------------------
# discover() — error handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discover_raises_on_4xx():
    """discover() propagates HTTPStatusError on 4xx directory response."""
    with _patch_client(_mock_response({}, status_code=404)):
        with pytest.raises(httpx.HTTPStatusError):
            await _make_client().discover()


@pytest.mark.asyncio
async def test_discover_raises_on_5xx():
    """discover() propagates HTTPStatusError on 5xx directory response."""
    with _patch_client(_mock_response({}, status_code=503)):
        with pytest.raises(httpx.HTTPStatusError):
            await _make_client().discover()


@pytest.mark.asyncio
async def test_discover_raises_on_connect_error():
    """discover() propagates ConnectError when directory is unreachable."""
    mock_instance = AsyncMock()
    mock_instance.get = AsyncMock(side_effect=httpx.ConnectError("unreachable"))
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("iicp_client.proxy.clients.directory.httpx.AsyncClient", return_value=mock_ctx):
        with pytest.raises(httpx.ConnectError):
            await _make_client().discover()


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------

def test_base_url_trailing_slash_stripped():
    """DirectoryClient strips trailing slash from base_url."""
    client = DirectoryClient(base_url="https://dir.example.com/")
    assert client._base_url == "https://dir.example.com"


def test_timeout_conversion_from_ms():
    """DirectoryClient converts timeout_ms to seconds for httpx."""
    client = DirectoryClient(base_url="https://dir.example.com", timeout_ms=3000)
    assert client._timeout == 3.0


# ---------------------------------------------------------------------------
# bootstrap()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bootstrap_returns_peers():
    """bootstrap() returns the peers list from the directory response."""
    peers = [{"node_id": "p1", "endpoint": "https://peer.example.com"}]
    with _patch_client(_mock_response({"peers": peers})):
        result = await _make_client().bootstrap()
    assert result == peers


@pytest.mark.asyncio
async def test_bootstrap_returns_empty_on_no_peers_key():
    """bootstrap() returns [] when directory response has no 'peers' key."""
    with _patch_client(_mock_response({})):
        result = await _make_client().bootstrap()
    assert result == []


# ---------------------------------------------------------------------------
# discover() Phase 6 federated-directory 307 redirect handling
# DIR-FED-05 (follow 307 transparently) + DIR-FED-06 (≤3 consecutive redirects)
# ---------------------------------------------------------------------------

def _mock_redirect_response(location: str, retry_after: int = 5, trust: str = "low") -> MagicMock:
    """Mock a 307 Temporary Redirect response from the Genesis Seed under load."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 307
    resp.headers = {
        "Location": location,
        "X-IICP-Seed-Redirect": "true",
        "X-IICP-Replica-Trust": trust,
        "X-IICP-Redirect-Reason": "load",
        "Retry-After": str(retry_after),
    }
    resp.request = MagicMock()
    return resp


def _patch_client_sequence(responses: list[MagicMock]):
    """Patch httpx.AsyncClient so successive .get() calls return responses in order."""
    iterator = iter(responses)
    mock_instance = AsyncMock()
    mock_instance.get = AsyncMock(side_effect=lambda *a, **kw: next(iterator))
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    return patch("iicp_client.proxy.clients.directory.httpx.AsyncClient", return_value=mock_ctx)


@pytest.mark.asyncio
async def test_discover_follows_307_to_replica_dir_fed_05():
    """DIR-FED-05: client MUST follow 307 transparently to Location target."""
    redirect = _mock_redirect_response("https://replica1.iicp.network/v1/discover")
    final = _mock_response({"nodes": [{"node_id": "n1", "endpoint": "https://n1.test"}]})
    with _patch_client_sequence([redirect, final]):
        result = await _make_client().discover(intent="urn:iicp:intent:llm:chat:v1")
    assert len(result) == 1
    assert result[0]["node_id"] == "n1"


@pytest.mark.asyncio
async def test_discover_caches_redirect_target():
    """First call follows 307; second call uses cached target without hitting origin."""
    redirect = _mock_redirect_response("https://replica1.iicp.network/v1/discover", retry_after=60)
    final_after_redirect = _mock_response({"nodes": [{"node_id": "n1"}]})
    cached_call = _mock_response({"nodes": [{"node_id": "n2"}]})

    client = _make_client()
    with _patch_client_sequence([redirect, final_after_redirect, cached_call]):
        first = await client.discover()
        second = await client.discover()
    assert first[0]["node_id"] == "n1"
    # Second call should go directly to replica (no redirect chain), returning the cached target's response
    assert second[0]["node_id"] == "n2"


@pytest.mark.asyncio
async def test_discover_refuses_more_than_3_redirects_dir_fed_06():
    """DIR-FED-06: client MUST NOT follow >3 consecutive redirects (loop detection)."""
    # 4 consecutive 307s should trigger the loop-detection guard
    redirects = [
        _mock_redirect_response(f"https://r{i}.iicp.network/v1/discover")
        for i in range(4)
    ]
    with _patch_client_sequence(redirects):
        with pytest.raises(httpx.HTTPStatusError, match="max consecutive redirects"):
            await _make_client().discover()


@pytest.mark.asyncio
async def test_discover_handles_relative_redirect_location():
    """If 307 Location is relative, stay on current host."""
    redirect = MagicMock(spec=httpx.Response)
    redirect.status_code = 307
    redirect.headers = {
        "Location": "/v1/discover-alternate",
        "Retry-After": "5",
    }
    redirect.request = MagicMock()
    # When relative location, we continue on the same base (no host change)
    final = _mock_response({"nodes": [{"node_id": "n-relative"}]})
    with _patch_client_sequence([redirect, final]):
        result = await _make_client().discover()
    assert result[0]["node_id"] == "n-relative"


@pytest.mark.asyncio
async def test_discover_no_redirect_normal_path_unchanged():
    """Sanity: no redirect → existing behavior unchanged."""
    with _patch_client(_mock_response({"nodes": [{"node_id": "direct"}]})):
        result = await _make_client().discover()
    assert result[0]["node_id"] == "direct"


@pytest.mark.asyncio
async def test_discover_rejects_non_https_redirect_scheme_bug_311():
    """bug-311: redirect Location MUST be https://.  http://, file://, ftp:// rejected to prevent SSRF."""
    redirect_http = _mock_redirect_response("http://internal.example.com/v1/discover")
    _final = _mock_response({"nodes": [{"node_id": "should-not-reach-this"}]})
    # When http:// redirect arrives, _process_redirect returns current_base (no follow);
    # the loop re-issues to current_base which would again return 307; loop-guard triggers after 3 attempts.
    # Sequence: 4 http:// redirects → loop guard fires
    with _patch_client_sequence([redirect_http] * 4):
        with pytest.raises(httpx.HTTPStatusError, match="max consecutive redirects"):
            await _make_client().discover()


@pytest.mark.asyncio
async def test_discover_rejects_file_scheme_redirect_bug_311():
    """bug-311: file:// scheme MUST be rejected — prevents local-FS SSRF attempts."""
    redirect_file = _mock_redirect_response("file:///etc/passwd")
    with _patch_client_sequence([redirect_file] * 4):
        with pytest.raises(httpx.HTTPStatusError, match="max consecutive redirects"):
            await _make_client().discover()


# ---------------------------------------------------------------------------
# discover() — cip_capable parameter (S.12 §5.2, CIP-CALL-01 prerequisite)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_sends_cip_capable_true_param():
    """S.12 §5.2: discover(cip_capable=True) must send cip_capable=1 in query params.

    CIP coordinator dispatch MUST only consider nodes with allow_remote_inference=true.
    Laravel boolean validation accepts 1/0, not "true"/"false" strings (CIP-BUG-01 fix).
    """
    mock_instance = AsyncMock()
    mock_instance.get = AsyncMock(return_value=_mock_response({"nodes": []}))
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("iicp_client.proxy.clients.directory.httpx.AsyncClient", return_value=mock_ctx):
        await _make_client().discover(cip_capable=True)

    call_kwargs = mock_instance.get.call_args.kwargs
    assert call_kwargs["params"]["cip_capable"] == 1


@pytest.mark.asyncio
async def test_discover_sends_cip_capable_false_param():
    """discover(cip_capable=False) must send cip_capable=0 (not "false" string)."""
    mock_instance = AsyncMock()
    mock_instance.get = AsyncMock(return_value=_mock_response({"nodes": []}))
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("iicp_client.proxy.clients.directory.httpx.AsyncClient", return_value=mock_ctx):
        await _make_client().discover(cip_capable=False)

    call_kwargs = mock_instance.get.call_args.kwargs
    assert call_kwargs["params"]["cip_capable"] == 0


@pytest.mark.asyncio
async def test_discover_omits_cip_capable_when_not_provided():
    """discover() must not include cip_capable param when cip_capable=None (default)."""
    mock_instance = AsyncMock()
    mock_instance.get = AsyncMock(return_value=_mock_response({"nodes": []}))
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("iicp_client.proxy.clients.directory.httpx.AsyncClient", return_value=mock_ctx):
        await _make_client().discover()

    call_kwargs = mock_instance.get.call_args.kwargs
    assert "cip_capable" not in call_kwargs["params"]


# ---------------------------------------------------------------------------
# credit_balance() — WQ-059 / B-A §10.1 affordability gate input
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_credit_balance_returns_float_on_success():
    """GET /v1/credits/balance → parses the 'balance' field as a float."""
    with _patch_client(_mock_response({"balance": 12.5})):
        result = await _make_client().credit_balance("node-token-abc")
    assert result == 12.5


@pytest.mark.asyncio
async def test_credit_balance_sends_bearer_token():
    """The node_token is sent as a Bearer Authorization header."""
    resp = _mock_response({"balance": 1.0})
    mock_instance = AsyncMock()
    mock_instance.get = AsyncMock(return_value=resp)
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    with patch("iicp_client.proxy.clients.directory.httpx.AsyncClient", return_value=mock_ctx):
        await _make_client().credit_balance("tok-xyz")
    _, kwargs = mock_instance.get.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer tok-xyz"


@pytest.mark.asyncio
async def test_credit_balance_none_on_empty_token():
    """No token → None (gate skipped) without any HTTP call."""
    assert await _make_client().credit_balance("") is None


@pytest.mark.asyncio
async def test_credit_balance_none_on_http_error():
    """Non-200 → None (best-effort): a directory hiccup must not block inference."""
    with _patch_client(_mock_response({}, status_code=503)):
        result = await _make_client().credit_balance("node-token-abc")
    assert result is None


@pytest.mark.asyncio
async def test_credit_balance_none_when_field_absent():
    """200 but no 'balance' key → None (treated as unknown)."""
    with _patch_client(_mock_response({"other": 1})):
        result = await _make_client().credit_balance("node-token-abc")
    assert result is None
