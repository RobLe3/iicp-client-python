"""Tests for CircuitBreaker."""
from __future__ import annotations

from iicp_client.proxy.routing.circuit_breaker import CircuitBreaker, CircuitOpenError


def test_circuit_allows_initially():
    cb = CircuitBreaker(threshold=3, reset_s=30)
    cb.check("node-1")  # should not raise


def test_circuit_opens_after_threshold():
    cb = CircuitBreaker(threshold=3, reset_s=30)
    for _ in range(3):
        cb.record_failure("node-1")
    try:
        cb.check("node-1")
        raise AssertionError("expected CircuitOpenError")
    except CircuitOpenError as exc:
        assert exc.node_id == "node-1"


def test_circuit_resets_after_success():
    cb = CircuitBreaker(threshold=3, reset_s=30)
    for _ in range(3):
        cb.record_failure("node-1")
    cb.record_success("node-1")
    cb.check("node-1")  # should not raise


def test_circuit_allows_probe_after_reset():
    cb = CircuitBreaker(threshold=1, reset_s=0)
    cb.record_failure("node-1")
    # With reset_s=0 it should allow probe immediately
    assert cb.allow("node-1") is True
