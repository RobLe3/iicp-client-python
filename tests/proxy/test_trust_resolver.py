"""DIR-FED-TRUST-01 / S.13 §3.2 conformance tests for trust_resolver."""
from __future__ import annotations

from iicp_client.proxy.clients.trust_resolver import resolve_discovery_set, resolve_node_conflict

# --- §3.2 rule 1: seed beats all replicas, per-field --------------------

def test_seed_field_overrides_replica():
    seed = {"node_id": "n1", "available": False, "load": 0.1}
    replica = {"node_id": "n1", "available": True, "load": 0.9}
    merged = resolve_node_conflict(seed, [(replica, 42, "high")])
    assert merged["available"] is False, "seed.available beats replica.available"
    assert merged["load"] == 0.1, "seed.load beats replica.load"


def test_replica_fills_field_seed_omits():
    seed = {"node_id": "n1", "available": True}
    replica = {"node_id": "n1", "available": False, "credit_balance": 12.5}
    merged = resolve_node_conflict(seed, [(replica, 42, "low")])
    # Field seed has → seed wins
    assert merged["available"] is True
    # Field only replica has → replica fills in
    assert merged["credit_balance"] == 12.5


# --- §3.2 rule 2: newer seq beats older among replicas ----------------

def test_replica_higher_seq_wins():
    r_old = {"node_id": "n1", "load": 0.3, "available": True}
    r_new = {"node_id": "n1", "load": 0.7, "available": False}
    merged = resolve_node_conflict(None, [(r_old, 100, "high"), (r_new, 150, "low")])
    # Higher seq (150) wins regardless of tier
    assert merged["load"] == 0.7
    assert merged["available"] is False


def test_replica_tier_tiebreaker_on_equal_seq():
    r_low = {"node_id": "n1", "load": 0.4}
    r_high = {"node_id": "n1", "load": 0.6}
    # Equal seq → higher tier wins
    merged = resolve_node_conflict(None, [(r_low, 100, "low"), (r_high, 100, "high")])
    assert merged["load"] == 0.6, "high-tier replica wins on equal seq"


def test_replica_tier_does_not_override_higher_seq():
    r_low_high = {"node_id": "n1", "load": 0.4}  # high tier, low seq
    r_high_low = {"node_id": "n1", "load": 0.6}  # low tier, high seq
    merged = resolve_node_conflict(None, [(r_low_high, 100, "high"), (r_high_low, 200, "low")])
    # Seq is THE primary signal; tier never beats fresher seq
    assert merged["load"] == 0.6, "fresher seq beats higher tier"


# --- §3.2 rule 3: gossip is suggestion-only ----------------------------

def test_gossip_does_not_override_seed():
    seed = {"node_id": "n1", "available": True}
    gossip = {"node_id": "n1", "available": False, "load": 0.9}
    merged = resolve_node_conflict(seed, [], [gossip])
    assert merged["available"] is True, "gossip cannot override seed.available"
    # Gossip-only field can fill in
    assert merged["load"] == 0.9


def test_gossip_does_not_override_replica():
    replica = {"node_id": "n1", "available": True}
    gossip = {"node_id": "n1", "available": False}
    merged = resolve_node_conflict(None, [(replica, 100, "medium")], [gossip])
    assert merged["available"] is True, "gossip cannot override replica"


def test_gossip_only_node_included_with_gossip_fields():
    # No seed, no replica — gossip is the only source
    gossip = {"node_id": "g1", "load": 0.5, "available": True}
    merged = resolve_node_conflict(None, [], [gossip])
    assert merged == gossip


# --- §3.2 rule 4: field-level resolution, not row-level ----------------

def test_field_level_merge_combines_seed_and_replica():
    seed = {"node_id": "n1", "load": 0.1}  # seed has load, NOT credit_balance
    replica = {"node_id": "n1", "load": 0.9, "credit_balance": 12.5}  # both
    merged = resolve_node_conflict(seed, [(replica, 42, "medium")])
    assert merged["load"] == 0.1, "seed.load wins (field-level)"
    assert merged["credit_balance"] == 12.5, "replica.credit_balance fills the gap"


# --- resolve_discovery_set: union of node IDs ---------------------------

def test_discovery_set_unions_all_sources():
    seed = [{"node_id": "n1", "load": 0.1}]
    replica_snap = [{"node_id": "n1", "load": 0.5}, {"node_id": "n2", "load": 0.3}]
    gossip = [{"node_id": "n3", "load": 0.7}]
    result = resolve_discovery_set(seed, [(replica_snap, 100, "medium")], gossip)
    by_id = {n["node_id"]: n for n in result}
    assert set(by_id) == {"n1", "n2", "n3"}
    assert by_id["n1"]["load"] == 0.1, "n1 seed-wins"
    assert by_id["n2"]["load"] == 0.3, "n2 from replica (seed silent)"
    assert by_id["n3"]["load"] == 0.7, "n3 from gossip (seed + replica silent)"


def test_empty_inputs_return_empty_list():
    assert resolve_discovery_set(None, None, None) == []
    assert resolve_discovery_set([], [], []) == []


# --- Edge cases --------------------------------------------------------

def test_none_seed_with_only_replicas():
    replicas = [
        ({"node_id": "n1", "load": 0.4}, 100, "low"),
        ({"node_id": "n1", "load": 0.5}, 150, "low"),
    ]
    merged = resolve_node_conflict(None, replicas)
    assert merged["load"] == 0.5


def test_seed_only_no_replicas_or_gossip():
    seed = {"node_id": "n1", "load": 0.1}
    merged = resolve_node_conflict(seed, [], [])
    assert merged == seed


def test_multiple_replicas_pick_winning_only():
    # 3 replicas — only the one with highest seq contributes
    rs = [
        ({"node_id": "n1", "load": 0.1, "extra_field_a": 1}, 100, "high"),
        ({"node_id": "n1", "load": 0.5, "extra_field_b": 2}, 200, "low"),  # winner
        ({"node_id": "n1", "load": 0.3, "extra_field_c": 3}, 150, "high"),
    ]
    merged = resolve_node_conflict(None, rs)
    assert merged["load"] == 0.5
    assert merged.get("extra_field_b") == 2
    # Only winning replica's fields appear — losers are discarded entirely
    assert "extra_field_a" not in merged
    assert "extra_field_c" not in merged
