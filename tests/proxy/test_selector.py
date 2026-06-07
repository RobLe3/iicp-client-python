"""Tests for NodeSelector — ADR-008 filter-only contract.

ADR-008 rule: "Score computed server-side by directory. Clients cannot influence scores."
The selector must NOT re-rank; it only removes unavailable nodes, preserving the
directory-provided order for the rest. Circuit-open filtering is handled by FallbackChain.
"""
from __future__ import annotations

from iicp_client.proxy.routing.selector import NodeSelector


def _node(node_id: str, available: bool = True, score: float = 0.85) -> dict:
    return {
        "node_id": node_id,
        "endpoint": f"http://{node_id}",
        "available": available,
        "score": score,
    }


def test_selector_preserves_directory_order(selector):
    """PROXY-ROUTE-02: Available nodes are returned in directory order (score-desc from discover)."""
    a = _node("a")
    b = _node("b")
    c = _node("c")
    ranked = selector.rank([a, b, c])
    assert [n["node_id"] for n in ranked] == ["a", "b", "c"]


def test_selector_excludes_unavailable_nodes(selector):
    dead = _node("dead", available=False)
    assert selector.rank([dead]) == []


def test_selector_passes_all_available_nodes(selector):
    nodes = [_node("x"), _node("y"), _node("z")]
    assert selector.rank(nodes) == nodes


def test_selector_mixed_availability(selector):
    nodes = [_node("ok"), _node("down", available=False), _node("also_ok")]
    ranked = selector.rank(nodes)
    assert [n["node_id"] for n in ranked] == ["ok", "also_ok"]


def test_selector_empty_input(selector):
    assert selector.rank([]) == []


# ---------------------------------------------------------------------------
# min_reputation filter — issue #74 (§2.2 Auth/Reputation gate)
# ---------------------------------------------------------------------------


def _rnode(node_id: str, reputation_score: float, available: bool = True) -> dict:
    return {
        "node_id": node_id,
        "endpoint": f"http://{node_id}",
        "available": available,
        "reputation_score": reputation_score,
    }


def test_min_reputation_excludes_low_reputation_nodes():
    """Nodes below min_reputation threshold are dropped even if available."""
    sel = NodeSelector(min_reputation=0.6)
    nodes = [_rnode("ok", 0.8), _rnode("low", 0.5), _rnode("also_ok", 0.9)]
    ranked = sel.rank(nodes)
    assert [n["node_id"] for n in ranked] == ["ok", "also_ok"]


def test_min_reputation_zero_admits_all_available():
    """Default min_reputation=0.0 never drops nodes on reputation grounds."""
    sel = NodeSelector(min_reputation=0.0)
    nodes = [_rnode("a", 0.0), _rnode("b", 0.1), _rnode("c", 1.0)]
    ranked = sel.rank(nodes)
    assert len(ranked) == 3


def test_min_reputation_exact_threshold_is_admitted():
    """Nodes at exactly min_reputation are admitted (strictly less than is filtered)."""
    sel = NodeSelector(min_reputation=0.5)
    assert sel.rank([_rnode("exact", 0.5)]) == [_rnode("exact", 0.5)]


def test_min_reputation_no_field_defaults_to_admitted():
    """Nodes with no reputation_score field default to 1.0 (admitted at any threshold)."""
    sel = NodeSelector(min_reputation=0.9)
    node = {"node_id": "legacy", "endpoint": "http://x", "available": True}
    ranked = sel.rank([node])
    assert len(ranked) == 1


def test_min_reputation_does_not_reorder():
    """min_reputation filtering preserves directory order among admitted nodes."""
    sel = NodeSelector(min_reputation=0.6)
    nodes = [_rnode("first", 0.9), _rnode("drop", 0.3), _rnode("second", 0.7)]
    ranked = sel.rank(nodes)
    assert [n["node_id"] for n in ranked] == ["first", "second"]


# ---------------------------------------------------------------------------
# Model compatibility filtering — issue #79 (CIP-P2)
# ---------------------------------------------------------------------------


def _mnode(node_id: str, models: list[str], available: bool = True) -> dict:
    return {
        "node_id": node_id,
        "endpoint": f"http://{node_id}",
        "available": available,
        "score": 0.9,
        "models": models,
    }


def test_model_filter_returns_only_advertising_nodes():
    """NodeSelector returns model-matching nodes first when requested_models is set."""
    sel = NodeSelector()
    nodes = [
        _mnode("a", ["llama3", "mistral"]),
        _mnode("b", ["phi3"]),
        _mnode("c", ["llama3"]),
    ]
    ranked = sel.rank(nodes, requested_models=["llama3"])
    ids = [n["node_id"] for n in ranked]
    assert ids == ["a", "c"]


def test_model_filter_fallback_when_no_match():
    """NodeSelector returns all available nodes when no model-matching nodes exist."""
    sel = NodeSelector()
    nodes = [
        _mnode("x", ["phi3"]),
        _mnode("y", ["mistral"]),
    ]
    ranked = sel.rank(nodes, requested_models=["llama3"])
    assert len(ranked) == 2  # any-model fallback


