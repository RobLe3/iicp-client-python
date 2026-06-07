# SPDX-License-Identifier: Apache-2.0
"""The proxy self-identifies as `iicp-proxy` on every response (maintainer req)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from iicp_client.proxy.config import ProxyConfig
from iicp_client.proxy.main import create_app


def test_proxy_identifies_as_iicp_proxy_on_every_response():
    with TestClient(create_app(ProxyConfig()), raise_server_exceptions=False) as c:
        for path in ("/v1/models", "/api/version", "/api/tags", "/status", "/metrics"):
            r = c.get(path)
            assert r.headers.get("server") == "iicp-proxy", f"{path} → {r.headers.get('server')}"
