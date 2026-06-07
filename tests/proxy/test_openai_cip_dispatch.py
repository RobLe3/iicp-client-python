# SPDX-License-Identifier: Apache-2.0
"""Unit tests for compute_cip_envelope() — CIP-CALL-01 (S.12 §4.1).

Verifies the shared helper (proxy.cip.dispatch) that evaluates §2.2 consumer
gates and builds the CIP dispatch envelope before task submission. Tests are
isolated from HTTP infrastructure and inject CIPDispatchConfig directly.
"""
from __future__ import annotations

import pytest

from iicp_client.proxy.cip.coordinator import CIPDispatchConfig, CIPStrategy
from iicp_client.proxy.cip.dispatch import (
    CIPInsufficientCredits,
    CIPNoEligibleWorkers,
    compute_cip_envelope,
    resolve_consumer_balance,
)
from iicp_client.proxy.cip.strategies import SessionBudgetTracker


def _cfg(**kwargs) -> CIPDispatchConfig:
    return CIPDispatchConfig(**kwargs)


_NODE_CAPABLE = {"node_id": "n-1", "allow_remote_inference": True}
_NODE_INCAPABLE = {"node_id": "n-2", "allow_remote_inference": False}
_NODE_NO_KEY = {"node_id": "n-3"}


# ── guard rails — always returns None ────────────────────────────────────────

def test_returns_none_when_cip_config_is_none():
    assert compute_cip_envelope([_NODE_CAPABLE], {}, None, "t-1", None) is None


def test_returns_none_when_cip_disabled():
    cfg = _cfg(enabled=False)
    assert compute_cip_envelope([_NODE_CAPABLE], {}, cfg, "t-1", None) is None


def test_returns_none_for_realtime_qos():
    """Realtime QoS uses redundancy path; CIP envelope must not be injected."""
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST)
    assert compute_cip_envelope([_NODE_CAPABLE], {}, cfg, "t-1", "realtime") is None


def test_returns_none_when_no_eligible_workers_local_first():
    """local-first strategy with no CIP-capable nodes resolves to LOCAL."""
    cfg = _cfg(enabled=True, strategy=CIPStrategy.LOCAL_FIRST)
    result = compute_cip_envelope([_NODE_INCAPABLE, _NODE_NO_KEY], {}, cfg, "t-1", None)
    assert result is None


# ── envelope content ─────────────────────────────────────────────────────────

def test_returns_envelope_remote_first_with_eligible_worker():
    """remote-first + eligible worker → REMOTE decision → envelope dict returned."""
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST)
    envelope = compute_cip_envelope([_NODE_CAPABLE], {}, cfg, "task-abc", None)
    assert envelope is not None
    assert envelope["cip_role"] == "worker"
    assert envelope["cip_parent_task_id"] == "task-abc"
    assert "cip_session_key" in envelope
    assert len(envelope["cip_session_key"]) == 64  # SHA-256 hex


def test_envelope_filters_incapable_nodes():
    """Only nodes with allow_remote_inference=True count as eligible workers."""
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST)
    # Mix: one incapable + one capable
    nodes = [_NODE_INCAPABLE, _NODE_CAPABLE]
    envelope = compute_cip_envelope(nodes, {}, cfg, "t-x", None)
    assert envelope is not None  # capable node was found


def test_remote_first_no_capable_nodes_raises_iicp_e022():
    """#471 / 4-C: remote-first + zero eligible workers → raise CIPNoEligibleWorkers
    (IICP-E022), surfaced as a 503 by the adapters — not silently run local.

    Fails without the fix — pre-#471 this collapsed to a None envelope and the
    remote-first request silently downgraded with no signal to the consumer.
    """
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST)
    with pytest.raises(CIPNoEligibleWorkers) as exc:
        compute_cip_envelope([_NODE_INCAPABLE], {}, cfg, "t-err", None)
    assert exc.value.error_code == "IICP-E022"


def test_local_first_no_capable_nodes_falls_back_to_local_no_raise():
    """4-C two-step: under local-first, no eligible workers degrades to local (None),
    never E022 — the 'local if available' half of the two-step."""
    cfg = _cfg(enabled=True, strategy=CIPStrategy.LOCAL_FIRST)
    result = compute_cip_envelope([_NODE_INCAPABLE, _NODE_NO_KEY], {}, cfg, "t-lf-e022", None)
    assert result is None


def test_sensitive_body_blocked_without_opt_in():
    """§10.2: high-sensitivity tasks MUST not dispatch unless send_sensitive_prompts=True."""
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST)
    body = {"sensitivity": "high"}
    result = compute_cip_envelope([_NODE_CAPABLE], body, cfg, "t-s", None)
    assert result is None


# ── trusted_peers enforcement ─────────────────────────────────────────────────

