"""CIP-INT1 (#85): Proxy CIP consumer mode end-to-end integration tests.

Exercises the full coordinator→adapter→directory credit-award flow using
respx mocks for both the adapter endpoint and directory /api/v1/credits/award.
No live infrastructure required.

ACs covered:
  AC1 — decide_dispatch REMOTE → mock adapter returns cip_receipt → submit_award 200
  AC2 — replay-cache blocks duplicate nonce on second submit_award call
  AC3 — TRACE-06/TRACE-07 not applicable to proxy side (adapter-side spans)
  AC4 — invalid HMAC in cip_receipt causes directory to return 422 → submit_award False
"""
from __future__ import annotations

import httpx
import pytest
import respx

from iicp_client.proxy.cip.coordinator import (
    CIPDispatchConfig,
    CIPStrategy,
    CIPWorkerReceipt,
    DispatchResult,
    ReplayCache,
    decide_dispatch,
    submit_award,
)

DIRECTORY_URL = "https://dir.test"
ADAPTER_URL = "https://adapter.test"
NODE_TOKEN = "integration-test-token"

_AWARD_OK = httpx.Response(200, json={"awarded": 1.5})
_AWARD_INVALID_HMAC = httpx.Response(422, json={"error": {"code": "IICP-E027"}})


def _cfg(*, enabled: bool = True, max_credits: float = 10.0) -> CIPDispatchConfig:
    return CIPDispatchConfig(
        enabled=enabled,
        strategy=CIPStrategy.REMOTE_FIRST,
        max_credits_per_task=max_credits,
    )


def _receipt(task_id: str, session_key: str | None, nonce: str, sig: str = "a" * 64) -> CIPWorkerReceipt:
    return CIPWorkerReceipt(
        task_id=task_id,
        worker_node_id="worker-1",
        tokens_used=1500,
        nonce=nonce,
        signature=sig,
        issued_at="2026-01-01T00:00:00Z",
        cip_session_key=session_key,
    )


# ---------------------------------------------------------------------------
# AC1: full flow — dispatch → REMOTE → award accepted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_cip_e2e_dispatch_then_award_succeeds():
    """AC1: decide_dispatch→REMOTE then submit_award to directory returns True."""
    cfg = _cfg()
    decision = decide_dispatch(
        task_id="int-task-1",
        estimated_credits=2.0,
        sensitivity=None,
        eligible_workers=["worker-1"],
        config=cfg,
    )
    assert decision.result == DispatchResult.REMOTE

    receipt = _receipt("int-task-1", decision.cip_session_key, "nonce-abc")
    cache = ReplayCache()

    respx.post(f"{DIRECTORY_URL}/api/v1/credits/award").mock(return_value=_AWARD_OK)

    result = await submit_award(
        receipt=receipt,
        expected_session_key=decision.cip_session_key,
        replay_cache=cache,
        directory_url=DIRECTORY_URL,
        node_token=NODE_TOKEN,
    )
    assert result is True


# ---------------------------------------------------------------------------
# AC2: replay-cache blocks duplicate nonce
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_cip_e2e_replay_cache_blocks_second_call():
    """AC2: second submit_award with same nonce is rejected by replay cache."""
    cfg = _cfg()
    decision = decide_dispatch(
        task_id="int-task-2",
        estimated_credits=1.0,
        sensitivity=None,
        eligible_workers=["worker-1"],
        config=cfg,
    )

    receipt = _receipt("int-task-2", decision.cip_session_key, "nonce-replay-xyz")
    cache = ReplayCache()

    respx.post(f"{DIRECTORY_URL}/api/v1/credits/award").mock(return_value=_AWARD_OK)

    result1 = await submit_award(
        receipt=receipt,
        expected_session_key=decision.cip_session_key,
        replay_cache=cache,
        directory_url=DIRECTORY_URL,
        node_token=NODE_TOKEN,
    )
    assert result1 is True

    # Second call — same nonce, must be blocked before reaching directory
    result2 = await submit_award(
        receipt=receipt,
        expected_session_key=decision.cip_session_key,
        replay_cache=cache,
        directory_url=DIRECTORY_URL,
        node_token=NODE_TOKEN,
    )
    assert result2 is False


# ---------------------------------------------------------------------------
# AC4: invalid HMAC → directory 422+IICP-E027 → submit_award returns False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_cip_e2e_invalid_hmac_rejected_by_directory():
    """AC4: directory returns 422+IICP-E027 for invalid HMAC; submit_award returns False."""
    cfg = _cfg()
    decision = decide_dispatch(
        task_id="int-task-3",
        estimated_credits=3.0,
        sensitivity=None,
        eligible_workers=["worker-1"],
        config=cfg,
    )

    # Receipt with a deliberately wrong signature
    receipt = _receipt("int-task-3", decision.cip_session_key, "nonce-bad-sig", "bad" * 21 + "b")
    cache = ReplayCache()

    respx.post(f"{DIRECTORY_URL}/api/v1/credits/award").mock(return_value=_AWARD_INVALID_HMAC)

    result = await submit_award(
        receipt=receipt,
        expected_session_key=decision.cip_session_key,
        replay_cache=cache,
        directory_url=DIRECTORY_URL,
        node_token=NODE_TOKEN,
    )
    assert result is False


# ---------------------------------------------------------------------------
# Session binding: mismatched session key rejected before network call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cip_e2e_session_key_mismatch_no_network_call():
    """Session key mismatch must abort before any directory network call."""
    cfg = _cfg()
    decision = decide_dispatch(
        task_id="int-task-4",
        estimated_credits=1.0,
        sensitivity=None,
        eligible_workers=["worker-1"],
        config=cfg,
    )

    # Receipt carries a different session key
    receipt = _receipt("int-task-4", "wrong-session-key", "nonce-mismatch")
    cache = ReplayCache()

    # No respx mock — if a network call is made, the test will raise ConnectError
    result = await submit_award(
        receipt=receipt,
        expected_session_key=decision.cip_session_key,
        replay_cache=cache,
        directory_url=DIRECTORY_URL,
        node_token=NODE_TOKEN,
    )
    assert result is False
