"""Tests for FallbackChain."""
from __future__ import annotations

from uuid import uuid4

import respx
from httpx import Response

from iicp_client.proxy.routing.circuit_breaker import CircuitBreaker
from iicp_client.proxy.routing.fallback import FallbackChain
from iicp_client.proxy.routing.retry import RetryManager
from iicp_client.proxy.routing.router import TaskRouter


def make_chain(token: str = "tok") -> FallbackChain:
    retry = RetryManager(max_retries=1, base_ms=1)
    circuit = CircuitBreaker(threshold=5, reset_s=30)
    router = TaskRouter(node_token=token, retry=retry, circuit=circuit)
    return FallbackChain(router=router)


@respx.mock
async def test_fallback_succeeds_on_first_node():
    chain = make_chain()
    node = {"node_id": "n1", "endpoint": "http://1.2.3.4:8080", "region": "eu-central",
            "available": True, "load": 0.1, "active_jobs": 0, "max_concurrent": 4}

    respx.post("http://1.2.3.4:8080/v1/task").mock(
        return_value=Response(200, json={"status": "success", "task_id": str(uuid4()),
                                        "result": {}, "metrics": {"latency_ms": 10}, "error": None})
    )
    result = await chain.execute([node], uuid4(), "urn:iicp:intent:llm:chat:v1", {}, 5000)
    assert result["status"] == "success"


@respx.mock
async def test_fallback_tries_second_node_on_first_failure():
    chain = make_chain()
    n1 = {"node_id": "n1", "endpoint": "http://1.2.3.4:8080", "region": "eu-central",
          "available": True, "load": 0.1, "active_jobs": 0, "max_concurrent": 4}
    n2 = {"node_id": "n2", "endpoint": "http://1.2.3.5:8080", "region": "eu-central",
          "available": True, "load": 0.1, "active_jobs": 0, "max_concurrent": 4}

    respx.post("http://1.2.3.4:8080/v1/task").mock(side_effect=Exception("down"))
    respx.post("http://1.2.3.5:8080/v1/task").mock(
        return_value=Response(200, json={"status": "success", "task_id": str(uuid4()),
                                        "result": {}, "metrics": {"latency_ms": 5}, "error": None})
    )
    result = await chain.execute([n1, n2], uuid4(), "urn:iicp:intent:llm:chat:v1", {}, 5000)
    assert result["status"] == "success"


@respx.mock
async def test_fallback_returns_error_when_all_nodes_fail():
    chain = make_chain()
    node = {"node_id": "n1", "endpoint": "http://1.2.3.4:8080", "region": "eu-central",
            "available": True, "load": 0.1, "active_jobs": 0, "max_concurrent": 4}

    respx.post("http://1.2.3.4:8080/v1/task").mock(side_effect=Exception("all down"))
    result = await chain.execute([node], uuid4(), "urn:iicp:intent:llm:chat:v1", {}, 5000)
    assert result["status"] == "error"
    assert result["error"]["code"] == "no_available_node"


async def test_fallback_returns_error_on_empty_node_list():
    """WQ-030: empty discover → IICP-E033 (specific, distinct from no_available_node)."""
    chain = make_chain()
    result = await chain.execute([], uuid4(), "urn:iicp:intent:llm:chat:v1", {}, 5000)
    assert result["status"] == "error"
    assert result["error"]["code"] == "IICP-E033"
    assert "urn:iicp:intent:llm:chat:v1" in result["error"]["message"]
    assert "Verify" in result["error"]["message"] or "verify" in result["error"]["message"]


# ---------------------------------------------------------------------------
# CIP-CALL-01: cip_envelope passthrough through FallbackChain (S.12 §4.1)
# ---------------------------------------------------------------------------