def test_trusted_peers_allows_matching_node():
    """trusted_peers non-empty: node in list is still eligible."""
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST, trusted_peers=["n-1"])
    envelope = compute_cip_envelope([_NODE_CAPABLE], {}, cfg, "t-tp1", None)
    assert envelope is not None


def test_trusted_peers_blocks_unlisted_node():
    """trusted_peers non-empty: node NOT in list is excluded → no eligible workers
    → IICP-E022 (remote-first; #471)."""
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST, trusted_peers=["other-node"])
    with pytest.raises(CIPNoEligibleWorkers):  # n-1 not in trusted list → excluded
        compute_cip_envelope([_NODE_CAPABLE], {}, cfg, "t-tp2", None)


def test_trusted_peers_empty_allows_any():
    """trusted_peers = [] (default): any CIP-capable node is eligible."""
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST, trusted_peers=[])
    envelope = compute_cip_envelope([_NODE_CAPABLE], {}, cfg, "t-tp3", None)
    assert envelope is not None


# ── empty / malformed node_id guard ──────────────────────────────────────────

def test_node_without_node_id_is_excluded():
    """Malformed directory response (allow_remote_inference=True but no node_id) is excluded."""
    _NODE_NO_NODEID = {"allow_remote_inference": True}  # missing node_id entirely
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST)
    # Only malformed node in pool → no eligible workers → IICP-E022 (remote-first; #471)
    with pytest.raises(CIPNoEligibleWorkers):
        compute_cip_envelope([_NODE_NO_NODEID], {}, cfg, "t-mn", None)


def test_node_with_empty_node_id_is_excluded():
    """Node with empty-string node_id is excluded from eligible workers."""
    _NODE_EMPTY_ID = {"node_id": "", "allow_remote_inference": True}
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST)
    with pytest.raises(CIPNoEligibleWorkers):  # excluded → no eligible workers (#471)
        compute_cip_envelope([_NODE_EMPTY_ID], {}, cfg, "t-ei", None)


# ── min_reputation enforcement (CIP-CALL-02) ─────────────────────────────────

def test_min_reputation_zero_allows_node_without_score():
    """min_reputation=0.0 (default): nodes with no reputation_score field are eligible."""
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST, min_reputation=0.0)
    # _NODE_CAPABLE has no reputation_score; 0.0 >= 0.0 → eligible
    envelope = compute_cip_envelope([_NODE_CAPABLE], {}, cfg, "t-rep0", None)
    assert envelope is not None


def test_min_reputation_blocks_low_score_node():
    """§2.2: node with reputation_score below min_reputation is excluded."""
    _NODE_LOW_REP = {"node_id": "n-low", "allow_remote_inference": True, "reputation_score": 0.3}
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST, min_reputation=0.5)
    with pytest.raises(CIPNoEligibleWorkers):  # below threshold → excluded (#471)
        compute_cip_envelope([_NODE_LOW_REP], {}, cfg, "t-rep1", None)


def test_min_reputation_allows_node_at_threshold():
    """Node with reputation_score exactly equal to min_reputation is eligible (inclusive)."""
    _NODE_AT_REP = {"node_id": "n-at", "allow_remote_inference": True, "reputation_score": 0.5}
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST, min_reputation=0.5)
    envelope = compute_cip_envelope([_NODE_AT_REP], {}, cfg, "t-rep2", None)
    assert envelope is not None


def test_min_reputation_filters_mixed_pool():
    """Only nodes meeting min_reputation threshold remain in eligible pool."""
    _NODE_HIGH_REP = {"node_id": "n-hi", "allow_remote_inference": True, "reputation_score": 0.9}
    _NODE_LOW_REP = {"node_id": "n-lo", "allow_remote_inference": True, "reputation_score": 0.2}
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST, min_reputation=0.6)
    # Only n-hi meets threshold → dispatch proceeds (REMOTE envelope)
    envelope = compute_cip_envelope([_NODE_LOW_REP, _NODE_HIGH_REP], {}, cfg, "t-rep3", None)
    assert envelope is not None


# ── session_credit_budget enforcement (CIP-CALL-03) ──────────────────────────

def test_session_tracker_none_allows_dispatch():
    """No tracker (unlimited session) does not gate dispatch."""
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST)
    envelope = compute_cip_envelope([_NODE_CAPABLE], {}, cfg, "t-sbt0", None, session_tracker=None)
    assert envelope is not None


def test_session_tracker_with_budget_allows_first_dispatch():
    """Fresh tracker with remaining budget does not block dispatch."""
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST)
    tracker = SessionBudgetTracker(session_credit_budget=10.0)
    envelope = compute_cip_envelope([_NODE_CAPABLE], {}, cfg, "t-sbt1", None, session_tracker=tracker)
    assert envelope is not None


