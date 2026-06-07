"""
Federated trust conflict resolver (S.13 v0.3.1 §3.2 / DIR-FED-TRUST-01, P6-4.3).

Resolves node-state conflicts when discover responses arrive from multiple
sources (seed + replicas + gossip). Strict precedence:

  1. Seed beats all replicas (per-field, where the seed has the field).
  2. Among replicas: newer `seq` wins; trust_tier is tie-breaker on equal seq.
  3. Gossip is suggestion-only — never overrides seed or replica values.
  4. Field-level resolution, not row-level (mix-and-match per-field).

This is a pure function — no IO, no state. Caller assembles inputs from
DirectoryClient.discover() against seed + replicas (or replica /v1/snapshot
catch-up) and feeds them in. Output is a single canonical node dict per
node_id.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_TIER_RANK = {"low": 0, "medium": 1, "high": 2}


def resolve_node_conflict(
    seed_node: dict[str, Any] | None,
    replica_observations: list[tuple[dict[str, Any], int, str]] | None = None,
    gossip_observations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Merge node-state observations per S.13 §3.2 strict precedence.

    Args:
        seed_node: node dict from seed's /v1/discover (or None if seed silent).
        replica_observations: list of (node_dict, replica_seq, replica_tier)
            tuples. seq is the replica's most recent /v1/events seq; tier is
            'low'|'medium'|'high'.
        gossip_observations: list of node dicts learned via Phase 2 peer
            exchange. Suggestion-only — never overrides seed/replica.

    Returns:
        Merged node dict. Empty dict if no inputs are non-empty.

    Behavior:
    - Seed fields always win.
    - Replica fields fill in gaps the seed didn't provide. Among replicas,
      the one with highest seq contributes; tier is tie-breaker on equal seq.
    - Gossip fields fill in gaps NEITHER seed NOR any replica provided.
    """
    seed_node = seed_node or {}
    replica_observations = replica_observations or []
    gossip_observations = gossip_observations or []

    # Pick the winning replica observation: max(seq), tier as tie-breaker.
    winning_replica: dict[str, Any] = {}
    if replica_observations:
        ranked = sorted(
            replica_observations,
            key=lambda obs: (obs[1], _TIER_RANK.get(obs[2], 0)),
            reverse=True,
        )
        winning_replica = ranked[0][0] or {}

    merged: dict[str, Any] = {}
    # 3. Gossip first (lowest priority — easily overridden)
    for gossip in gossip_observations:
        for k, v in (gossip or {}).items():
            merged.setdefault(k, v)
    # 2. Replica overrides gossip
    for k, v in winning_replica.items():
        merged[k] = v
    # 1. Seed overrides everything for the fields it provides
    for k, v in seed_node.items():
        merged[k] = v
    return merged


def resolve_discovery_set(
    seed_nodes: list[dict[str, Any]] | None,
    replica_snapshots: list[tuple[list[dict[str, Any]], int, str]] | None = None,
    gossip_nodes: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Resolve a full discover response set across seed + replicas + gossip.

    Returns a deduplicated list of merged node dicts, one per unique node_id.
    A node present only in gossip is included (suggestion-only, but the
    field-level rule means a gossip-only node has gossip fields throughout —
    callers can decide whether to act on it).

    Per §3.2 the union of node_ids comes from all sources (no source can
    suppress a node the others know about); per-node merge follows
    resolve_node_conflict().
    """
    seed_nodes = seed_nodes or []
    replica_snapshots = replica_snapshots or []
    gossip_nodes = gossip_nodes or []

    by_id: dict[str, dict[str, Any]] = {}

    def _ingest(node_dict: dict[str, Any]) -> None:
        nid = node_dict.get("node_id")
        if nid:
            by_id.setdefault(nid, {})

    for n in seed_nodes:
        _ingest(n)
    for snap, _seq, _tier in replica_snapshots:
        for n in snap:
            _ingest(n)
    for n in gossip_nodes:
        _ingest(n)

    result: list[dict[str, Any]] = []
    for node_id in by_id:
        seed_for_node = next((n for n in seed_nodes if n.get("node_id") == node_id), None)
        replica_obs = []
        for snap, seq, tier in replica_snapshots:
            for n in snap:
                if n.get("node_id") == node_id:
                    replica_obs.append((n, seq, tier))
                    break
        gossip_obs = [n for n in gossip_nodes if n.get("node_id") == node_id]
        merged = resolve_node_conflict(seed_for_node, replica_obs, gossip_obs)
        if merged:
            result.append(merged)
    return result