@respx.mock
async def test_fallback_passes_cip_envelope_to_router():
    """CIP-CALL-01: cip_envelope provided to FallbackChain.execute() must reach NodeClient.

    FallbackChain is the caller of TaskRouter.route(); without forwarding cip_envelope,
    any coordinator that dispatches CIP sub-tasks via FallbackChain would silently drop
    the cip object from the outbound CALL body.
    """
    from unittest.mock import patch

    cip_env = {"cip_role": "worker", "cip_session_key": "sess-fallback-test"}
    captured: list[dict] = []

    node = {"node_id": "n-cip", "endpoint": "http://cip-worker", "region": "eu-central",
            "available": True}

    async def capturing_route(n, tid, intent, payload, tms, cip_envelope=None, **_kwargs):
        captured.append({"cip_envelope": cip_envelope})
        # Echo back the session key so CIP-BIND-01 check passes
        sk = (cip_envelope or {}).get("cip_session_key")
        return {"status": "success", "trace": {"cip_session_key": sk}}

    chain = make_chain()
    with patch.object(chain._router, "route", side_effect=capturing_route):
        result = await chain.execute(
            [node], uuid4(), "urn:iicp:intent:llm:chat:v1", {}, 5000,
            cip_envelope=cip_env,
        )

    assert result["status"] == "success"
    assert len(captured) == 1
    assert captured[0]["cip_envelope"] == cip_env


# ---------------------------------------------------------------------------
# CIP-AGG-01: trace.cip_aggregation in coordinator RESPONSE (S.12 §4.3)
# ---------------------------------------------------------------------------


@respx.mock
async def test_cip_agg_present_on_success():
    """CIP-AGG-01: coordinator RESPONSE MUST include trace.cip_aggregation when CIP was activated."""
    from unittest.mock import patch

    cip_env = {"cip_role": "worker", "cip_session_key": "sess-agg-test"}
    node = {"node_id": "worker-agg-01", "endpoint": "http://worker1", "region": "eu-central", "available": True}

    async def mock_route(n, tid, intent, payload, tms, cip_envelope=None, **_kwargs):
        sk = (cip_envelope or {}).get("cip_session_key")
        return {"status": "success", "task_id": str(tid), "result": {}, "metrics": {"latency_ms": 5},
                "trace": {"cip_session_key": sk}}

    chain = make_chain()
    with patch.object(chain._router, "route", side_effect=mock_route):
        result = await chain.execute(
            [node], uuid4(), "urn:iicp:intent:llm:chat:v1", {}, 5000,
            cip_envelope=cip_env,
        )

    assert result["status"] == "success"
    assert "trace" in result
    agg = result["trace"]["cip_aggregation"]
    assert agg["policy"] == "best_of_n"
    assert agg["replicas_dispatched"] == 1
    assert agg["replicas_responded"] == 1
    assert agg["selected_worker_id"] == "worker-agg-01"


@respx.mock
async def test_cip_agg_zero_responded_when_all_fail():
    """CIP-AGG-01: replicas_responded == 0 and selected_worker_id == null when all nodes fail."""
    from unittest.mock import patch

    cip_env = {"cip_role": "worker", "cip_session_key": "sess-fail-test"}
    node = {"node_id": "worker-fail", "endpoint": "http://fail-worker", "region": "eu-central", "available": True}

    async def failing_route(n, tid, intent, payload, tms, cip_envelope=None, **_kwargs):
        raise RuntimeError("worker down")

    chain = make_chain()
    with patch.object(chain._router, "route", side_effect=failing_route):
        result = await chain.execute(
            [node], uuid4(), "urn:iicp:intent:llm:chat:v1", {}, 5000,
            cip_envelope=cip_env,
        )

    assert result["status"] == "error"
    assert "trace" in result
    agg = result["trace"]["cip_aggregation"]
    assert agg["replicas_responded"] == 0
    assert agg["selected_worker_id"] is None


