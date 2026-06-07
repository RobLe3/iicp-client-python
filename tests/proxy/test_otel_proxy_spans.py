"""TRACE-04/05/08/09/10/13: Proxy OTel span unit tests.

Verifies that proxy_discover_span, proxy_route_span, cip_dispatch_span,
cip_award_span, cip_consensus_span, and cip_no_consensus_span are context
managers that work correctly in no-op mode (no OTel SDK installed).
Mirrors adapter/tests/test_otel_cip_spans.py pattern.
"""
from __future__ import annotations

import pytest

from iicp_client.proxy.otel_tracer import (
    cip_award_span,
    cip_consensus_span,
    cip_dispatch_span,
    cip_no_consensus_span,
    proxy_discover_span,
    proxy_route_span,
)

# ---------------------------------------------------------------------------
# TRACE-13: proxy_discover_span
# ---------------------------------------------------------------------------


def test_proxy_discover_span_is_context_manager():
    with proxy_discover_span(intent="urn:iicp:intent:llm:chat:v1") as span:
        assert span is not None


def test_proxy_discover_span_noop_sets_attributes():
    with proxy_discover_span(intent="urn:iicp:intent:llm:chat:v1") as span:
        span.set_attribute("iicp.intent", "urn:iicp:intent:llm:chat:v1")
        span.set_attribute("iicp.discover.node_count", 7)


def test_proxy_discover_span_exception_propagates():
    with pytest.raises(ConnectionError, match="discover-fail"):
        with proxy_discover_span(intent="urn:iicp:intent:llm:chat:v1"):
            raise ConnectionError("discover-fail")


# ---------------------------------------------------------------------------
# TRACE-04: proxy_route_span
# ---------------------------------------------------------------------------


def test_proxy_route_span_is_context_manager():
    with proxy_route_span(task_id="t-1", intent="urn:iicp:intent:llm:chat:v1") as span:
        assert span is not None


def test_proxy_route_span_noop_does_not_raise():
    with proxy_route_span(task_id="t-1", intent="urn:iicp:intent:llm:chat:v1") as span:
        span.set_attribute("iicp.task_id", "t-1")
        span.set_attribute("iicp.intent", "urn:iicp:intent:llm:chat:v1")


def test_proxy_route_span_exception_propagates():
    with pytest.raises(ValueError, match="route-fail"):
        with proxy_route_span(task_id="t-err", intent="urn:iicp:intent:llm:chat:v1"):
            raise ValueError("route-fail")


# ---------------------------------------------------------------------------
# TRACE-05: cip_dispatch_span
# ---------------------------------------------------------------------------


def test_cip_dispatch_span_is_context_manager():
    with cip_dispatch_span(task_id="t-1", strategy="best-of-n") as span:
        assert span is not None


def test_cip_dispatch_span_noop_does_not_raise():
    with cip_dispatch_span(task_id="t-1", strategy="majority-vote") as span:
        span.set_attribute("iicp.task_id", "t-1")
        span.set_attribute("iicp.cip.strategy", "majority-vote")


def test_cip_dispatch_span_exception_propagates():
    with pytest.raises(RuntimeError, match="dispatch-fail"):
        with cip_dispatch_span(task_id="t-err", strategy="map-reduce"):
            raise RuntimeError("dispatch-fail")


# ---------------------------------------------------------------------------
# TRACE-08: cip_award_span
# ---------------------------------------------------------------------------


def test_cip_award_span_is_context_manager():
    with cip_award_span(task_id="t-1", tokens_used=500, amount=0.5) as span:
        assert span is not None


def test_cip_award_span_noop_sets_attributes():
    with cip_award_span(task_id="t-1", tokens_used=500, amount=0.5) as span:
        span.set_attribute("iicp.task_id", "t-1")
        span.set_attribute("iicp.cip.tokens_used", 500)
        span.set_attribute("iicp.cip.credits_amount", 0.5)


def test_cip_award_span_zero_amount():
    with cip_award_span(task_id="t-zero", tokens_used=0, amount=0.0) as span:
        assert span is not None


def test_cip_award_span_exception_propagates():
    with pytest.raises(RuntimeError, match="award-fail"):
        with cip_award_span(task_id="t-err", tokens_used=100, amount=0.1):
            raise RuntimeError("award-fail")


# ---------------------------------------------------------------------------
# TRACE-09: cip_consensus_span
# ---------------------------------------------------------------------------


def test_cip_consensus_span_is_context_manager():
    with cip_consensus_span(task_id="t-1", policy="local-first", replicas=1, quorum_met=True) as span:
        assert span is not None


def test_cip_consensus_span_noop_sets_attributes():
    with cip_consensus_span(task_id="t-1", policy="balanced", replicas=3, quorum_met=True, latency_ms=42.5) as span:
        span.set_attribute("iicp.task_id", "t-1")
        span.set_attribute("iicp.cip.policy", "balanced")
        span.set_attribute("iicp.cip.replicas", 3)
        span.set_attribute("iicp.cip.quorum_met", True)
        span.set_attribute("iicp.cip.latency_ms", 42.5)


def test_cip_consensus_span_exception_propagates():
    with pytest.raises(RuntimeError, match="consensus-fail"):
        with cip_consensus_span(task_id="t-err", policy="remote-first", replicas=1, quorum_met=True):
            raise RuntimeError("consensus-fail")


# ---------------------------------------------------------------------------
# TRACE-10: cip_no_consensus_span
# ---------------------------------------------------------------------------


def test_cip_no_consensus_span_is_context_manager():
    with cip_no_consensus_span(task_id="t-1", reason="IICP-E022") as span:
        assert span is not None


def test_cip_no_consensus_span_noop_sets_attributes():
    with cip_no_consensus_span(task_id="t-1", reason="IICP-E022", eligible_workers=0) as span:
        span.set_attribute("iicp.task_id", "t-1")
        span.set_attribute("iicp.cip.reason", "IICP-E022")
        span.set_attribute("iicp.cip.eligible_workers", 0)


def test_cip_no_consensus_span_exception_propagates():
    with pytest.raises(ValueError, match="no-consensus-fail"):
        with cip_no_consensus_span(task_id="t-err", reason="IICP-E022", eligible_workers=2):
            raise ValueError("no-consensus-fail")


# ---------------------------------------------------------------------------
# Span coexistence — TRACE-04/05/08/09/10 can nest without conflict
# ---------------------------------------------------------------------------


def test_all_proxy_spans_nest_safely():
    with proxy_route_span("t-nest", "urn:iicp:intent:llm:chat:v1") as r:
        with cip_dispatch_span("t-nest", "best-of-n") as d:
            with cip_award_span("t-nest", 1000, 1.0) as a:
                with cip_consensus_span("t-nest", "balanced", 2, True) as c:
                    with cip_no_consensus_span("t-nest", "IICP-E022", 0) as n:
                        assert all(s is not None for s in (r, d, a, c, n))
