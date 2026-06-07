"""Phase 5A CIP Coordinator unit tests — §2.2 normative gate coverage.

Each test is tagged with the normative MUST requirement it exercises from
spec/iicp-cooperative-inference.md §2.2 Consumer Activation and §10.2/§10.4.
No live infrastructure or fixtures required.
"""
from __future__ import annotations

import time

import pytest

from iicp_client.proxy.cip.coordinator import (
    CIPDispatchConfig,
    CIPPrivacyConfig,
    CIPReceipt,
    CIPStrategy,
    CIPWorkerReceipt,
    DispatchDecision,
    DispatchResult,
    ReplayCache,
    build_cip_envelope,
    cip_exhaustion_result,
    compute_worker_timeout_s,
    decide_dispatch,
    make_session_key,
    sign_receipt,
    submit_award,
    validate_cip_request_fields,
    verify_receipt_signature,
)
from iicp_client.proxy.cip.strategies import LocalFirstStrategy, NodeInfo, SessionBudgetTracker
from iicp_client.proxy.otel_tracer import cip_dispatch_span

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _cfg(**kwargs: object) -> CIPDispatchConfig:
    """Build a CIPDispatchConfig with enabled=True and safe defaults."""
    kwargs.setdefault("enabled", True)
    return CIPDispatchConfig(**kwargs)  # type: ignore[arg-type]