async def test_cip_agg_present_on_empty_node_list():
    """CIP-AGG-01: empty node list response also includes cip_aggregation when CIP is active."""
    cip_env = {"cip_role": "worker", "cip_session_key": "sess-empty"}
    chain = make_chain()
    result = await chain.execute(
        [], uuid4(), "urn:iicp:intent:llm:chat:v1", {}, 5000,
        cip_envelope=cip_env,
    )
    assert result["status"] == "error"
    assert result["error"]["code"] == "IICP-E033"
    assert "trace" in result
    agg = result["trace"]["cip_aggregation"]
    assert agg["replicas_responded"] == 0
    assert agg["selected_worker_id"] is None


async def test_cip_agg_not_present_without_cip_envelope():
    """CIP-AGG-01: no trace.cip_aggregation when cip_envelope is None (non-CIP dispatch)."""
    chain = make_chain()
    result = await chain.execute(
        [], uuid4(), "urn:iicp:intent:llm:chat:v1", {}, 5000,
    )
    assert result.get("trace") is None or "cip_aggregation" not in result.get("trace", {})


@respx.mock
async def test_cip_agg_majority_vote_includes_vote_fields():
    """CIP-AGG-01: majority_vote policy MUST include cip_vote_count and cip_quorum_threshold."""
    from unittest.mock import patch

    cip_env = {"cip_role": "worker", "cip_session_key": "sess-mv"}
    node = {"node_id": "worker-mv", "endpoint": "http://mv-worker", "region": "eu-central", "available": True}

    async def mock_route(n, tid, intent, payload, tms, cip_envelope=None, **_kwargs):
        sk = (cip_envelope or {}).get("cip_session_key")
        return {"status": "success", "task_id": str(tid), "result": {}, "metrics": {"latency_ms": 5},
                "trace": {"cip_session_key": sk}}

    chain = make_chain()
    with patch.object(chain._router, "route", side_effect=mock_route):
        result = await chain.execute(
            [node], uuid4(), "urn:iicp:intent:llm:chat:v1", {}, 5000,
            cip_envelope=cip_env,
            cip_policy="majority_vote",
            cip_replicas=3,
            cip_quorum=2,
        )

    agg = result["trace"]["cip_aggregation"]
    assert agg["policy"] == "majority_vote"
    assert "cip_vote_count" in agg
    assert "cip_quorum_threshold" in agg
    assert agg["cip_quorum_threshold"] == 2


# ---------------------------------------------------------------------------
# CIP-BIND-01 — session key binding verification (S.12 §10.4)
# ---------------------------------------------------------------------------

@respx.mock
async def test_cip_bind_matching_key_accepted():
    """CIP-BIND-01: worker response with matching cip_session_key is accepted."""
    from unittest.mock import patch

    session_key = "sk-abc123"
    cip_env = {"cip_role": "worker", "cip_session_key": session_key}
    node = {"node_id": "worker-1", "endpoint": "http://worker1", "region": "eu-central", "available": True}

    async def mock_route(n, tid, intent, payload, tms, cip_envelope=None, **_kwargs):
        return {
            "status": "success",
            "task_id": str(tid),
            "result": {},
            "metrics": {"latency_ms": 5},
            "trace": {"cip_session_key": session_key},
        }

    chain = make_chain()
    with patch.object(chain._router, "route", side_effect=mock_route):
        result = await chain.execute(
            [node], uuid4(), "urn:iicp:intent:llm:chat:v1", {}, 5000,
            cip_envelope=cip_env,
        )

    assert result["status"] == "success"


