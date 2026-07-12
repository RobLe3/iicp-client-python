"""Opt-in, deterministic-testable `iicp.selection.v1` candidate ordering."""
from __future__ import annotations
from typing import TypeVar

T = TypeVar("T")

def weighted_v1_order(nodes: list[T], max_retries: int, random_value: float, *, score=lambda n: n.score, load=lambda n: getattr(n, "load", 0.0), node_id=lambda n: n.node_id) -> list[T]:
    if len(nodes) <= 1:
        return nodes[:max_retries]
    pool = nodes[: max(1, min(len(nodes), 3))]
    weights = [max(float(score(node)), 0.01) / (1.0 + max(0.0, min(float(load(node)), 1.0))) for node in pool]
    remaining = max(0.0, min(float(random_value), 0.999999999)) * sum(weights)
    chosen = pool[-1]
    for node, weight in zip(pool, weights):
        remaining -= weight
        if remaining <= 0:
            chosen = node
            break
    return [chosen, *[node for node in nodes[:max_retries] if node_id(node) != node_id(chosen)]][:max_retries]
