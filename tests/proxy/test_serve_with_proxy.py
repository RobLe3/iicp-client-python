# SPDX-License-Identifier: Apache-2.0
"""serve --with-proxy (ADR-050 / 2-C) — the co-host convenience.

The node and proxy stay crash-isolated: a proxy failure logs and returns, it must
never propagate out of _run_cohosted_proxy (which would drop the network-facing node).
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from iicp_client.cli import _build_parser, _run_cohosted_proxy


def test_serve_accepts_with_proxy_flag():
    p = _build_parser()
    assert p.parse_args(["serve", "--with-proxy"]).with_proxy is True
    assert p.parse_args(["serve"]).with_proxy is False


def test_cohosted_proxy_crash_is_isolated():
    """If the proxy server raises, the helper swallows it (logs) — never propagates,
    so `serve --with-proxy` keeps the node running."""
    async def _boom():
        raise RuntimeError("bind failed")

    server = MagicMock()
    server.serve = _boom
    with patch("iicp_client.proxy.main.create_app", return_value=MagicMock()), \
         patch("uvicorn.Server", return_value=server):
        # Must complete without raising.
        asyncio.run(_run_cohosted_proxy())


def test_cohosted_proxy_forces_loopback():
    """When co-hosted the proxy is bound to 127.0.0.1 regardless of config (trust boundary)."""
    captured = {}

    async def _noop():
        return None

    def _capture_config(app, host, port, **_kwargs):  # noqa: ARG001
        captured["host"] = host
        s = MagicMock()
        s.serve = _noop
        return s

    server = MagicMock()
    server.serve = _noop
    with patch("iicp_client.proxy.main.create_app", return_value=MagicMock()), \
         patch("uvicorn.Config", side_effect=_capture_config), \
         patch("uvicorn.Server", return_value=server):
        asyncio.run(_run_cohosted_proxy())
    assert captured["host"] == "127.0.0.1"