@respx.mock
async def test_cip_bind_wrong_key_discarded_tries_next():
    """CIP-BIND-01: response with mismatched cip_session_key is discarded; next node tried."""
    from unittest.mock import patch

    session_key = "sk-correct"
    cip_env = {"cip_role": "worker", "cip_session_key": session_key}
    n1 = {"node_id": "worker-bad", "endpoint": "http://worker1", "region": "eu-central", "available": True}
    n2 = {"node_id": "worker-good", "endpoint": "http://worker2", "region": "eu-central", "available": True}
    calls: list[str] = []

    async def mock_route(n, tid, intent, payload, tms, cip_envelope=None, **_kwargs):
        nid = n.get("node_id", "")
        calls.append(nid)
        if nid == "worker-bad":
            return {
                "status": "success",
                "task_id": str(tid),
                "result": {},
                "metrics": {"latency_ms": 5},
                "trace": {"cip_session_key": "sk-WRONG"},
            }
        return {
            "status": "success",
            "task_id": str(tid),
            "result": {},
            "metrics": {"latency_ms": 5},
            "trace": {"cip_session_key": session_key},
        }

    chain = make_chain()
    with patch.object(chain._router, "route", side_effect=mock_route):
        result = await chain.execute(
            [n1, n2], uuid4(), "urn:iicp:intent:llm:chat:v1", {}, 5000,
            cip_envelope=cip_env,
        )

    assert result["status"] == "success"
    assert calls == ["worker-bad", "worker-good"], "should have tried both nodes in order"


@respx.mock
async def test_cip_bind_missing_key_discarded():
    """CIP-BIND-01: response missing trace.cip_session_key is treated as mismatch and discarded."""
    from unittest.mock import patch

    session_key = "sk-expected"
    cip_env = {"cip_role": "worker", "cip_session_key": session_key}
    node = {"node_id": "worker-1", "endpoint": "http://worker1", "region": "eu-central", "available": True}

    async def mock_route(n, tid, intent, payload, tms, cip_envelope=None, **_kwargs):
        # No trace at all — key is missing
        return {"status": "success", "task_id": str(tid), "result": {}, "metrics": {"latency_ms": 5}}

    chain = make_chain()
    with patch.object(chain._router, "route", side_effect=mock_route):
        result = await chain.execute(
            [node], uuid4(), "urn:iicp:intent:llm:chat:v1", {}, 5000,
            cip_envelope=cip_env,
        )

    # Single node discarded → exhausted error
    assert result["status"] == "error"
    assert result["error"]["code"] == "no_available_node"


@respx.mock
async def test_cip_bind_no_check_without_cip_envelope():
    """CIP-BIND-01: no session key check when cip_envelope is None (non-CIP dispatch)."""
    from unittest.mock import patch

    node = {"node_id": "worker-1", "endpoint": "http://worker1", "region": "eu-central", "available": True}

    async def mock_route(n, tid, intent, payload, tms, cip_envelope=None, **_kwargs):
        # Response with no trace — should be fine when not CIP
        return {"status": "success", "task_id": str(tid), "result": {}, "metrics": {"latency_ms": 5}}

    chain = make_chain()
    with patch.object(chain._router, "route", side_effect=mock_route):
        result = await chain.execute(
            [node], uuid4(), "urn:iicp:intent:llm:chat:v1", {}, 5000,
            cip_envelope=None,
        )

    assert result["status"] == "success"


# ── CIP-CR1-WIRE: TC-9d submit_award wiring ──────────────────────────────────

def make_chain_with_award(
    token: str = "tok",
    directory_url: str = "http://dir",
) -> FallbackChain:
    from iicp_client.proxy.cip.coordinator import ReplayCache
    retry = RetryManager(max_retries=1, base_ms=1)
    circuit = CircuitBreaker(threshold=5, reset_s=30)
    router = TaskRouter(node_token=token, retry=retry, circuit=circuit)
    return FallbackChain(
        router=router,
        replay_cache=ReplayCache(),
        directory_url=directory_url,
        node_token=token,
    )


