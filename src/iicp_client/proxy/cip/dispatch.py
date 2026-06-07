# SPDX-License-Identifier: Apache-2.0
"""Phase 5 §2.2 consumer dispatch helper — shared by all protocol compat layers.

Evaluates CIP consumer gates (enabled, realtime-qos bypass, eligible workers,
trusted-peers allowlist, sensitivity) and builds the cip_envelope dict that is
passed to FallbackChain.execute(cip_envelope=...) for remote CIP dispatch.

Isolated here so OpenAI-compat, Ollama-compat, and Anthropic-compat handlers
all call the same gate logic — a single fix propagates to every surface.

Spec: spec/iicp-cooperative-inference.md §2.2. Conformance: CIP-CALL-01 (S.12 §4.1).
ADR: ADR-012 (CIP security). See proxy/cip/coordinator.py for decide_dispatch().
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from iicp_client.proxy.cip.strategies import SessionBudgetTracker

# QoS values that route through the parallel aggregator path (OpenAI-compat).
# CIP dispatch is skipped for these — the aggregator handles execution directly.
REALTIME_QOS: frozenset[str] = frozenset({"realtime"})


class CIPInsufficientCredits(Exception):
    """Remote CIP dispatch blocked because the consumer can't afford it (B-A, §10.1).

    Raised by ``compute_cip_envelope`` only when ``decide_dispatch`` resolves to a
    structured ERROR with code ``IICP-E036`` — i.e. balance < routing cost AND no
    local fallback exists (remote-first / balanced strategy). Under local-first the
    gate degrades to a silent local run instead, so this is never raised. Compat
    adapters catch it and return HTTP 402 with the IICP error code; the dict|None
    happy path (LOCAL → None, REMOTE → envelope) is unchanged.
    """

    def __init__(self, error_code: str = "IICP-E036") -> None:
        self.error_code = error_code
        super().__init__(f"insufficient S-Credit balance for remote dispatch ({error_code})")


class CIPNoEligibleWorkers(Exception):
    """Remote CIP dispatch wanted, but no eligible workers exist (#471 / §2.2 Gate 5/6).

    Raised by ``compute_cip_envelope`` only when ``decide_dispatch`` resolves to a
    structured ERROR with code ``IICP-E022`` — i.e. remote-first / balanced strategy
    with fewer eligible CIP workers than the replica requirement. Under local-first
    the gate degrades to a silent local run instead (the "local if available" half of
    the two-step, decision 4-C), so this is never raised. Compat adapters catch it and
    return HTTP 503 with the IICP error code; the dict|None happy path is unchanged.
    """

    def __init__(self, error_code: str = "IICP-E022") -> None:
        self.error_code = error_code
        super().__init__(f"no eligible CIP workers for remote dispatch ({error_code})")


async def resolve_consumer_balance(
    directory: Any,
    node_token: str | None,
    cip_config: Any,
) -> float | None:
    """Fetch the consumer balance for the §10.1 affordability gate, or None.

    Returns None (gate skipped) when CIP is disabled, no node_token is configured,
    or the directory lacks a ``credit_balance`` method — confining the extra
    directory round-trip to CIP-enabled proxies that actually authenticate. The
    fetch itself is best-effort (DirectoryClient.credit_balance swallows failures).
    """
    if cip_config is None or not getattr(cip_config, "enabled", False):
        return None
    if not node_token:
        return None
    fetch = getattr(directory, "credit_balance", None)
    if fetch is None:
        return None
    return await fetch(node_token)


def compute_cip_envelope(
    nodes: list[dict[str, Any]],
    body: dict[str, Any],
    cip_config: Any,
    task_id: str,
    qos: str | None = None,
    session_tracker: SessionBudgetTracker | None = None,
    consumer_balance: float | None = None,
) -> dict[str, str] | None:
    """Phase 5 §2.2: evaluate CIP consumer gates and build dispatch envelope.

    Returns None when: CIP is disabled, qos is realtime (aggregator handles that
    path), no eligible workers survive the trust filter, or decide_dispatch()
    resolves to LOCAL or ERROR. CIP-CALL-01: envelope is non-None only for REMOTE
    decisions (S.12 §4.1).

    `session_tracker` enforces the §2.2 session_credit_budget ceiling (CIP-CALL-03).
    Pass the app-lifetime SessionBudgetTracker from app.state.cip_budget_tracker;
    pass None for unlimited (no budget configured).

    Caller passes the result directly to FallbackChain.execute(cip_envelope=...).
    A None result is safe — FallbackChain treats it as a plain local task.
    """
    if cip_config is None or not cip_config.enabled or qos in REALTIME_QOS:
        return None
    from iicp_client.proxy.cip.coordinator import (
        DispatchResult,
        build_cip_envelope,
        decide_dispatch,
        validate_cip_request_fields,
    )
    # S.12 §5.2: MUST validate cip fields at parse time before any dispatch (CIP-VAL-01)
    if validate_cip_request_fields(body) is not None:
        return None  # invalid cip fields — fall back to local execution

    # Guard: exclude malformed nodes (no node_id), apply min_reputation threshold
    # (§2.2: nodes below this score are excluded), and apply trusted_peers allowlist.
    # A node with allow_remote_inference=True but no node_id inflates Gate 5 count
    # without providing a usable target; trusted_peers (§2.2) restricts dispatch to
    # operator-approved workers when non-empty.
    min_rep: float = getattr(cip_config, "min_reputation", 0.0)
    eligible = [
        n["node_id"]
        for n in nodes
        if n.get("allow_remote_inference")
        and n.get("node_id")
        and n.get("reputation_score", 0.0) >= min_rep
    ]
    if cip_config.trusted_peers:
        _trusted = set(cip_config.trusted_peers)
        eligible = [nid for nid in eligible if nid in _trusted]

    # §2.2: MUST fan out to exactly cip.replicas workers; MUST NOT dispatch to
    # a reduced replica count if fewer eligible workers exist (CIP-CALL-04).
    replicas = body.get("cip", {}).get("replicas", 1) if isinstance(body.get("cip"), dict) else 1

    decision = decide_dispatch(
        task_id=task_id,
        estimated_credits=1.0,
        sensitivity=body.get("sensitivity"),
        eligible_workers=eligible,
        config=cip_config,
        session_tracker=session_tracker,
        replicas=replicas,
        consumer_balance=consumer_balance,
    )
    # B-A / §10.1: a remote-first (or balanced) request the consumer can't afford
    # resolves to a structured ERROR with IICP-E036 — surface it instead of silently
    # collapsing to a local run (which build_cip_envelope's None would do). Other
    # ERROR codes (IICP-E022/E024) keep their existing local-fallback behaviour.
    if decision.result == DispatchResult.ERROR and decision.error_code == "IICP-E036":
        raise CIPInsufficientCredits(decision.error_code)
    # #471 / 4-C: remote-first (or balanced) request with no eligible CIP workers
    # resolves to ERROR IICP-E022 — surface it (mirrors the §10.1/E036 two-step)
    # rather than silently collapsing to a local run. Local-first already returned
    # LOCAL above (the "local if available" half), so it is never raised there.
    if decision.result == DispatchResult.ERROR and decision.error_code == "IICP-E022":
        raise CIPNoEligibleWorkers(decision.error_code)
    envelope = build_cip_envelope(decision, task_id)
    # envelope is non-None iff decision was REMOTE — record spend so the session
    # ceiling decrements correctly on successive calls (CIP-CALL-03 §2.2).
    if envelope is not None and session_tracker is not None:
        session_tracker.record_spend(1.0)
    return envelope
