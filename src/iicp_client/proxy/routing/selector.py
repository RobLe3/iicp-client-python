# SPDX-License-Identifier: Apache-2.0
"""Node selector — preserve directory score order, drop only unavailable nodes.

The selector is intentionally the dumbest link in the routing chain: it does
NOT re-rank, does NOT introduce client-side scoring, and does NOT filter on
circuit state (FallbackChain handles that). Three filter axes:
1. availability flag — MUST be true (ADR-008)
2. min_reputation threshold — operator-configured CIP gate (issue #74, §2.2)
3. load < 0.8 — MUST at selection time per S.12 §5.1 (CIP-CALL-05); the directory
   scoring formula (ADR-008) penalises high-load nodes but does not hard-drop them,
   so this client-side guard enforces the spec MUST.

Cross-references:
    - ADR-008 — "Score computed server-side by directory; clients cannot influence scores."
    - ADR-012 — Phase 5 CIP weights are evaluated server-side (per-request when ?model=)
    - spec/iicp-dir.md §discover — response is pre-sorted DESC by score
    - spec/iicp-cooperative-inference.md §2.2 — min_reputation gate for CIP workers (issue #74)
    - spec/iicp-cooperative-inference.md §5.1 — load < 0.8 MUST at selection time (CIP-CALL-05)
    - issue #79 CIP-P2 — model-compatibility partition

Keeping the proxy out of the ranking business preserves a single source of
truth (the directory) and prevents client/server score drift.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ADR-008: "Score computed server-side by directory. Clients cannot influence scores."
# The proxy preserves the directory score order and only removes nodes whose available
# flag is false, load >= 0.8, or reputation below threshold. Circuit breaker filtering
# is handled by FallbackChain + TaskRouter.

_DEFAULT_REPUTATION = 0.0  # no filter by default; operators set min_reputation in CIP config
_MAX_WORKER_LOAD = 0.8      # S.12 §5.1 CIP-CALL-05: MUST drop workers at or above this load


def _node_models(node: dict[str, Any]) -> set[str]:
    """Extract the flat set of model identifiers advertised by a node."""
    return set(node.get("models", []))


def _model_matches(node: dict[str, Any], requested: list[str]) -> bool:
    """Return True when the node advertises at least one of the requested models."""
    if not requested:
        return True
    return bool(_node_models(node) & set(requested))


class NodeSelector:
    """Pass-through ranker — see module docstring for the no-client-scoring rule.

    Two filter axes (intersection): availability flag (must be true), and the
    operator-configured min_reputation threshold (CIP gate from issue #74).
    """

    def __init__(
        self,
        preferred_region: str | None = None,
        min_reputation: float = _DEFAULT_REPUTATION,
    ) -> None:
        # preferred_region retained for future use (e.g., logging); not used for scoring
        self._region = preferred_region
        # §2.2 / issue #74: minimum reputation threshold for CIP-eligible workers.
        # Nodes below this threshold are dropped before CIP dispatch even if available.
        self._min_reputation = min_reputation
        # S.12 §5.1 CIP-CALL-05: hard load ceiling; nodes at or above _MAX_WORKER_LOAD
        # must not be selected even if the directory returned them as available.
        self._max_load = _MAX_WORKER_LOAD

    def rank(
        self,
        nodes: list[dict[str, Any]],
        requested_models: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return nodes in directory score order, excluding unavailable, overloaded, or low-reputation nodes.

        The directory response is already sorted descending by score — preserve that order.
        Filter axes applied in order:
        1. available=False → drop (ADR-008)
        2. load >= 0.8 → drop (S.12 §5.1 CIP-CALL-05 MUST)
        3. reputation_score < min_reputation → drop (§2.2 Auth/Reputation gate, issue #74)

        Model compatibility (issue #79 CIP-P2): when requested_models is set, nodes that
        advertise a matching model are returned first. If no model-matching nodes exist,
        all available nodes are returned as fallback (any-model fallback per §2.2).

        Circuit-open nodes are not filtered here; TaskRouter raises CircuitOpenError and
        FallbackChain moves to the next candidate.
        """
        available = []
        for n in nodes:
            if not n.get("available", False):
                continue
            node_load = n.get("load", 0.0)
            if node_load >= self._max_load:
                logger.debug(
                    "NodeSelector: dropping node %s — load %.3f >= max %.3f (S.12 §5.1)",
                    n.get("node_id", "?"),
                    node_load,
                    self._max_load,
                )
                continue
            if n.get("reputation_score", 1.0) < self._min_reputation:
                logger.debug(
                    "NodeSelector: dropping node %s — reputation_score %.3f < min %.3f",
                    n.get("node_id", "?"),
                    n.get("reputation_score", 0.0),
                    self._min_reputation,
                )
                continue
            available.append(n)

        if not requested_models:
            return available

        # Partition: model-matching nodes first, then any-model fallback
        matching = [n for n in available if _model_matches(n, requested_models)]
        if matching:
            return matching
        # Fallback: no model-match nodes → return all available (any-model)
        logger.debug(
            "NodeSelector: no model-matching nodes for %s — falling back to any-model pool",
            requested_models,
        )
        return available

    def select(
        self,
        nodes: list[dict[str, Any]],
        requested_models: list[str] | None = None,
        max_multiplier: float | None = None,
        min_quality_score: float | None = None,
        max_credits: float | None = None,
        expected_tokens: int = 1000,
    ) -> list[dict[str, Any]]:
        """Apply ADR-019 pricing and quality filters on top of rank().

        Filter order:
        1. rank() — availability, min_reputation, model compatibility
        2. min_quality_score — drop nodes below ADR-008 score threshold
        3. max_multiplier — drop nodes whose pricing multiplier exceeds limit
        4. max_credits — estimate cost per node; if all exceed budget, fall back
           to the lowest-multiplier node (never returns an error if ≥1 quality-
           eligible node remains after steps 2–3).

        The deprecated billing.priority="premium" field is accepted for backward
        compatibility but not used for selection (ADR-019 §3).
        """
        candidates = self.rank(nodes, requested_models)

        if min_quality_score is not None:
            candidates = [n for n in candidates if n.get("score", 0.0) >= min_quality_score]

        if max_multiplier is not None:
            candidates = [
                n for n in candidates
                if n.get("credit_cost_multiplier", 1.0) <= max_multiplier
            ]

        if max_credits is not None and candidates:
            in_budget = []
            for n in candidates:
                multiplier = n.get("credit_cost_multiplier", 1.0)
                estimated = ((expected_tokens + 999) // 1000) * multiplier
                if estimated <= max_credits:
                    in_budget.append(n)
            if not in_budget:
                # All quality-eligible nodes exceed budget — fall back to cheapest
                candidates = [min(candidates, key=lambda n: n.get("credit_cost_multiplier", 1.0))]
                logger.debug(
                    "NodeSelector: all nodes exceed budget %.2f — falling back to lowest-multiplier node",
                    max_credits,
                )
            else:
                candidates = in_budget

        return candidates
