"""Phase 5A CIP Consumer Mode unit tests.

Covers: safe defaults, clamp logic, is_remote_allowed gate, configure override.
All tests use pure unit assertions — no network, no fixtures required.
"""
from __future__ import annotations

from iicp_client.proxy.cip.consumer import (
    CIPConsumerConfig,
    configure_consumer,
    get_consumer_config,
)


def test_consumer_defaults_are_disabled():
    """CIP-S1: consumer mode is off by default — no implicit remote inference."""
    cfg = CIPConsumerConfig()
    assert cfg.enabled is False
    assert cfg.policy == "local_only"
    assert cfg.fallback_to_local is True


def test_consumer_defaults_deny_remote():
    """is_remote_allowed() returns False with all safe defaults."""
    cfg = CIPConsumerConfig()
    assert cfg.is_remote_allowed() is False


def test_consumer_enabled_with_local_only_policy_still_denies_remote():
    """Enabling consumer with local_only policy must NOT allow remote routing."""
    cfg = CIPConsumerConfig(enabled=True, policy="local_only")
    assert cfg.is_remote_allowed() is False


def test_consumer_enabled_with_remote_policy_allows_remote():
    """enabled=True + policy != local_only → is_remote_allowed() is True."""
    cfg = CIPConsumerConfig(enabled=True, policy="prefer_remote")
    assert cfg.is_remote_allowed() is True


def test_consumer_replicas_clamped_to_minimum_one():
    """replicas < 1 is clamped to 1 — at least one result required."""
    cfg = CIPConsumerConfig(replicas=0)
    assert cfg.replicas == 1


def test_consumer_replicas_clamped_to_maximum_ten():
    """replicas > 10 is clamped to 10 — safety boundary on fan-out."""
    cfg = CIPConsumerConfig(replicas=999)
    assert cfg.replicas == 10


def test_consumer_coordinator_timeout_clamped_to_max():
    """coordinator_timeout_ms > 60_000 is clamped to 60_000."""
    cfg = CIPConsumerConfig(coordinator_timeout_ms=999_999)
    assert cfg.coordinator_timeout_ms == 60_000


def test_consumer_coordinator_timeout_minimum_is_one():
    """coordinator_timeout_ms <= 0 is clamped to 1."""
    cfg = CIPConsumerConfig(coordinator_timeout_ms=0)
    assert cfg.coordinator_timeout_ms == 1


def test_configure_consumer_overwrites_singleton():
    """configure_consumer() updates the module-level singleton for the session."""
    configure_consumer(enabled=True, policy="prefer_remote", replicas=3)
    cfg = get_consumer_config()
    assert cfg.enabled is True
    assert cfg.policy == "prefer_remote"
    assert cfg.replicas == 3
    # Reset to safe defaults after test
    configure_consumer()
