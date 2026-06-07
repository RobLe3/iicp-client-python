# SPDX-License-Identifier: Apache-2.0
"""ADR-014: Prometheus metrics for the proxy — routing_ms and retries_total."""
from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Histogram, generate_latest

# ADR-014 §1 mandatory proxy metrics
proxy_routing_ms = Histogram(
    "iicp_proxy_routing_ms",
    "End-to-end proxy routing latency in milliseconds",
    buckets=[10, 25, 50, 100, 250, 500, 1000, 2500, 5000],
)

proxy_retries_total = Counter(
    "iicp_proxy_retries_total",
    "Total proxy retry attempts",
    ["reason"],
)


def record_routing(latency_ms: float) -> None:
    proxy_routing_ms.observe(latency_ms)


def record_retry(reason: str = "error") -> None:
    proxy_retries_total.labels(reason=reason).inc()


def metrics_output() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
