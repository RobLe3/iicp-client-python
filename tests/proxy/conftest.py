"""Shared test fixtures for proxy tests."""
from __future__ import annotations

import pytest

from iicp_client.proxy.config import ProxyConfig
from iicp_client.proxy.routing.circuit_breaker import CircuitBreaker
from iicp_client.proxy.routing.fallback import FallbackChain
from iicp_client.proxy.routing.retry import RetryManager
from iicp_client.proxy.routing.router import TaskRouter
from iicp_client.proxy.routing.selector import NodeSelector

NODE_TOKEN = "test-node-token"


@pytest.fixture
def cfg() -> ProxyConfig:
    return ProxyConfig(
        directory_url="http://mock-directory",
        preferred_region="eu-central",
        max_retries=2,
        retry_base_ms=10,
        circuit_breaker_threshold=3,
        circuit_breaker_reset_s=5,
    )


@pytest.fixture
def retry(cfg: ProxyConfig) -> RetryManager:
    return RetryManager(max_retries=cfg.max_retries, base_ms=cfg.retry_base_ms)


@pytest.fixture
def circuit(cfg: ProxyConfig) -> CircuitBreaker:
    return CircuitBreaker(
        threshold=cfg.circuit_breaker_threshold, reset_s=cfg.circuit_breaker_reset_s
    )


@pytest.fixture
def router(retry: RetryManager, circuit: CircuitBreaker) -> TaskRouter:
    return TaskRouter(node_token=NODE_TOKEN, retry=retry, circuit=circuit)


@pytest.fixture
def selector(cfg: ProxyConfig) -> NodeSelector:
    return NodeSelector(preferred_region=cfg.preferred_region)


@pytest.fixture
def fallback(router: TaskRouter) -> FallbackChain:
    return FallbackChain(router=router)


@pytest.fixture
def good_node() -> dict:
    return {
        "node_id": "node-1",
        "endpoint": "http://mock-node-1",
        "region": "eu-central",
        "available": True,
        "load": 0.1,
        "active_jobs": 1,
        "max_concurrent": 4,
    }


@pytest.fixture
def bad_node() -> dict:
    return {
        "node_id": "node-2",
        "endpoint": "http://mock-node-2",
        "region": "us-east",
        "available": True,
        "load": 0.9,
        "active_jobs": 4,
        "max_concurrent": 4,
    }
