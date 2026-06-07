# SPDX-License-Identifier: Apache-2.0
"""OpenTelemetry span helpers — ADR-014 TRACE-04/05/08/09/10/13.

Provides span context managers for mandatory IICP proxy spans:
  iicp.proxy.discover       (TRACE-13) — directory discover HTTP call
  iicp.proxy.route          (TRACE-04) — node resolution and task dispatch
  iicp.cip.dispatch         (TRACE-05) — CIP consumer gate evaluation
  iicp.cip.award            (TRACE-08) — credit award submission to directory
  iicp.proxy.cip_consensus  (TRACE-09) — successful CIP consensus (REMOTE authorized)
  iicp.proxy.cip_no_consensus (TRACE-10) — failed CIP consensus (IICP-E022, 502 path)

Behaviour:
  - When opentelemetry-api is installed AND OTEL_EXPORTER_OTLP_ENDPOINT
    is set: exports spans to the configured collector via OTLP/HTTP.
  - Otherwise: yields a no-op span so call sites need no conditionals.

W3C traceparent forwarding to upstream nodes is handled at the HTTP layer
in clients/node.py; this module manages the proxy-side span lifecycle.
"""
from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Generator

logger = logging.getLogger(__name__)

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.sdk.trace import TracerProvider as _TracerProvider

    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False

_initialised = False
_tracer: object | None = None


def _init() -> None:
    global _initialised, _tracer
    if _initialised:
        return
    _initialised = True

    if not _OTEL_AVAILABLE:
        _tracer = _NoopTracer()
        return

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = _TracerProvider()
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
            _otel_trace.set_tracer_provider(provider)
            _tracer = _otel_trace.get_tracer("iicp.proxy")
            logger.info("otel_tracer: OTLP exporter configured → %s", endpoint)
        except Exception as exc:  # noqa: BLE001
            logger.warning("otel_tracer: OTLP init failed (%s) — using no-op", exc)
            _tracer = _NoopTracer()
    else:
        provider = _TracerProvider()
        _otel_trace.set_tracer_provider(provider)
        _tracer = _otel_trace.get_tracer("iicp.proxy")


class _NoopSpan:
    def set_attribute(self, _key: str, _value: object) -> None:
        pass

    def record_exception(self, _exc: BaseException) -> None:
        pass


class _NoopTracer:
    @contextlib.contextmanager
    def start_as_current_span(self, _name: str, **_kw: object) -> Generator[_NoopSpan, None, None]:
        yield _NoopSpan()


@contextlib.contextmanager
def proxy_discover_span(intent: str) -> Generator[object, None, None]:
    """TRACE-13: iicp.proxy.discover — wraps the directory discover HTTP call.

    Emitted once per task dispatch before node selection. Captures the intent
    queried and (via set_attribute after the call) the candidate node count.
    Parent span: iicp.proxy.route when CIP is inactive, or the task root span.
    """
    _init()
    assert _tracer is not None
    with _tracer.start_as_current_span("iicp.proxy.discover") as span:  # type: ignore[union-attr]
        span.set_attribute("iicp.intent", intent)
        yield span


@contextlib.contextmanager
def proxy_route_span(task_id: str, intent: str) -> Generator[object, None, None]:
    """TRACE-04: iicp.proxy.route — wraps node resolution and task dispatch."""
    _init()
    assert _tracer is not None
    with _tracer.start_as_current_span("iicp.proxy.route") as span:  # type: ignore[union-attr]
        span.set_attribute("iicp.task_id", task_id)
        span.set_attribute("iicp.intent", intent)
        yield span


@contextlib.contextmanager
def cip_dispatch_span(task_id: str, strategy: str) -> Generator[object, None, None]:
    """TRACE-05: iicp.cip.dispatch — wraps the CIP consumer dispatch decision.

    Emitted when the coordinator evaluates the §2.2 gates (enabled → credit →
    sensitivity → workers). Nested inside iicp.proxy.route when CIP is active.
    """
    _init()
    assert _tracer is not None
    with _tracer.start_as_current_span("iicp.cip.dispatch") as span:  # type: ignore[union-attr]
        span.set_attribute("iicp.task_id", task_id)
        span.set_attribute("iicp.cip.strategy", strategy)
        yield span


@contextlib.contextmanager
def cip_consensus_span(
    task_id: str,
    policy: str,
    replicas: int,
    quorum_met: bool,
    latency_ms: float | None = None,
) -> Generator[object, None, None]:
    """TRACE-09: iicp.proxy.cip_consensus — successful CIP consensus (REMOTE authorized).

    Emitted when decide_dispatch() returns REMOTE and all replicas have responded.
    Attributes: policy (strategy), replicas (fan-out count), quorum_met (always True
    in this path), latency_ms (end-to-end consensus duration). Nested inside
    iicp.cip.dispatch when CIP consensus succeeds.
    """
    _init()
    assert _tracer is not None
    with _tracer.start_as_current_span("iicp.proxy.cip_consensus") as span:  # type: ignore[union-attr]
        span.set_attribute("iicp.task_id", task_id)
        span.set_attribute("iicp.cip.policy", policy)
        span.set_attribute("iicp.cip.replicas", replicas)
        span.set_attribute("iicp.cip.quorum_met", quorum_met)
        if latency_ms is not None:
            span.set_attribute("iicp.cip.latency_ms", latency_ms)
        yield span


@contextlib.contextmanager
def cip_no_consensus_span(
    task_id: str,
    reason: str,
    eligible_workers: int = 0,
) -> Generator[object, None, None]:
    """TRACE-10: iicp.proxy.cip_no_consensus — failed CIP consensus (IICP-E022 / 502 path).

    Emitted when decide_dispatch() returns ERROR (no CIP-capable workers reachable
    in REMOTE_FIRST/BALANCED strategy). The error_code is IICP-E022 per spec §2.2.
    eligible_workers is the count of workers considered before the failure.
    """
    _init()
    assert _tracer is not None
    with _tracer.start_as_current_span("iicp.proxy.cip_no_consensus") as span:  # type: ignore[union-attr]
        span.set_attribute("iicp.task_id", task_id)
        span.set_attribute("iicp.cip.reason", reason)
        span.set_attribute("iicp.cip.eligible_workers", eligible_workers)
        yield span


@contextlib.contextmanager
def cip_award_span(task_id: str, tokens_used: int, amount: float) -> Generator[object, None, None]:
    """TRACE-08: iicp.cip.award — wraps the credit award POST to the directory.

    Emitted only when submit_award() makes a network call. Carries task_id,
    tokens_used, and credits amount for credit ledger tracing. Set on both
    success and failure paths; the outcome is readable from the span status.
    """
    _init()
    assert _tracer is not None
    with _tracer.start_as_current_span("iicp.cip.award") as span:  # type: ignore[union-attr]
        span.set_attribute("iicp.task_id", task_id)
        span.set_attribute("iicp.cip.tokens_used", tokens_used)
        span.set_attribute("iicp.cip.credits_amount", amount)
        yield span
