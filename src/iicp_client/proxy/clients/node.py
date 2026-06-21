# SPDX-License-Identifier: Apache-2.0
"""Async client for a single IICP adapter node."""
from __future__ import annotations

import ipaddress
import logging
import os
import ssl
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx

logger = logging.getLogger(__name__)


def _tls_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_default_certs()
    return ctx


def _is_ssrf_safe(url: str) -> bool:
    """Return True only if url is safe to connect to as a node endpoint (SSRF guard).

    Rejects: localhost, RFC1918, link-local, metadata endpoints, Docker service names,
    and known internal DNS suffixes. Mirrors adapter.network.nat_detector._looks_routable().
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    # Dev/test escape hatch (default OFF): allow loopback/private node endpoints so a
    # node + proxy can run on one host (local mesh) and for E2E tests. NEVER enable in
    # production — it re-opens the SSRF surface this guard exists to close.
    if os.environ.get("IICP_PROXY_ALLOW_LOOPBACK_NODES", "").strip().lower() in ("1", "true", "yes"):
        return True
    if host in {"localhost", "0.0.0.0", "::1", "::"}:
        return False
    blocked_suffixes = (".local", ".internal", ".lan", ".test", ".invalid", ".localhost")
    if any(host.endswith(s) for s in blocked_suffixes):
        return False
    if "." not in host:  # bare Docker/compose service name without TLD
        return False
    try:
        addr = ipaddress.ip_address(host)
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return False
    except ValueError:
        pass
    return True


class NodeClient:
    def __init__(
        self, endpoint: str, node_token: str, transport_endpoint: str | None = None
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._transport_endpoint = transport_endpoint.rstrip("/") if transport_endpoint else None
        self._token = node_token
        self._transport_attempted = False  # track if we've tried native endpoint

    async def submit_task(
        self,
        task_id: UUID,
        intent: str,
        payload: dict[str, Any],
        timeout_ms: int,
        trace_id: str | None = None,
        cip_envelope: dict[str, Any] | None = None,
        source_node_id: str | None = None,
    ) -> dict[str, Any]:
        """Submit a task with HTTP→native IICP fallback (spec v0.7.0 dual-endpoint).

        When HTTP endpoint (443/8080) fails with connection error or 503, and the
        node advertised a transport_endpoint (native IICP on port 9484), retries
        on that address. This enables inference behind NAT where HTTP is filtered
        but native binary framing (port 9484) can traverse the NAT.
        """
        body = {
            "task_id": str(task_id),
            "intent": intent,
            "payload": payload,
            "constraints": {"timeout_ms": timeout_ms},
            "auth": {"node_token": self._token},
        }
        if source_node_id:
            # #525/G1b: proxy/coordinator dispatches identify the querying node so
            # credit self-query neutrality can apply at the directory when receipts are awarded.
            body["source_node_id"] = source_node_id
        # Assemble trace block — merge trace_id + CIP coordinator role (S.12 §4.2 CIP-CALL-06)
        trace_block: dict[str, Any] = {}
        if trace_id:
            trace_block["trace_id"] = trace_id
        if cip_envelope is not None:
            # Coordinator MUST declare cip_role in its own trace when initiating dispatch
            trace_block["cip_role"] = "coordinator"
        if trace_block:
            body["trace"] = trace_block
        # S.12 §4.1: include cip envelope when dispatching a CIP sub-task (CIP-CALL-01)
        if cip_envelope is not None:
            body["cip"] = cip_envelope
        timeout = (timeout_ms / 1000.0) + 2.0  # buffer for network overhead
        headers = {"X-IICP-Trace-Id": trace_id} if trace_id else {}

        # Primary attempt: HTTP endpoint (control-plane default)
        try:
            async with httpx.AsyncClient(timeout=timeout, verify=_tls_context()) as client:
                resp = await client.post(
                    f"{self._endpoint}/v1/task", json=body, headers=headers
                )
                resp.raise_for_status()
                return resp.json()
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as e:
            # Fallback: try native IICP endpoint if advertised (spec v0.7.0 dual-endpoint)
            if self._transport_endpoint and not self._transport_attempted:
                self._transport_attempted = True
                logger.info(
                    "HTTP endpoint failed (%s): falling back to transport_endpoint %s",
                    type(e).__name__, self._transport_endpoint
                )
                try:
                    async with httpx.AsyncClient(timeout=timeout, verify=_tls_context()) as client:
                        resp = await client.post(
                            f"{self._transport_endpoint}/v1/task", json=body, headers=headers
                        )
                        resp.raise_for_status()
                        return resp.json()
                except Exception as fallback_err:
                    logger.warning(
                        "Transport endpoint also failed: %s. Both HTTP and native IICP unreachable.",
                        type(fallback_err).__name__
                    )
                    raise
            # No fallback available or already attempted — raise original error
            raise