def _decide(**kwargs: object) -> DispatchDecision:
    """Call decide_dispatch with safe test defaults."""
    kwargs.setdefault("task_id", "test-task-id")
    kwargs.setdefault("estimated_credits", 1.0)
    kwargs.setdefault("sensitivity", None)
    kwargs.setdefault("eligible_workers", ["worker-1"])
    return decide_dispatch(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Gate 1 — enabled check (§2.2 ¶1)
# ---------------------------------------------------------------------------


def test_disabled_config_returns_local():
    """§2.2: MUST NOT dispatch CIP sub-tasks unless enabled = true."""
    config = CIPDispatchConfig(enabled=False)
    result = _decide(eligible_workers=["w1"], config=config)
    assert result.result == DispatchResult.LOCAL
    assert result.cip_session_key is None


def test_enabled_config_can_reach_remote():
    """enabled=True with eligible workers and no blocking gates → REMOTE."""
    config = _cfg()
    result = _decide(config=config)
    assert result.result == DispatchResult.REMOTE


# ---------------------------------------------------------------------------
# Gate 2 — credit check (§2.2 ¶2)
# ---------------------------------------------------------------------------


def test_credit_exceeds_limit_returns_local():
    """§2.2: If estimated_credits > max_credits_per_task, MUST fall back to local."""
    config = _cfg(max_credits_per_task=5.0)
    result = _decide(estimated_credits=5.01, config=config)
    assert result.result == DispatchResult.LOCAL


def test_credits_at_exact_limit_passes():
    """Credits == max_credits_per_task is still allowed (boundary: strictly greater triggers gate)."""
    config = _cfg(max_credits_per_task=10.0)
    result = _decide(estimated_credits=10.0, config=config)
    assert result.result == DispatchResult.REMOTE


# ---------------------------------------------------------------------------
# Gate 2c — consumer S-Credit balance (billing §10.1, decision B-A)
# #404: each fails without the Gate 2c logic (silent dispatch / wrong outcome).
# ---------------------------------------------------------------------------


def test_insufficient_balance_local_first_falls_back_to_local():
    """B-A: balance < routing cost under local-first → graceful local fallback, not an error."""
    config = _cfg(strategy=CIPStrategy.LOCAL_FIRST)
    result = _decide(estimated_credits=5.0, consumer_balance=1.0, config=config)
    assert result.result == DispatchResult.LOCAL


def test_insufficient_balance_remote_first_returns_iicp_e036():
    """B-A: balance < cost with no local fallback (remote-first) → IICP-E036, never silent dispatch."""
    config = _cfg(strategy=CIPStrategy.REMOTE_FIRST)
    result = _decide(estimated_credits=5.0, consumer_balance=1.0, config=config)
    assert result.result == DispatchResult.ERROR
    assert result.error_code == "IICP-E036"


def test_sufficient_balance_does_not_block():
    """balance >= cost → the balance gate is a no-op (proceeds to remote with eligible workers)."""
    config = _cfg(strategy=CIPStrategy.REMOTE_FIRST)
    result = _decide(estimated_credits=1.0, consumer_balance=10.0, config=config)
    assert result.result == DispatchResult.REMOTE


def test_unknown_balance_skips_gate():
    """consumer_balance=None (not fetched) → gate skipped, back-compat behavior preserved."""
    config = _cfg()
    result = _decide(estimated_credits=1.0, consumer_balance=None, config=config)
    assert result.result == DispatchResult.REMOTE


def test_zero_max_credits_raises_at_construction():
    """§2.2: max_credits_per_task MUST be > 0; reject at startup otherwise."""
    with pytest.raises(ValueError, match="MUST be > 0"):
        CIPDispatchConfig(enabled=True, max_credits_per_task=0)


def test_negative_max_credits_raises_at_construction():
    """Negative max_credits_per_task is also invalid."""
    with pytest.raises(ValueError, match="MUST be > 0"):
        CIPDispatchConfig(enabled=True, max_credits_per_task=-1.0)


# ---------------------------------------------------------------------------
# Gate 3 — sensitivity check (§2.2 ¶3 + §10.2)
# ---------------------------------------------------------------------------


def test_high_sensitivity_blocked_by_default():
    """§10.2: MUST NOT dispatch task with sensitivity='high' when send_sensitive_prompts=false."""
    config = _cfg()  # send_sensitive_prompts defaults to False
    result = _decide(sensitivity="high", config=config)
    assert result.result == DispatchResult.LOCAL


def test_high_sensitivity_allowed_when_opted_in():
    """Operator may override by setting send_sensitive_prompts=true."""
    config = _cfg(privacy=CIPPrivacyConfig(send_sensitive_prompts=True))
    result = _decide(sensitivity="high", config=config)
    assert result.result == DispatchResult.REMOTE


def test_low_sensitivity_not_blocked():
    """Tasks with sensitivity='low' are not blocked by the privacy gate."""
    config = _cfg()
    result = _decide(sensitivity="low", config=config)
    assert result.result == DispatchResult.REMOTE


# ---------------------------------------------------------------------------
# Gate 4 — eligible workers check (§2.2 + IICP-E022)
# ---------------------------------------------------------------------------


def test_no_workers_local_first_returns_local():
    """§2.2: local-first with no workers → fall back to local execution."""
    config = _cfg(strategy=CIPStrategy.LOCAL_FIRST)
    result = _decide(eligible_workers=[], config=config)
    assert result.result == DispatchResult.LOCAL
    assert result.error_code is None


def test_no_workers_remote_first_returns_iicp_e022():
    """§2.2: MUST return IICP-E022 when no eligible remote workers and cannot complete locally."""
    config = _cfg(strategy=CIPStrategy.REMOTE_FIRST)
    result = _decide(eligible_workers=[], config=config)
    assert result.result == DispatchResult.ERROR
    assert result.error_code == "IICP-E022"


def test_balanced_strategy_with_workers_returns_remote():
    """§2.2 balanced: when workers are available, balanced strategy routes to remote.

    Balanced is a supported CIPStrategy value (local-first | remote-first | balanced).
    With eligible workers, balanced goes REMOTE (same as remote-first in Phase 5;
    Phase 6 will add load-aware selection between local and remote).
    """
    config = _cfg(strategy=CIPStrategy.BALANCED)
    result = _decide(eligible_workers=["worker-1"], config=config)
    assert result.result == DispatchResult.REMOTE
    assert result.cip_session_key is not None


def test_balanced_strategy_no_workers_returns_iicp_e022():
    """§2.2 balanced: with no workers, balanced returns IICP-E022 (same as remote-first).

    The balanced strategy does not fall back to local when no workers are available —
    only local-first does that (§2.2 ¶4). Balanced/remote-first both return IICP-E022.
    """
    config = _cfg(strategy=CIPStrategy.BALANCED)
    result = _decide(eligible_workers=[], config=config)
    assert result.result == DispatchResult.ERROR
    assert result.error_code == "IICP-E022"


# ---------------------------------------------------------------------------
# Session key (§10.4)
# ---------------------------------------------------------------------------


def test_remote_dispatch_includes_session_key():
    """§10.4: successful remote dispatch MUST include a cip_session_key."""
    config = _cfg()
    result = _decide(config=config)
    assert result.result == DispatchResult.REMOTE
    assert result.cip_session_key is not None
    assert len(result.cip_session_key) == 64  # SHA-256 hex digest


def test_session_keys_are_unique_per_dispatch():
    """§10.4: per-session random salt ensures keys are unique across dispatches."""
    k1 = make_session_key("same-task")
    k2 = make_session_key("same-task")
    assert k1 != k2, "session keys must be unique — salt must be random"


def test_local_dispatch_has_no_session_key():
    """LOCAL decisions do not carry a cip_session_key (no remote session to bind)."""
    config = CIPDispatchConfig(enabled=False)
    result = _decide(config=config)
    assert result.result == DispatchResult.LOCAL
    assert result.cip_session_key is None


# ---------------------------------------------------------------------------
# CIPReceipt — TC-9b nonce (§10, ADR-012)
# ---------------------------------------------------------------------------


def test_cip_receipt_has_nonce():
    """TC-9b: every receipt MUST carry a nonce for replay detection."""
    receipt = CIPReceipt(
        task_id="t1", worker_id="w1", tokens_used=100,
        credits_charged=1.0, issued_at=time.time(),
    )
    assert receipt.nonce
    assert len(receipt.nonce) == 32  # secrets.token_hex(16) → 32-char hex


def test_cip_receipt_nonces_are_unique():
    """TC-9b: each receipt instance generates a distinct nonce."""
    kwargs = dict(task_id="t1", worker_id="w1", tokens_used=100,
                  credits_charged=1.0, issued_at=0.0)
    r1 = CIPReceipt(**kwargs)
    r2 = CIPReceipt(**kwargs)
    assert r1.nonce != r2.nonce


def test_cip_receipt_accepts_explicit_nonce():
    """Callers may supply an explicit nonce (e.g., from a signed payload)."""
    receipt = CIPReceipt(
        task_id="t1", worker_id="w1", tokens_used=10,
        credits_charged=0.1, issued_at=0.0, nonce="fixed-nonce",
    )
    assert receipt.nonce == "fixed-nonce"


# ---------------------------------------------------------------------------
# ReplayCache — TC-9b replay detection (§10, ADR-012)
# ---------------------------------------------------------------------------


def test_replay_cache_first_nonce_is_not_replay():
    """TC-9b: a nonce seen for the first time MUST NOT be flagged as replay."""
    cache = ReplayCache()
    assert not cache.is_replay("nonce-first")


def test_replay_cache_second_use_is_replay():
    """TC-9b: a nonce seen a second time MUST be flagged as replay."""
    cache = ReplayCache()
    cache.is_replay("nonce-abc")
    assert cache.is_replay("nonce-abc")


def test_replay_cache_different_nonces_are_independent():
    """Two distinct nonces are each accepted on first use."""
    cache = ReplayCache()
    assert not cache.is_replay("nonce-1")
    assert not cache.is_replay("nonce-2")


def test_replay_cache_expired_nonce_is_accepted_again():
    """TC-9b: after the window expires, an evicted nonce is no longer blocked."""
    cache = ReplayCache(window_seconds=0.02)
    cache.is_replay("nonce-expiring")
    time.sleep(0.05)
    assert not cache.is_replay("nonce-expiring")


def test_replay_cache_does_not_expire_within_window():
    """A nonce within the retention window is still blocked."""
    cache = ReplayCache(window_seconds=60.0)
    cache.is_replay("nonce-live")
    assert cache.is_replay("nonce-live")


# ---------------------------------------------------------------------------
# cip_dispatch_span — TRACE-05 (ADR-014 D3)
# ---------------------------------------------------------------------------


def test_cip_dispatch_span_is_a_context_manager():
    """TRACE-05: cip_dispatch_span must yield without raising (no-op fallback)."""
    with cip_dispatch_span(task_id="t1", strategy="local-first") as span:
        span.set_attribute("test.key", "value")


# ---------------------------------------------------------------------------
# TC-9a — signed receipts (§10.3, ADR-012)
# ---------------------------------------------------------------------------

_SECRET = "test-session-key-abc123"


def _receipt(**kwargs: object) -> CIPReceipt:
    kwargs.setdefault("task_id", "t1")
    kwargs.setdefault("worker_id", "w1")
    kwargs.setdefault("tokens_used", 100)
    kwargs.setdefault("credits_charged", 1.0)
    kwargs.setdefault("issued_at", 0.0)
    return CIPReceipt(**kwargs)  # type: ignore[arg-type]


def test_sign_receipt_produces_64_char_hex():
    """TC-9a: HMAC-SHA256 signature is a 64-character hex string."""
    r = _receipt()
    sig = sign_receipt(r, _SECRET)
    assert len(sig) == 64
    assert all(c in "0123456789abcdef" for c in sig)


def test_verify_receipt_valid_signature_passes():
    """TC-9a: coordinator MUST accept receipts with a valid signature."""
    r = _receipt()
    r.signature = sign_receipt(r, _SECRET)
    assert verify_receipt_signature(r, _SECRET)


def test_verify_receipt_wrong_secret_rejected():
    """TC-9a: coordinator MUST reject receipts signed with a different key."""
    r = _receipt()
    r.signature = sign_receipt(r, _SECRET)
    assert not verify_receipt_signature(r, "wrong-secret")


def test_verify_receipt_tampered_field_rejected():
    """TC-9a: coordinator MUST reject receipts where any field was altered after signing."""
    r = _receipt()
    r.signature = sign_receipt(r, _SECRET)
    r.credits_charged = 9999.0  # tamper
    assert not verify_receipt_signature(r, _SECRET)


def test_verify_receipt_no_signature_rejected():
    """TC-9a: unsigned receipts (signature=None) MUST be rejected."""
    r = _receipt()
    assert r.signature is None
    assert not verify_receipt_signature(r, _SECRET)


def test_sign_receipt_is_deterministic_for_same_fields():
    """Same receipt fields and secret always produce the same signature."""
    r = _receipt(nonce="fixed-nonce")
    assert sign_receipt(r, _SECRET) == sign_receipt(r, _SECRET)


# ---------------------------------------------------------------------------
# TC-9d — submit_award: coordinator award wiring (§7, ADR-012)
# ---------------------------------------------------------------------------


def _worker_receipt(**kwargs: object) -> CIPWorkerReceipt:
    """Build a CIPWorkerReceipt with safe test defaults."""
    import secrets as _secrets
    return CIPWorkerReceipt(
        task_id=kwargs.get("task_id", "task-123"),  # type: ignore[arg-type]
        worker_node_id=kwargs.get("worker_node_id", "node-abc"),  # type: ignore[arg-type]
        tokens_used=kwargs.get("tokens_used", 500),  # type: ignore[arg-type]
        nonce=kwargs.get("nonce", _secrets.token_hex(16)),  # type: ignore[arg-type]
        signature=kwargs.get("signature", "a" * 64),  # type: ignore[arg-type]
        issued_at=kwargs.get("issued_at", "2026-01-01T00:00:00Z"),  # type: ignore[arg-type]
        cip_session_key=kwargs.get("cip_session_key", "session-key-1"),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_submit_award_valid_receipt_posts_to_directory(respx_mock):
    """TC-9d: valid receipt with matching session key → POST /v1/credits/award."""
    import httpx as _httpx
    receipt = _worker_receipt(nonce="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
    cache = ReplayCache()
    respx_mock.post("https://dir.test/api/v1/credits/award").mock(
        return_value=_httpx.Response(200, json={"awarded": 0.5})
    )
    result = await submit_award(
        receipt=receipt,
        expected_session_key=receipt.cip_session_key,
        replay_cache=cache,
        directory_url="https://dir.test",
        node_token="tok",
    )
    assert result is True


@pytest.mark.asyncio
async def test_submit_award_replay_nonce_rejected():
    """TC-9b + TC-9d: coordinator rejects a replayed nonce before network call."""
    receipt = _worker_receipt(nonce="deaddead" * 4)
    cache = ReplayCache()
    cache.is_replay(receipt.nonce)  # first use — marks as seen
    result = await submit_award(
        receipt=receipt,
        expected_session_key=receipt.cip_session_key,
        replay_cache=cache,
        directory_url="https://dir.test",
        node_token="tok",
    )
    assert result is False


@pytest.mark.asyncio
async def test_submit_award_session_key_mismatch_rejected():
    """TC-9d: receipt with wrong cip_session_key MUST be rejected (session binding)."""
    receipt = _worker_receipt(cip_session_key="session-A")
    cache = ReplayCache()
    result = await submit_award(
        receipt=receipt,
        expected_session_key="session-B",  # mismatch
        replay_cache=cache,
        directory_url="https://dir.test",
        node_token="tok",
    )
    assert result is False


@pytest.mark.asyncio
async def test_submit_award_null_expected_session_allows_any_key():
    """TC-9d: expected_session_key=None skips session binding check."""
    import httpx as _httpx
    import respx
    receipt = _worker_receipt(cip_session_key="any-key", nonce="e" * 32)
    cache = ReplayCache()
    with respx.mock:
        respx.post("https://dir.test/api/v1/credits/award").mock(
            return_value=_httpx.Response(200, json={"awarded": 0.5})
        )
        result = await submit_award(
            receipt=receipt,
            expected_session_key=None,
            replay_cache=cache,
            directory_url="https://dir.test",
            node_token="tok",
        )
    assert result is True


@pytest.mark.asyncio
async def test_submit_award_directory_422_returns_false():
    """TC-9d: directory IICP-E027 rejection → submit_award returns False."""
    import httpx as _httpx
    import respx
    receipt = _worker_receipt(nonce="f" * 32)
    cache = ReplayCache()
    with respx.mock:
        respx.post("https://dir.test/api/v1/credits/award").mock(
            return_value=_httpx.Response(422, json={"error": {"code": "IICP-E027"}})
        )
        result = await submit_award(
            receipt=receipt,
            expected_session_key=receipt.cip_session_key,
            replay_cache=cache,
            directory_url="https://dir.test",
            node_token="tok",
        )
    assert result is False


@pytest.mark.asyncio
async def test_submit_award_network_error_returns_false():
    """TC-9d: network error during award submission → returns False (non-raising)."""
    import httpx as _httpx
    import respx
    receipt = _worker_receipt(nonce="0" * 32)
    cache = ReplayCache()
    with respx.mock:
        respx.post("https://dir.test/api/v1/credits/award").mock(
            side_effect=_httpx.ConnectError("refused")
        )
        result = await submit_award(
            receipt=receipt,
            expected_session_key=receipt.cip_session_key,
            replay_cache=cache,
            directory_url="https://dir.test",
            node_token="tok",
        )
    assert result is False


def test_worker_receipt_from_dict_roundtrips():
    """CIPWorkerReceipt.from_dict() correctly parses all fields."""
    data = {
        "task_id": "t-1",
        "worker_node_id": "n-1",
        "tokens_used": 200,
        "nonce": "a" * 32,
        "signature": "b" * 64,
        "issued_at": "2026-01-01T00:00:00Z",
        "cip_session_key": "sk",
        "cip_parent_task_id": "parent-1",
    }
    r = CIPWorkerReceipt.from_dict(data)
    assert r.task_id == "t-1"
    assert r.tokens_used == 200
    assert r.cip_parent_task_id == "parent-1"


@pytest.mark.asyncio
async def test_submit_award_amount_calculation():
    """TC-9d: tokens_used / tokens_per_credit determines award amount sent to directory."""
    import httpx as _httpx
    import respx
    receipt = _worker_receipt(tokens_used=2000, nonce="c" * 32)
    cache = ReplayCache()
    captured = {}
    with respx.mock:
        def _capture(request):
            import json
            captured["body"] = json.loads(request.content)
            return _httpx.Response(200, json={"awarded": 2.0})
        respx.post("https://dir.test/api/v1/credits/award").mock(side_effect=_capture)
        await submit_award(
            receipt=receipt,
            expected_session_key=receipt.cip_session_key,
            replay_cache=cache,
            directory_url="https://dir.test",
            node_token="tok",
            tokens_per_credit=1000.0,
        )
    assert captured["body"]["amount"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# CIP Consumer mode — dispatch→award integration (mocked, no live infra)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_cip_flow_dispatch_then_award():
    """CIP-INT: full consumer flow: decide_dispatch → REMOTE → submit_award accepted."""
    import secrets as _sec

    import httpx as _httpx
    import respx
    config = _cfg(enabled=True, max_credits_per_task=5.0)
    task_id = "flow-task-1"
    _session_nonce = _sec.token_hex(16)
    receipt_nonce = _sec.token_hex(16)

    # Step 1: dispatch decision
    decision = decide_dispatch(
        task_id=task_id,
        estimated_credits=2.0,
        sensitivity=None,
        eligible_workers=["worker-node-1"],
        config=config,
    )
    assert decision.result == DispatchResult.REMOTE
    assert decision.cip_session_key is not None

    # Step 2: simulate worker returning a CIPWorkerReceipt
    receipt = CIPWorkerReceipt(
        task_id=task_id,
        worker_node_id="worker-node-1",
        tokens_used=1500,
        nonce=receipt_nonce,
        signature="a" * 64,
        issued_at="2026-01-01T00:00:00Z",
        cip_session_key=decision.cip_session_key,
    )

    # Step 3: submit_award
    cache = ReplayCache()
    with respx.mock:
        respx.post("https://dir.test/api/v1/credits/award").mock(
            return_value=_httpx.Response(200, json={"awarded": 1.5})
        )
        result = await submit_award(
            receipt=receipt,
            expected_session_key=decision.cip_session_key,
            replay_cache=cache,
            directory_url="https://dir.test",
            node_token="tok",
        )
    assert result is True


@pytest.mark.asyncio
async def test_cip_flow_credit_limit_prevents_remote():
    """CIP consumer: estimated_credits over limit → LOCAL (no award needed)."""
    config = _cfg(enabled=True, max_credits_per_task=1.0)
    decision = decide_dispatch(
        task_id="t-limit",
        estimated_credits=5.0,  # over limit
        sensitivity=None,
        eligible_workers=["worker-1"],
        config=config,
    )
    assert decision.result == DispatchResult.LOCAL
    assert decision.cip_session_key is None


@pytest.mark.asyncio
async def test_cip_flow_session_key_binds_receipt_to_dispatch():
    """CIP consumer: receipt with wrong session key cannot be awarded."""
    config = _cfg(enabled=True)
    decision = decide_dispatch(
        task_id="t-bind",
        estimated_credits=1.0,
        sensitivity=None,
        eligible_workers=["worker-1"],
        config=config,
    )
    # Forge a receipt with a different session key
    receipt = CIPWorkerReceipt(
        task_id="t-bind",
        worker_node_id="worker-1",
        tokens_used=500,
        nonce="b" * 32,
        signature="c" * 64,
        issued_at="2026-01-01T00:00:00Z",
        cip_session_key="wrong-session-key",
    )
    cache = ReplayCache()
    result = await submit_award(
        receipt=receipt,
        expected_session_key=decision.cip_session_key,
        replay_cache=cache,
        directory_url="https://dir.test",
        node_token="tok",
    )
    assert result is False  # session binding prevents award


@pytest.mark.asyncio
async def test_cip_flow_disabled_consumer_local_only():
    """CIP consumer disabled: all tasks go LOCAL, no session key, no award."""
    config = CIPDispatchConfig(enabled=False)
    for _ in range(3):
        d = decide_dispatch(
            task_id="t-dis",
            estimated_credits=0.1,
            sensitivity=None,
            eligible_workers=["worker-1"],
            config=config,
        )
        assert d.result == DispatchResult.LOCAL
        assert d.cip_session_key is None


@pytest.mark.asyncio
async def test_cip_flow_sensitive_task_stays_local_by_default():
    """CIP consumer: high sensitivity task MUST stay local unless opted in."""
    config = _cfg(enabled=True, max_credits_per_task=100.0)
    d = decide_dispatch(
        task_id="t-sens",
        estimated_credits=1.0,
        sensitivity="high",
        eligible_workers=["worker-1"],
        config=config,
    )
    assert d.result == DispatchResult.LOCAL


@pytest.mark.asyncio
async def test_cip_flow_sensitive_task_remote_when_opted_in():
    """CIP consumer: high sensitivity goes REMOTE when send_sensitive_prompts=True."""
    import httpx as _httpx
    import respx
    config = _cfg(enabled=True, privacy=CIPPrivacyConfig(send_sensitive_prompts=True))
    d = decide_dispatch(
        task_id="t-sens-opt",
        estimated_credits=1.0,
        sensitivity="high",
        eligible_workers=["worker-1"],
        config=config,
    )
    assert d.result == DispatchResult.REMOTE

    receipt = CIPWorkerReceipt(
        task_id="t-sens-opt",
        worker_node_id="worker-1",
        tokens_used=100,
        nonce="d" * 32,
        signature="e" * 64,
        issued_at="2026-01-01T00:00:00Z",
        cip_session_key=d.cip_session_key,
    )
    cache = ReplayCache()
    with respx.mock:
        respx.post("https://dir.test/api/v1/credits/award").mock(
            return_value=_httpx.Response(200, json={"awarded": 0.1})
        )
        result = await submit_award(
            receipt=receipt,
            expected_session_key=d.cip_session_key,
            replay_cache=cache,
            directory_url="https://dir.test",
            node_token="tok",
        )
    assert result is True


# ---------------------------------------------------------------------------
# Issue #78: LocalFirstStrategy + session credit budget (#CIP-P1)
# ---------------------------------------------------------------------------


def test_local_first_prefers_local_node():
    """§2.2: local-first strategy returns LOCAL when a loopback node matches intent."""
    nodes = [
        NodeInfo(node_id="local-1", endpoint="http://localhost:8080", intent="llm:chat"),
        NodeInfo(node_id="remote-1", endpoint="https://remote.example.com", intent="llm:chat"),
    ]
    strategy = LocalFirstStrategy()
    assert strategy.should_dispatch_remote(nodes, intent="llm:chat") is False


def test_local_first_falls_back_to_remote():
    """§2.2: local-first falls back to remote when no local node is available."""
    nodes = [
        NodeInfo(node_id="remote-1", endpoint="https://remote.example.com", intent="llm:chat"),
    ]
    strategy = LocalFirstStrategy()
    assert strategy.should_dispatch_remote(nodes, intent="llm:chat") is True


def test_local_first_empty_node_list_goes_remote():
    """§2.2: local-first with no nodes available → should_dispatch_remote True."""
    strategy = LocalFirstStrategy()
    assert strategy.should_dispatch_remote([], intent="llm:chat") is True


def test_local_first_strategy_blocks_dispatch_in_decide():
    """decide_dispatch uses LocalFirstStrategy when strategy=local-first and node_list given."""
    nodes = [
        NodeInfo(node_id="local-1", endpoint="http://127.0.0.1:8080"),
    ]
    config = _cfg(strategy=CIPStrategy.LOCAL_FIRST)
    d = decide_dispatch(
        task_id="t-local",
        estimated_credits=1.0,
        sensitivity=None,
        eligible_workers=["remote-1"],
        config=config,
        node_list=nodes,
        intent="llm:chat",
    )
    assert d.result == DispatchResult.LOCAL


def test_local_first_falls_back_remote_when_no_local_in_decide():
    """decide_dispatch dispatches remote when no local node and workers available."""
    nodes = [
        NodeInfo(node_id="remote-1", endpoint="https://remote.example.com"),
    ]
    config = _cfg(strategy=CIPStrategy.LOCAL_FIRST)
    d = decide_dispatch(
        task_id="t-remote-fallback",
        estimated_credits=1.0,
        sensitivity=None,
        eligible_workers=["remote-1"],
        config=config,
        node_list=nodes,
        intent="llm:chat",
    )
    assert d.result == DispatchResult.REMOTE


def test_session_budget_exhausted_goes_local():
    """§2.2 session_credit_budget: task blocked when session budget exhausted."""
    tracker = SessionBudgetTracker(session_credit_budget=5.0)
    tracker.record_spend(4.5)
    config = _cfg()
    d = decide_dispatch(
        task_id="t-budget",
        estimated_credits=1.0,
        sensitivity=None,
        eligible_workers=["worker-1"],
        config=config,
        session_tracker=tracker,
    )
    assert d.result == DispatchResult.LOCAL


def test_session_budget_allows_within_limit():
    """§2.2 session_credit_budget: task allowed when spend is within remaining budget."""
    tracker = SessionBudgetTracker(session_credit_budget=10.0)
    tracker.record_spend(3.0)
    config = _cfg()
    d = decide_dispatch(
        task_id="t-budget-ok",
        estimated_credits=2.0,
        sensitivity=None,
        eligible_workers=["worker-1"],
        config=config,
        session_tracker=tracker,
    )
    assert d.result == DispatchResult.REMOTE


def test_session_budget_unlimited_by_default():
    """§2.2: session_credit_budget=None means no session ceiling."""
    tracker = SessionBudgetTracker(session_credit_budget=None)
    tracker.record_spend(10000.0)
    assert tracker.can_spend(9999.0) is True
    config = _cfg(max_credits_per_task=5000.0)  # high per-task limit; session is unlimited
    d = decide_dispatch(
        task_id="t-unlimited",
        estimated_credits=1.0,
        sensitivity=None,
        eligible_workers=["worker-1"],
        config=config,
        session_tracker=tracker,
    )
    assert d.result == DispatchResult.REMOTE


def test_session_budget_tracker_remaining():
    """SessionBudgetTracker.remaining decrements correctly."""
    tracker = SessionBudgetTracker(session_credit_budget=10.0)
    tracker.record_spend(3.0)
    assert tracker.remaining == pytest.approx(7.0)
    tracker.record_spend(7.0)
    assert tracker.remaining == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# CIP-CALL-01: build_cip_envelope helper (S.12 §4.1, §10.4, CIP-04)
# ---------------------------------------------------------------------------


def test_build_cip_envelope_remote_decision_produces_correct_fields():
    """CIP-CALL-01: build_cip_envelope MUST return cip_role='worker', cip_session_key, cip_parent_task_id."""
    decision = DispatchDecision(result=DispatchResult.REMOTE, cip_session_key="sess-abc-999")
    env = build_cip_envelope(decision, parent_task_id="task-parent-001")
    assert env is not None
    assert env["cip_role"] == "worker"
    assert env["cip_session_key"] == "sess-abc-999"
    assert env["cip_parent_task_id"] == "task-parent-001"


def test_build_cip_envelope_local_decision_returns_none():
    """CIP-CALL-01: build_cip_envelope MUST return None for LOCAL decisions (non-CIP dispatch)."""
    decision = DispatchDecision(result=DispatchResult.LOCAL, cip_session_key=None)
    env = build_cip_envelope(decision, parent_task_id="task-parent-002")
    assert env is None


def test_build_cip_envelope_error_decision_returns_none():
    """CIP-CALL-01: build_cip_envelope MUST return None for ERROR decisions."""
    decision = DispatchDecision(result=DispatchResult.ERROR, error_code="IICP-E022")
    env = build_cip_envelope(decision, parent_task_id="task-parent-003")
    assert env is None


def test_build_cip_envelope_integrates_with_decide_dispatch():
    """CIP-CALL-01: build_cip_envelope round-trip with decide_dispatch produces valid envelope."""
    config = _cfg()
    decision = decide_dispatch(
        task_id="task-roundtrip",
        estimated_credits=1.0,
        sensitivity=None,
        eligible_workers=["worker-1"],
        config=config,
    )
    assert decision.result == DispatchResult.REMOTE
    env = build_cip_envelope(decision, parent_task_id="task-roundtrip")
    assert env is not None
    assert env["cip_role"] == "worker"
    assert env["cip_session_key"] == decision.cip_session_key
    assert env["cip_parent_task_id"] == "task-roundtrip"


# ---------------------------------------------------------------------------
# Gate 6 — replica count check (CIP-CALL-04, §2.2)
# §2.2: MUST fan out to exactly cip.replicas; MUST NOT dispatch to a reduced count
# ---------------------------------------------------------------------------


def test_insufficient_workers_for_replicas_returns_e022():
    """CIP-CALL-04: MUST return IICP-E022 when eligible workers < cip.replicas (S.12 §2.2).

    The coordinator MUST NOT dispatch to a reduced replica count when fewer
    eligible workers exist than the requested replica count.
    """
    config = _cfg(strategy=CIPStrategy.REMOTE_FIRST)
    result = _decide(eligible_workers=["worker-1", "worker-2"], config=config, replicas=3)
    assert result.result == DispatchResult.ERROR
    assert result.error_code == "IICP-E022"


def test_insufficient_workers_local_first_falls_back_to_local():
    """CIP-CALL-04: local-first strategy falls back to local when eligible < replicas."""
    config = _cfg(strategy=CIPStrategy.LOCAL_FIRST)
    result = _decide(eligible_workers=["worker-1"], config=config, replicas=3)
    assert result.result == DispatchResult.LOCAL


def test_exact_worker_count_meets_replica_requirement():
    """CIP-CALL-04: eligible == replicas satisfies the gate — REMOTE allowed."""
    config = _cfg(strategy=CIPStrategy.REMOTE_FIRST)
    result = _decide(eligible_workers=["worker-1", "worker-2", "worker-3"], config=config, replicas=3)
    assert result.result == DispatchResult.REMOTE


def test_replicas_equals_one_never_triggers_gate():
    """CIP-CALL-04: replicas=1 (default) does not activate the reduced-count check."""
    config = _cfg()
    result = _decide(eligible_workers=["worker-1"], config=config, replicas=1)
    assert result.result == DispatchResult.REMOTE


# ---------------------------------------------------------------------------
# §3.1 + §6 — worker_timeout / IICP-E024 (CIP-TIMEOUT-01)
# §3.1: If zero workers respond within worker_timeout, Coordinator MUST fall
# back to local (if available) or return IICP-E024 (all workers timed out).
# §6: worker_timeout = coordinator_timeout × 0.6
# ---------------------------------------------------------------------------


def test_compute_worker_timeout_is_60pct_of_coordinator():
    """CIP-TIMEOUT-01: §6 formula — worker_timeout = coordinator_timeout × 0.6."""
    assert compute_worker_timeout_s(30_000) == pytest.approx(18.0)
    assert compute_worker_timeout_s(10_000) == pytest.approx(6.0)
    assert compute_worker_timeout_s(0) == pytest.approx(0.0)


def test_cip_exhaustion_fallback_to_local_returns_local():
    """CIP-TIMEOUT-01: §3.1 MUST — fallback_to_local=True yields LOCAL, not IICP-E024."""
    result = cip_exhaustion_result(fallback_to_local=True)
    assert result.result == DispatchResult.LOCAL
    assert result.error_code is None


def test_cip_exhaustion_no_local_returns_iicp_e024():
    """CIP-TIMEOUT-01: §3.1 MUST — fallback_to_local=False yields IICP-E024."""
    result = cip_exhaustion_result(fallback_to_local=False)
    assert result.result == DispatchResult.ERROR
    assert result.error_code == "IICP-E024"


def test_cip_dispatch_config_default_coordinator_timeout():
    """CIP-TIMEOUT-01: CIPDispatchConfig default coordinator_timeout_ms is 30 000 ms (§6)."""
    config = CIPDispatchConfig(max_credits_per_task=1.0)
    assert config.coordinator_timeout_ms == 30_000


# ---------------------------------------------------------------------------
# CIP-VAL-01: parse-time validation — cip.policy, cip.replicas, cip.quorum
# S.12 §5.2: all four checks MUST occur before any worker selection or dispatch
# ---------------------------------------------------------------------------


def test_validate_no_cip_block_returns_none():
    """CIP-VAL-01: body without a cip block is not a CIP request — no validation error."""
    assert validate_cip_request_fields({"payload": "x"}) is None


def test_validate_cip_block_not_dict_returns_none():
    """CIP-VAL-01: cip block that is not a dict is ignored — no crash."""
    assert validate_cip_request_fields({"cip": "invalid"}) is None


def test_validate_valid_policy_best_of_n_passes():
    """CIP-VAL-01: cip.policy='best_of_n' is a valid value."""
    assert validate_cip_request_fields({"cip": {"policy": "best_of_n"}}) is None


def test_validate_valid_policy_majority_vote_passes():
    """CIP-VAL-01: cip.policy='majority_vote' is a valid value."""
    assert validate_cip_request_fields({"cip": {"policy": "majority_vote"}}) is None


def test_validate_valid_policy_map_reduce_passes():
    """CIP-VAL-01: cip.policy='map_reduce' is a valid value."""
    assert validate_cip_request_fields({"cip": {"policy": "map_reduce"}}) is None


def test_validate_invalid_policy_returns_e028():
    """CIP-VAL-01: any cip.policy value outside the valid set → IICP-E028."""
    assert validate_cip_request_fields({"cip": {"policy": "unknown"}}) == "IICP-E028"
    assert validate_cip_request_fields({"cip": {"policy": ""}}) == "IICP-E028"
    assert validate_cip_request_fields({"cip": {"policy": "BEST_OF_N"}}) == "IICP-E028"


def test_validate_replicas_bounds_pass():
    """CIP-VAL-01: cip.replicas in [1, 10] are valid."""
    assert validate_cip_request_fields({"cip": {"replicas": 1}}) is None
    assert validate_cip_request_fields({"cip": {"replicas": 10}}) is None
    assert validate_cip_request_fields({"cip": {"replicas": 5}}) is None


def test_validate_replicas_zero_returns_e028():
    """CIP-VAL-01: cip.replicas=0 is below the minimum [1, 10] → IICP-E028."""
    assert validate_cip_request_fields({"cip": {"replicas": 0}}) == "IICP-E028"


def test_validate_replicas_eleven_returns_e028():
    """CIP-VAL-01: cip.replicas=11 exceeds the maximum [1, 10] → IICP-E028."""
    assert validate_cip_request_fields({"cip": {"replicas": 11}}) == "IICP-E028"


def test_validate_majority_vote_odd_replicas_passes():
    """CIP-VAL-01: majority_vote with odd replicas (3, 5, 7) is valid."""
    for n in (3, 5, 7, 9):
        assert validate_cip_request_fields({"cip": {"policy": "majority_vote", "replicas": n}}) is None


def test_validate_majority_vote_even_replicas_returns_e025():
    """CIP-VAL-01: majority_vote with even replicas → IICP-E025 (§3.2)."""
    assert validate_cip_request_fields({"cip": {"policy": "majority_vote", "replicas": 2}}) == "IICP-E025"
    assert validate_cip_request_fields({"cip": {"policy": "majority_vote", "replicas": 4}}) == "IICP-E025"


def test_validate_quorum_at_replicas_passes():
    """CIP-VAL-01: cip.quorum = cip.replicas is valid (exactly meets replicas count)."""
    assert validate_cip_request_fields({"cip": {"replicas": 3, "quorum": 3}}) is None


def test_validate_quorum_exceeds_replicas_returns_e028():
    """CIP-VAL-01: cip.quorum > cip.replicas → IICP-E028."""
    assert validate_cip_request_fields({"cip": {"replicas": 3, "quorum": 4}}) == "IICP-E028"


def test_validate_quorum_without_explicit_replicas_uses_default_one():
    """CIP-VAL-01: quorum > 1 when replicas is absent (default 1) → IICP-E028."""
    assert validate_cip_request_fields({"cip": {"quorum": 2}}) == "IICP-E028"


def test_validate_null_quorum_always_passes():
    """CIP-VAL-01: cip.quorum=null (None in Python) is always valid."""
    assert validate_cip_request_fields({"cip": {"quorum": None, "replicas": 3}}) is None


# ---------------------------------------------------------------------------
# CIP-CR1-PROXY: expires_at in submit_award payload (CreditsController required field)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_award_payload_includes_expires_at(respx_mock):
    """CIP-CR1-PROXY: submit_award MUST include expires_at in POST body (required by CreditsController)."""
    import json

    import httpx as _httpx
    import respx as _r
    receipt = _worker_receipt(nonce="d" * 32)
    receipt.expires_at = "2026-05-22T00:05:00+00:00"
    cache = ReplayCache()
    captured: dict = {}

    with _r.mock:
        def _cap(req):
            captured["body"] = json.loads(req.content)
            return _httpx.Response(200, json={"awarded": 0.5})
        _r.post("https://dir.test/api/v1/credits/award").mock(side_effect=_cap)
        await submit_award(
            receipt=receipt,
            expected_session_key=receipt.cip_session_key,
            replay_cache=cache,
            directory_url="https://dir.test",
            node_token="tok",
        )
    assert "expires_at" in captured["body"], "expires_at must be forwarded to CreditsController"
    assert captured["body"]["expires_at"] == "2026-05-22T00:05:00+00:00"


def test_cip_worker_receipt_from_dict_parses_expires_at():
    """CIP-CR1-PROXY: CIPWorkerReceipt.from_dict() must parse expires_at when present."""
    r = CIPWorkerReceipt.from_dict({
        "task_id": "t1",
        "worker_node_id": "n1",
        "tokens_used": 100,
        "nonce": "a" * 32,
        "signature": "b" * 64,
        "issued_at": "2026-05-22T00:00:00Z",
        "expires_at": "2026-05-22T00:05:00Z",
    })
    assert r.expires_at == "2026-05-22T00:05:00Z"


def test_cip_worker_receipt_from_dict_expires_at_defaults_none():
    """CIP-CR1-PROXY: CIPWorkerReceipt.from_dict() expires_at defaults to None when absent."""
    r = CIPWorkerReceipt.from_dict({
        "task_id": "t1",
        "worker_node_id": "n1",
        "tokens_used": 100,
        "nonce": "a" * 32,
        "signature": "b" * 64,
        "issued_at": "2026-05-22T00:00:00Z",
    })
    assert r.expires_at is None