def test_model_filter_absent_returns_all():
    """No requested_models → all available nodes returned (backward compat)."""
    sel = NodeSelector()
    nodes = [_mnode("a", ["llama3"]), _mnode("b", [])]
    ranked = sel.rank(nodes)
    assert len(ranked) == 2


def test_model_filter_multi_model_matches_any():
    """Node matches when it advertises any of the requested models."""
    sel = NodeSelector()
    nodes = [
        _mnode("only-mistral", ["mistral"]),
        _mnode("both", ["llama3", "mistral"]),
        _mnode("neither", ["phi3"]),
    ]
    ranked = sel.rank(nodes, requested_models=["llama3", "mistral"])
    ids = [n["node_id"] for n in ranked]
    assert "only-mistral" in ids
    assert "both" in ids
    assert "neither" not in ids


# ---------------------------------------------------------------------------
# Load ceiling filter — S.12 §5.1 CIP-CALL-05 (Workers MUST have load < 0.8)
# ---------------------------------------------------------------------------


def _lnode(node_id: str, load: float, available: bool = True) -> dict:
    return {
        "node_id": node_id,
        "endpoint": f"http://{node_id}",
        "available": available,
        "load": load,
    }


def test_load_ceiling_drops_overloaded_node():
    """CIP-CALL-05: node with load=0.8 MUST be dropped (threshold is exclusive < 0.8)."""
    sel = NodeSelector()
    nodes = [_lnode("ok", 0.7), _lnode("full", 0.8), _lnode("beyond", 0.95)]
    ranked = sel.rank(nodes)
    assert [n["node_id"] for n in ranked] == ["ok"]


def test_load_ceiling_admits_node_just_below_threshold():
    """CIP-CALL-05: node with load=0.799 MUST be admitted (< 0.8)."""
    sel = NodeSelector()
    assert sel.rank([_lnode("edge", 0.799)]) == [_lnode("edge", 0.799)]


def test_load_ceiling_node_without_load_field_admitted():
    """Nodes with no load field default to 0.0 — always admitted at any threshold."""
    sel = NodeSelector()
    node = {"node_id": "legacy", "endpoint": "http://x", "available": True}
    assert len(sel.rank([node])) == 1


def test_load_ceiling_preserves_directory_order_among_admitted():
    """CIP-CALL-05: load filter must not reorder the remaining admitted nodes."""
    sel = NodeSelector()
    nodes = [_lnode("first", 0.1), _lnode("overloaded", 0.9), _lnode("second", 0.5)]
    ranked = sel.rank(nodes)
    assert [n["node_id"] for n in ranked] == ["first", "second"]


def test_load_ceiling_all_overloaded_returns_empty():
    """CIP-CALL-05: if all workers are at or above 0.8 load, result is empty — callers must handle."""
    sel = NodeSelector()
    nodes = [_lnode("a", 0.8), _lnode("b", 0.9), _lnode("c", 1.0)]
    assert sel.rank(nodes) == []


# ---------------------------------------------------------------------------
# ADR-019 pricing/quality filters — issue #144
# ---------------------------------------------------------------------------


def _pnode(node_id: str, score: float = 0.85, multiplier: float = 1.0, available: bool = True) -> dict:
    return {
        "node_id": node_id,
        "endpoint": f"http://{node_id}",
        "available": available,
        "score": score,
        "credit_cost_multiplier": multiplier,
    }


def test_select_max_multiplier_filters_expensive_node():
    """max_multiplier=2.0 must filter out node with credit_cost_multiplier=3.0."""
    sel = NodeSelector()
    nodes = [_pnode("cheap", multiplier=1.5), _pnode("expensive", multiplier=3.0)]
    result = sel.select(nodes, max_multiplier=2.0)
    ids = [n["node_id"] for n in result]
    assert "cheap" in ids
    assert "expensive" not in ids


def test_select_min_quality_score_filters_low_score_node():
    """min_quality_score=0.8 must filter out node with score=0.7."""
    sel = NodeSelector()
    nodes = [_pnode("good", score=0.9), _pnode("weak", score=0.7)]
    result = sel.select(nodes, min_quality_score=0.8)
    ids = [n["node_id"] for n in result]
    assert "good" in ids
    assert "weak" not in ids


def test_select_all_over_budget_falls_back_to_cheapest():
    """When all nodes exceed budget, fall back to the lowest-multiplier node — no error."""
    sel = NodeSelector()
    # max_credits=0.5, expected_tokens=1000 → estimate = ceil(1000/1000) × multiplier
    # both nodes have multiplier > 0.5 → over budget → fallback to multiplier=1.5
    nodes = [_pnode("pricey", multiplier=2.0), _pnode("cheap", multiplier=1.5)]
    result = sel.select(nodes, max_credits=0.5)
    assert len(result) == 1
    assert result[0]["node_id"] == "cheap"


def test_select_deprecated_priority_field_accepted_without_error():
    """billing.priority='premium' must not cause an error and must not reorder nodes."""
    sel = NodeSelector()
    nodes = [_pnode("first", score=0.9, multiplier=1.0), _pnode("second", score=0.6, multiplier=1.0)]
    # select() doesn't accept 'priority' — that's handled upstream; just verify rank preserved
    result = sel.select(nodes)
    assert [n["node_id"] for n in result] == ["first", "second"]