def test_session_tracker_exhausted_budget_blocks_dispatch():
    """§2.2: exhausted session budget routes task locally (returns None envelope)."""
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST)
    tracker = SessionBudgetTracker(session_credit_budget=0.5)
    # Spend budget before dispatch (estimated_credits=1.0 > remaining=0.5)
    tracker.record_spend(0.5)
    result = compute_cip_envelope([_NODE_CAPABLE], {}, cfg, "t-sbt2", None, session_tracker=tracker)
    assert result is None


def test_session_tracker_unlimited_budget_always_allows():
    """SessionBudgetTracker(session_credit_budget=None) is unlimited — never blocks."""
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST)
    tracker = SessionBudgetTracker(session_credit_budget=None)
    tracker.record_spend(9999.0)  # simulate huge accumulated spend
    envelope = compute_cip_envelope([_NODE_CAPABLE], {}, cfg, "t-sbt3", None, session_tracker=tracker)
    assert envelope is not None


def test_session_tracker_auto_records_spend_on_remote_dispatch():
    """§2.2: budget auto-decrements after each REMOTE dispatch (CIP-CALL-03 spend loop)."""
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST)
    # Budget just enough for one dispatch (estimated_credits=1.0 in decide_dispatch)
    tracker = SessionBudgetTracker(session_credit_budget=1.5)
    # First dispatch consumes 1.0 credits → 0.5 remaining
    envelope1 = compute_cip_envelope([_NODE_CAPABLE], {}, cfg, "t-sbt4a", None, session_tracker=tracker)
    assert envelope1 is not None
    # Second dispatch needs 1.0 but only 0.5 remains → routed locally → None
    envelope2 = compute_cip_envelope([_NODE_CAPABLE], {}, cfg, "t-sbt4b", None, session_tracker=tracker)
    assert envelope2 is None


# ── WQ-059 / B-A §10.1 — consumer affordability surfaced through the envelope ──
# These cover the RUNTIME wiring: compute_cip_envelope must raise IICP-E036 when a
# remote-first request can't be afforded (so the adapter returns HTTP 402) instead
# of silently collapsing to a local run, and must stay graceful under local-first.


def test_insufficient_balance_remote_first_raises_iicp_e036():
    """B-A: remote-first + balance < routing cost (1.0) → raise CIPInsufficientCredits.

    Fails without the fix — pre-WQ-059 the ERROR collapsed to a None envelope and
    the request ran locally with no signal to the consumer.
    """
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST)
    with pytest.raises(CIPInsufficientCredits) as exc:
        compute_cip_envelope([_NODE_CAPABLE], {}, cfg, "t-e036", None, consumer_balance=0.25)
    assert exc.value.error_code == "IICP-E036"


def test_insufficient_balance_local_first_falls_back_to_local_no_raise():
    """B-A two-step: under local-first a low balance degrades to local (None), never E036."""
    cfg = _cfg(enabled=True, strategy=CIPStrategy.LOCAL_FIRST)
    result = compute_cip_envelope([_NODE_CAPABLE], {}, cfg, "t-lf", None, consumer_balance=0.25)
    assert result is None


def test_sufficient_balance_remote_first_returns_envelope():
    """balance >= cost → affordability gate is a no-op, REMOTE envelope is returned."""
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST)
    envelope = compute_cip_envelope([_NODE_CAPABLE], {}, cfg, "t-ok", None, consumer_balance=10.0)
    assert envelope is not None


def test_unknown_balance_skips_gate_preserves_back_compat():
    """consumer_balance=None (not fetched) → gate skipped → prior behaviour (envelope)."""
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST)
    envelope = compute_cip_envelope([_NODE_CAPABLE], {}, cfg, "t-none", None, consumer_balance=None)
    assert envelope is not None


# ── resolve_consumer_balance — confines the directory round-trip ──────────────

class _FakeDirectory:
    def __init__(self, balance: float | None = 7.5) -> None:
        self._balance = balance
        self.calls: list[str] = []

    async def credit_balance(self, node_token: str) -> float | None:
        self.calls.append(node_token)
        return self._balance


@pytest.mark.asyncio
async def test_resolve_balance_none_when_cip_disabled():
    cfg = _cfg(enabled=False)
    d = _FakeDirectory()
    assert await resolve_consumer_balance(d, "tok", cfg) is None
    assert d.calls == []  # no fetch when CIP is off


@pytest.mark.asyncio
async def test_resolve_balance_none_when_no_token():
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST)
    d = _FakeDirectory()
    assert await resolve_consumer_balance(d, None, cfg) is None
    assert d.calls == []


@pytest.mark.asyncio
async def test_resolve_balance_fetches_when_enabled_and_token():
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST)
    d = _FakeDirectory(balance=3.0)
    assert await resolve_consumer_balance(d, "tok", cfg) == 3.0
    assert d.calls == ["tok"]


@pytest.mark.asyncio
async def test_resolve_balance_none_when_directory_lacks_method():
    cfg = _cfg(enabled=True, strategy=CIPStrategy.REMOTE_FIRST)
    assert await resolve_consumer_balance(object(), "tok", cfg) is None
