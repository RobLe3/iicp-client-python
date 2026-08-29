"""Internal HTTP helpers — TLS context, timeout normalization."""

from __future__ import annotations

import secrets
import ssl
import time
from typing import Any
from urllib.parse import urljoin

import httpx

from iicp_client._http_resource import (
    HttpTaskBodyError,
    append_response_chunk,
    bounded_request_json,
    validate_response_headers,
)
from iicp_client.endpoint_security import PinnedAsyncHTTPTransport, resolve_endpoint
from iicp_client.errors import IicpError, from_http


def _traceparent() -> str:
    """Generate a W3C traceparent header value (SDK-06).

    Format: 00-<trace-id>-<parent-id>-01
    trace-id  = 16 random bytes as 32 hex chars
    parent-id =  8 random bytes as 16 hex chars
    flags     = 01 (sampled)
    """
    return f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"


def _tls_context(verify: bool) -> ssl.SSLContext | bool:
    if not verify:
        # SDK-05: tls_verify=False only permitted in debug; prod builds must verify
        return False
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_default_certs()
    return ctx


async def get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout_ms: int = 5_000,
    component: str = "directory",
    tls_verify: bool = True,
    traceparent: str | None = None,
    extra_headers: dict[str, str] | None = None,
    follow_redirects: bool = False,
) -> dict[str, Any]:
    timeout = timeout_ms / 1000.0
    headers = {"traceparent": traceparent or _traceparent()}
    if extra_headers:
        headers.update(extra_headers)
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=_tls_context(tls_verify), follow_redirects=follow_redirects) as client:
            resp = await client.get(url, params=params, headers=headers)
    except httpx.TimeoutException:
        raise IicpError(
            code="IICP-E003",
            message=f"Request to {url} timed out after {timeout_ms}ms",
            component=component,
            retryable=True,
        ) from None
    except httpx.RequestError as exc:
        raise IicpError(
            code="IICP-E004",
            message=f"Network error reaching {url}: {exc}",
            component=component,
            retryable=True,
        ) from exc
    if not resp.is_success:
        raise from_http(resp.status_code, _safe_json(resp), component)
    return resp.json()


async def post_json(
    url: str,
    body: dict[str, Any],
    *,
    timeout_ms: int = 30_000,
    component: str = "adapter",
    tls_verify: bool = True,
    traceparent: str | None = None,
    extra_headers: dict[str, str] | None = None,
    pin_provider_endpoint: bool = False,
) -> tuple[dict[str, Any], int]:
    """Returns (response_body, elapsed_ms)."""
    try:
        encoded = bounded_request_json(body)
    except HttpTaskBodyError as exc:
        raise IicpError(
            code=exc.code,
            message=exc.message,
            component=component,
            retryable=False,
            http_status=exc.status,
        ) from None
    timeout = (timeout_ms / 1000.0) + 2.0
    headers: dict[str, str] = {
        "traceparent": traceparent or _traceparent(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    t0 = time.monotonic()
    response_client: httpx.AsyncClient | None = None
    try:
        if pin_provider_endpoint:
            current = url
            resp: httpx.Response | None = None
            for redirect_count in range(4):
                endpoint = await resolve_endpoint(current)
                transport = PinnedAsyncHTTPTransport(endpoint, verify=_tls_context(tls_verify))
                client = httpx.AsyncClient(
                    timeout=timeout,
                    transport=transport,
                    follow_redirects=False,
                )
                response_client = client
                request = client.build_request("POST", current, content=encoded, headers=headers)
                resp = await client.send(request, stream=True)
                if resp.status_code in {307, 308}:
                    location = resp.headers.get("location")
                    if redirect_count == 3 or not location:
                        await resp.aclose()
                        await client.aclose()
                        response_client = None
                        raise IicpError(
                            code="IICP-ENDPOINT-REFUSED",
                            message="provider redirect limit exceeded or omitted Location",
                            component=component,
                            retryable=False,
                        )
                    next_url = urljoin(str(resp.url), location)
                    current_origin = (resp.url.scheme, resp.url.host, resp.url.port)
                    parsed_next = httpx.URL(next_url)
                    next_origin = (parsed_next.scheme, parsed_next.host, parsed_next.port)
                    if next_origin != current_origin:
                        await resp.aclose()
                        await client.aclose()
                        response_client = None
                        raise IicpError(
                            code="IICP-ENDPOINT-REFUSED",
                            message="cross-origin provider redirect is not allowed",
                            component=component,
                            retryable=False,
                        )
                    await resp.aclose()
                    await client.aclose()
                    response_client = None
                    current = next_url
                    continue
                if 300 <= resp.status_code < 400:
                    await resp.aclose()
                    await client.aclose()
                    response_client = None
                    raise IicpError(
                        code="IICP-ENDPOINT-REFUSED",
                        message="provider redirect method is not allowed",
                        component=component,
                        retryable=False,
                    )
                response_client = client
                break
            assert resp is not None
        else:
            response_client = httpx.AsyncClient(timeout=timeout, verify=_tls_context(tls_verify))
            request = response_client.build_request("POST", url, content=encoded, headers=headers)
            resp = await response_client.send(request, stream=True)
    except httpx.TimeoutException:
        if response_client is not None:
            await response_client.aclose()
        raise IicpError(
            code="IICP-E003",
            message=f"Request to {url} timed out after {timeout_ms}ms",
            component=component,
            retryable=True,
        ) from None
    except httpx.RequestError as exc:
        if response_client is not None:
            await response_client.aclose()
        raise IicpError(
            code="IICP-E004",
            message=f"Network error reaching {url}: {exc}",
            component=component,
            retryable=True,
        ) from exc
    try:
        validate_response_headers(resp.headers)
        content = bytearray()
        async for chunk in resp.aiter_raw():
            append_response_chunk(content, chunk)
    except HttpTaskBodyError as exc:
        raise IicpError(
            code=exc.code,
            message=exc.message,
            component=component,
            retryable=False,
            http_status=exc.status,
        ) from None
    finally:
        await resp.aclose()
        if response_client is not None:
            await response_client.aclose()
    elapsed = int((time.monotonic() - t0) * 1000)
    try:
        decoded = httpx.Response(resp.status_code, content=bytes(content)).json()
    except Exception:
        decoded = {"error": {"code": "invalid_http_body", "message": "provider returned invalid JSON"}}
        if resp.is_success:
            raise IicpError(
                code="invalid_http_body",
                message="provider returned invalid JSON",
                component=component,
                retryable=False,
                http_status=500,
            ) from None
    if not resp.is_success:
        raise from_http(resp.status_code, decoded, component)
    if not isinstance(decoded, dict):
        raise IicpError(
            code="invalid_http_body",
            message="provider response must be a JSON object",
            component=component,
            retryable=False,
            http_status=500,
        )
    return decoded, elapsed


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {"message": resp.text[:200]}