@respx.mock
async def test_cip_cr1_wire_submit_award_called_on_cip_receipt():
    """CIP-CR1-WIRE (TC-9d): submit_award() fires when response contains cip_receipt."""
    import datetime
    from unittest.mock import AsyncMock, patch

    node = {"node_id": "worker-1", "endpoint": "http://worker1", "region": "eu-central",
            "available": True, "load": 0.1, "active_jobs": 0, "max_concurrent": 4}
    session_key = "sess_TEST01"
    cip_env = {"cip_parent_task_id": "t_PARENT", "cip_session_key": session_key, "cip_role": "worker"}

    now_iso = datetime.datetime.now(datetime.UTC).isoformat()
    exp_iso = (
        datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=300)
    ).isoformat()
    receipt_dict = {
        "task_id": "t_WORKER01",
        "worker_node_id": "worker-1",
        "tokens_used": 42,
        "nonce": "abc123nonce",
        "signature": "deadbeef" * 8,
        "issued_at": now_iso,
        "expires_at": exp_iso,
        "cip_session_key": session_key,
        # sha256 of canonical JSON of result={} (TC-9c §10.3)
        "response_hash": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    }

    async def mock_route(n, tid, intent, payload, tms, cip_envelope=None, **_kwargs):
        sk = (cip_envelope or {}).get("cip_session_key")
        return {
            "status": "success", "task_id": str(tid), "result": {}, "metrics": {"latency_ms": 5},
            "cip_receipt": receipt_dict,
            "trace": {"cip_session_key": sk},
        }

    chain = make_chain_with_award()
    award_mock = AsyncMock(return_value=True)
    with patch.object(chain._router, "route", side_effect=mock_route), \
         patch("iicp_client.proxy.routing.fallback._fire_award", new=award_mock):
        result = await chain.execute(
            [node], uuid4(), "urn:iicp:intent:llm:chat:v1", {}, 5000,
            cip_envelope=cip_env,
        )

    assert result["status"] == "success"
    award_mock.assert_called_once()
    call_kwargs = award_mock.call_args.kwargs
    assert call_kwargs["raw_receipt"] == receipt_dict
    assert call_kwargs["expected_session_key"] == session_key


@respx.mock
async def test_cip_cr1_wire_no_award_without_cip_receipt():
    """CIP-CR1-WIRE: submit_award() NOT called when response has no cip_receipt."""
    from unittest.mock import AsyncMock, patch

    node = {"node_id": "worker-1", "endpoint": "http://worker1", "region": "eu-central",
            "available": True, "load": 0.1, "active_jobs": 0, "max_concurrent": 4}
    cip_env = {"cip_session_key": "sess_TEST02", "cip_role": "worker"}

    async def mock_route(n, tid, intent, payload, tms, cip_envelope=None, **_kwargs):
        sk = (cip_envelope or {}).get("cip_session_key")
        return {
            "status": "success", "task_id": str(tid), "result": {}, "metrics": {"latency_ms": 5},
            "trace": {"cip_session_key": sk},
        }

    chain = make_chain_with_award()
    award_mock = AsyncMock(return_value=True)
    with patch.object(chain._router, "route", side_effect=mock_route), \
         patch("iicp_client.proxy.routing.fallback._fire_award", new=award_mock):
        result = await chain.execute(
            [node], uuid4(), "urn:iicp:intent:llm:chat:v1", {}, 5000,
            cip_envelope=cip_env,
        )

    assert result["status"] == "success"
    award_mock.assert_not_called()


@respx.mock
async def test_cip_cr1_wire_no_award_when_no_replay_cache():
    """CIP-CR1-WIRE: submit_award() NOT called when FallbackChain has no replay_cache (dev mode)."""
    import datetime
    from unittest.mock import AsyncMock, patch

    node = {"node_id": "worker-1", "endpoint": "http://worker1", "region": "eu-central",
            "available": True, "load": 0.1, "active_jobs": 0, "max_concurrent": 4}
    cip_env = {"cip_session_key": "sess_TEST03", "cip_role": "worker"}
    receipt_dict = {
        "task_id": "t_W", "worker_node_id": "worker-1", "tokens_used": 10,
        "nonce": "xyz", "signature": "sig", "issued_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "expires_at": datetime.datetime.now(datetime.UTC).isoformat(),
        # sha256 of canonical JSON of result={} (TC-9c §10.3)
        "response_hash": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    }

    async def mock_route(n, tid, intent, payload, tms, cip_envelope=None, **_kwargs):
        sk = (cip_envelope or {}).get("cip_session_key")
        return {
            "status": "success", "task_id": str(tid), "result": {}, "metrics": {"latency_ms": 5},
            "cip_receipt": receipt_dict,
            "trace": {"cip_session_key": sk},
        }

    chain = make_chain()  # no replay_cache
    award_mock = AsyncMock(return_value=True)
    with patch.object(chain._router, "route", side_effect=mock_route), \
         patch("iicp_client.proxy.routing.fallback._fire_award", new=award_mock):
        result = await chain.execute(
            [node], uuid4(), "urn:iicp:intent:llm:chat:v1", {}, 5000,
            cip_envelope=cip_env,
        )

    assert result["status"] == "success"
    award_mock.assert_not_called()


