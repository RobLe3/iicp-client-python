"""ADR-014: Tests for Prometheus /metrics endpoint in the proxy — METRICS-01."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from iicp_client.proxy.config import ProxyConfig
from iicp_client.proxy.main import create_app


@pytest.fixture()
def client() -> TestClient:
    cfg = ProxyConfig(
        directory_url="http://localhost:8080",
        node_token_env="IICP_NODE_TOKEN",
        host="127.0.0.1",
        port=8765,
    )
    app = create_app(cfg)
    return TestClient(app, raise_server_exceptions=False)


def test_metrics_endpoint_returns_200(client: TestClient) -> None:
    """METRICS-01: GET /metrics returns HTTP 200 with Prometheus text format."""
    r = client.get("/metrics")
    assert r.status_code == 200


def test_metrics_content_type_is_prometheus(client: TestClient) -> None:
    """METRICS-01: /metrics Content-Type contains 'text/plain'."""
    r = client.get("/metrics")
    assert "text/plain" in r.headers.get("content-type", "")


def test_metrics_contains_proxy_routing_ms(client: TestClient) -> None:
    """ADR-014: proxy /metrics includes iicp_proxy_routing_ms histogram."""
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "iicp_proxy_routing_ms" in r.text


def test_metrics_contains_proxy_retries_total(client: TestClient) -> None:
    """ADR-014: proxy /metrics includes iicp_proxy_retries_total counter."""
    r = client.get("/metrics")
    assert "iicp_proxy_retries_total" in r.text


def test_record_retry_increments_counter() -> None:
    """ADR-014: record_retry() increments iicp_proxy_retries_total."""
    from iicp_client.proxy.metrics import proxy_retries_total, record_retry

    before = proxy_retries_total.labels(reason="ConnectError")._value.get()
    record_retry(reason="ConnectError")
    after = proxy_retries_total.labels(reason="ConnectError")._value.get()
    assert after == before + 1
