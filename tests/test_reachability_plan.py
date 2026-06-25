"""Reachability escalation order (tunnel-FIRST, relay = last resort; maintainer 2026-06-13).

Guards the reorder so it can't silently break — the escalation lives in the large serve flow
(the #10 lesson: untested serve paths ship bugs), so the order is extracted into the pure
plan_reachability() which _serve consumes. Parity with the TS/Rust planners.
"""
from types import SimpleNamespace

from iicp_client.cli import direct_tunnel_fallback_reason, plan_reachability


def test_tunnel_first_for_tier3_with_tunnel_enabled():
    assert plan_reachability(3, False, True) == ["tunnel", "relay", "gossip"]
    assert plan_reachability(4, False, True) == ["tunnel", "relay", "gossip"]


def test_no_tunnel_restores_relay_first():
    assert plan_reachability(3, False, False) == ["relay", "gossip"]


def test_no_escalation_when_reachable_or_relay_configured():
    assert plan_reachability(0, False, True) == []   # tier<3 → direct/UPnP path
    assert plan_reachability(2, False, True) == []
    assert plan_reachability(3, True, True) == []     # explicit relay → no auto-escalation


def test_direct_tunnel_fallback_preserves_verified_direct():
    profile = SimpleNamespace(tier=0, transport_method="direct", ipv6=None)
    assert direct_tunnel_fallback_reason("http://203.0.113.10:9484", profile) is None


def test_direct_tunnel_fallback_flags_local_and_unverified_ipv6():
    assert direct_tunnel_fallback_reason("http://localhost:9484") == "local/private endpoint"
    assert (
        direct_tunnel_fallback_reason("http://[2a0a:a543::1]:9484")
        == "IPv6 direct endpoint has no verified inbound pinhole"
    )
    profile = SimpleNamespace(
        tier=1,
        transport_method="direct",
        ipv6=SimpleNamespace(pinhole_active=True),
    )
    assert direct_tunnel_fallback_reason("http://[2a0a:a543::1]:9484", profile) is None