@respx.mock
async def test_cip_response_hash_mismatch_discards_retries_next():
    """IICP-E2E-04: tampered response_hash in CIP receipt — proxy discards and tries next node.

    TC-9c §10.3: coordinator MUST independently verify response_hash against the canonical
    SHA-256 of the result field. A mismatch MUST discard the response and continue to the
    next available node (fallback chain proceeds).
    """
    from unittest.mock import patch

    cip_env = {"cip_role": "worker", "cip_session_key": "sess-e2e04"}
    n1 = {"node_id": "worker-tampered", "endpoint": "http://n1", "region": "eu-central", "available": True}
    n2 = {"node_id": "worker-honest", "endpoint": "http://n2", "region": "eu-central", "available": True}
    calls: list[str] = []

    correct_hash = __import__("hashlib").sha256(b'{"answer":"hi"}').hexdigest()

    async def mock_route(n, tid, intent, payload, tms, cip_envelope=None, **_kwargs):
        nid = n.get("node_id", "")
        calls.append(nid)
        sk = (cip_envelope or {}).get("cip_session_key")
        result = {"answer": "hi"}
        if nid == "worker-tampered":
            return {
                "status": "success", "task_id": str(tid), "result": result,
                "metrics": {"latency_ms": 5},
                "cip_receipt": {"response_hash": "00" * 32},
                "trace": {"cip_session_key": sk},
            }
        return {
            "status": "success", "task_id": str(tid), "result": result,
            "metrics": {"latency_ms": 5},
            "cip_receipt": {"response_hash": correct_hash},
            "trace": {"cip_session_key": sk},
        }

    chain = make_chain()
    with patch.object(chain._router, "route", side_effect=mock_route):
        result = await chain.execute(
            [n1, n2], uuid4(), "urn:iicp:intent:llm:chat:v1", {}, 5000,
            cip_envelope=cip_env,
        )

    assert calls == ["worker-tampered", "worker-honest"], "should discard tampered node and try next"
    assert result["status"] == "success"
    assert result.get("result", {}).get("answer") == "hi"


@respx.mock
async def test_cip_response_hash_missing_discards_node():
    """IICP-E2E-04: CIP receipt with missing response_hash field is discarded (TC-9c §10.3)."""
    from unittest.mock import patch

    cip_env = {"cip_role": "worker", "cip_session_key": "sess-e2e04b"}
    node = {"node_id": "worker-no-hash", "endpoint": "http://n1", "region": "eu-central", "available": True}

    async def mock_route(n, tid, intent, payload, tms, cip_envelope=None, **_kwargs):
        sk = (cip_envelope or {}).get("cip_session_key")
        return {
            "status": "success", "task_id": str(tid), "result": {},
            "metrics": {"latency_ms": 5},
            "cip_receipt": {},  # no response_hash — missing field
            "trace": {"cip_session_key": sk},
        }

    chain = make_chain()
    with patch.object(chain._router, "route", side_effect=mock_route):
        result = await chain.execute(
            [node], uuid4(), "urn:iicp:intent:llm:chat:v1", {}, 5000,
            cip_envelope=cip_env,
        )

    assert result["status"] == "error"
    assert result["error"]["code"] == "no_available_node"
