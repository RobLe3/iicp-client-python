# SPDX-License-Identifier: Apache-2.0
"""CIP dispatch gates + request validation — extracted from coordinator.py (#333 Priority #2b).

Pure functions (no I/O, no state) that:
  - Validate incoming CIP request fields (CIP-VAL-01)
  - Evaluate the S.12 §2.2 normative MUST gates (enabled / credit / sensitivity / workers / replicas)
  - Compute derived values: worker timeout, exhaustion result, envelope
  - Build the cip envelope handed to NodeClient.submit_task()

All decision logic; no network calls. Side-effect of `decide_dispatch` is generating
a cip_session_key for REMOTE outcomes (§10.4) — that's the only randomness.

Originally lived inside `coordinator.py`. Extracted so the coordinator file's
remaining concern is "submit_award ledger forwarding" — small + focused.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import NamedTuple

from iicp_client.proxy.cip.receipts import make_session_key
from iicp_client.proxy.cip.strategies import LocalFirstStrategy, NodeInfo, SessionBudgetTracker
from iicp_client.proxy.otel_tracer import cip_consensus_span, cip_no_consensus_span


class CIPStrategy(StrEnum):
    LOCAL_FIRST = "local-first"
    REMOTE_FIRST = "remote-first"
    BALANCED = "balanced"


class DispatchResult(StrEnum):
    LOCAL = "local"
    REMOTE = "remote"
    ERROR = "error"


@dataclass
class CIPPrivacyConfig:
    """§2.2 [cooperative_inference.privacy] schema."""

    send_sensitive_prompts: bool = False  # MUST default False (§2.2)


@dataclass
class CIPDispatchConfig:
    """Canonical §2.2 proxy.toml [cooperative_inference] schema.

    All fields default to the safest possible state (off / local-only).
    Operators must explicitly set enabled=True to allow remote dispatch.
    """

    enabled: bool = False                              # MUST default False
    strategy: CIPStrategy = CIPStrategy.LOCAL_FIRST   # local-first | remote-first | balanced
    max_credits_per_task: float = 10.0                 # MUST be > 0 (validated below)
    session_credit_budget: float | None = None         # None = unlimited session ceiling
    trusted_peers: list[str] = field(default_factory=list)  # empty = open
    min_reputation: float = 0.0                        # §2.2: nodes below this score excluded
    privacy: CIPPrivacyConfig = field(default_factory=CIPPrivacyConfig)
    coordinator_timeout_ms: int = 30_000               # §6: total task budget (ms); worker_timeout = 60%

    def __post_init__(self) -> None:
        if self.max_credits_per_task <= 0:
            raise ValueError(
                "max_credits_per_task MUST be > 0; reject at startup otherwise (S.12 §2.2)"
            )
        if isinstance(self.privacy, dict):
            self.privacy = CIPPrivacyConfig(**self.privacy)


class DispatchDecision(NamedTuple):
    result: DispatchResult
    error_code: str | None = None      # e.g. "IICP-E022" when no path exists
    cip_session_key: str | None = None  # present only when result == REMOTE


def compute_worker_timeout_s(coordinator_timeout_ms: int) -> float:
    """§6: derive worker_timeout from coordinator_timeout_ms using the spec formula.

    worker_timeout = coordinator_timeout × 0.6 — leaves 30% for aggregation and
    10% slack. Returns seconds (float) for use with asyncio.wait_for().
    """
    return max(0.0, coordinator_timeout_ms * 0.6 / 1000.0)


def cip_exhaustion_result(*, fallback_to_local: bool) -> DispatchDecision:
    """§3.1: outcome when zero workers respond within worker_timeout (IICP-E024).

    If a local model is available (`fallback_to_local=True`), the Coordinator
    MUST fall back to local execution. Otherwise MUST return IICP-E024
    (all workers timed out).
    """
    if fallback_to_local:
        return DispatchDecision(result=DispatchResult.LOCAL)
    return DispatchDecision(result=DispatchResult.ERROR, error_code="IICP-E024")


# CIP-VAL-01 valid policy values (S.12 §5.2 ¶1)
_VALID_CIP_POLICIES: frozenset[str] = frozenset({"best_of_n", "majority_vote", "map_reduce"})


def validate_cip_request_fields(body: dict) -> str | None:
    """Parse-time validation of cip.policy, cip.replicas, cip.quorum (S.12 §5.2).

    MUST be called before any worker selection or dispatch (S.12 §5.2 ¶4).
    Returns None on valid input or when no cip block is present.
    Returns an IICP error code on the first violation.
    """
    cip = body.get("cip")
    if not isinstance(cip, dict):
        return None

    policy = cip.get("policy")
    if policy is not None and policy not in _VALID_CIP_POLICIES:
        return "IICP-E028"

    replicas = cip.get("replicas")
    if replicas is not None:
        if not isinstance(replicas, int) or replicas < 1 or replicas > 10:
            return "IICP-E028"
        if policy == "majority_vote" and replicas % 2 == 0:
            return "IICP-E025"

    quorum = cip.get("quorum")
    if quorum is not None:
        if not isinstance(quorum, int) or quorum < 1:
            return "IICP-E028"
        effective_replicas = replicas if isinstance(replicas, int) else 1
        if quorum > effective_replicas:
            return "IICP-E028"

    return None


def _affordability_gate(
    estimated_credits: float,
    config: CIPDispatchConfig,
    session_tracker: SessionBudgetTracker | None,
    consumer_balance: float | None,
) -> DispatchDecision | None:
    """Gates 2a–2c (§2.2 / §10.1) — can the consumer afford this remote dispatch?

    Returns a blocking `DispatchDecision`, or `None` to proceed:
      - estimated_credits > per-task ceiling → LOCAL (fall back, don't overspend)
      - session budget exhausted → LOCAL
      - directory balance < routing cost (B-A) → local-first fallback, else IICP-E036
        (`consumer_balance=None` = balance unknown → skip the check)
    """
    if estimated_credits > config.max_credits_per_task:
        return DispatchDecision(result=DispatchResult.LOCAL)
    if session_tracker is not None and not session_tracker.can_spend(estimated_credits):
        return DispatchDecision(result=DispatchResult.LOCAL)
    if consumer_balance is not None and estimated_credits > consumer_balance:
        return _blocked_remote(config, "IICP-E036")
    return None


def _blocked_remote(config: CIPDispatchConfig, error_code: str) -> DispatchDecision:
    """A gate has blocked remote dispatch: under local-first run locally (graceful), else
    surface a structured ERROR. Consolidates the repeated local-first-else-error branch so
    the gate chain in decide_dispatch stays flat."""
    if config.strategy == CIPStrategy.LOCAL_FIRST:
        return DispatchDecision(result=DispatchResult.LOCAL)
    return DispatchDecision(result=DispatchResult.ERROR, error_code=error_code)


def decide_dispatch(
    *,
    task_id: str,
    estimated_credits: float,
    sensitivity: str | None,
    eligible_workers: list[str],
    config: CIPDispatchConfig,
    node_list: list[NodeInfo] | None = None,
    intent: str | None = None,
    session_tracker: SessionBudgetTracker | None = None,
    replicas: int = 1,
    consumer_balance: float | None = None,
) -> DispatchDecision:
    """Evaluate §2.2 normative gates and return a dispatch decision.

    Returns:
      LOCAL  — execute locally (gate blocked remote, or local-first fallback)
      REMOTE — may fan out; cip_session_key is set for §10.4 session binding
      ERROR  — structured error (IICP-E022) when no execution path exists

    Gate order follows the spec: enabled → credit → sensitivity → local-first
    → eligible workers → replica count.
    """
    # Gate 1 — §2.2: MUST NOT dispatch without cooperative_inference.enabled = true
    if not config.enabled:
        return DispatchDecision(result=DispatchResult.LOCAL)

    # Gates 2a–2c — affordability (per-task ceiling, session budget, consumer balance §10.1)
    blocked = _affordability_gate(estimated_credits, config, session_tracker, consumer_balance)
    if blocked is not None:
        return blocked

    # Gate 3 — sensitivity opt-in
    if sensitivity == "high" and not config.privacy.send_sensitive_prompts:
        return DispatchDecision(result=DispatchResult.LOCAL)

    # Gate 4 — LocalFirstStrategy: prefer local provider when available (§2.2)
    if config.strategy == CIPStrategy.LOCAL_FIRST and node_list is not None:
        strategy = LocalFirstStrategy()
        if not strategy.should_dispatch_remote(node_list, intent):
            return DispatchDecision(result=DispatchResult.LOCAL)

    # Gate 5/6 — eligible worker count must satisfy the replica requirement
    n_eligible = len(eligible_workers)
    if n_eligible < max(replicas, 1):
        if config.strategy == CIPStrategy.LOCAL_FIRST:
            return DispatchDecision(result=DispatchResult.LOCAL)
        with cip_no_consensus_span(  # TRACE-10
            task_id=task_id,
            reason="IICP-E022",
            eligible_workers=n_eligible,
        ):
            pass
        return DispatchDecision(result=DispatchResult.ERROR, error_code="IICP-E022")

    # All gates passed — generate §10.4 session key and allow remote dispatch
    session_key = make_session_key(task_id)
    with cip_consensus_span(  # TRACE-09
        task_id=task_id,
        policy=config.strategy.value,
        replicas=1,
        quorum_met=True,
    ):
        pass
    return DispatchDecision(result=DispatchResult.REMOTE, cip_session_key=session_key)


def build_cip_envelope(
    decision: DispatchDecision,
    parent_task_id: str,
) -> dict[str, str] | None:
    """Build the cip object for a CALL body from a REMOTE DispatchDecision (CIP-CALL-01).

    Returns None when the decision is not REMOTE so callers can pass the result
    directly to NodeClient.submit_task(cip_envelope=...) without an extra branch.
    """
    if decision.result != DispatchResult.REMOTE or decision.cip_session_key is None:
        return None
    return {
        "cip_role": "worker",
        "cip_session_key": decision.cip_session_key,
        "cip_parent_task_id": parent_task_id,
    }
