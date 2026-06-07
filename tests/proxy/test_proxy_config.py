# SPDX-License-Identifier: Apache-2.0
"""ProxyConfig CIP consumer config loading tests — CIP-CFG-01.

Verifies that all [cooperative_inference] keys from iicp_client.proxy.toml are loaded
into ProxyConfig fields, and that to_cip_dispatch_config() maps them to a
correctly-typed CIPDispatchConfig (S.12 §2.2).
"""
from __future__ import annotations

import tempfile

from iicp_client.proxy.config import ProxyConfig


def _write_toml(content: str) -> str:
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False)
    tmp.write(content)
    tmp.flush()
    return tmp.name


# ── default safe values ────────────────────────────────────────────────────────

def test_cip_defaults_off_without_toml():
    cfg = ProxyConfig()
    assert cfg.cip_enabled is False
    assert cfg.cip_strategy == "local-first"
    assert cfg.cip_max_credits_per_task == 10.0
    assert cfg.cip_session_credit_budget is None
    assert cfg.cip_send_sensitive_prompts is False
    assert cfg.cip_trusted_peers == []


def test_to_cip_dispatch_config_defaults_disabled():
    """§2.2 ¶1: enabled MUST default false — no accidental remote dispatch."""
    dispatch = ProxyConfig().to_cip_dispatch_config()
    assert dispatch.enabled is False


# ── per-field loading ──────────────────────────────────────────────────────────

def test_cip_enabled_from_toml():
    path = _write_toml("[cooperative_inference]\nenabled = true\n")
    assert ProxyConfig.from_toml(path).cip_enabled is True


def test_cip_strategy_from_toml():
    path = _write_toml("[cooperative_inference]\nstrategy = \"remote-first\"\n")
    assert ProxyConfig.from_toml(path).cip_strategy == "remote-first"


def test_cip_max_credits_per_task_from_toml():
    path = _write_toml("[cooperative_inference]\nmax_credits_per_task = 25.0\n")
    assert ProxyConfig.from_toml(path).cip_max_credits_per_task == 25.0


def test_cip_session_credit_budget_from_toml():
    path = _write_toml("[cooperative_inference]\nsession_credit_budget = 100.0\n")
    assert ProxyConfig.from_toml(path).cip_session_credit_budget == 100.0


def test_cip_send_sensitive_prompts_from_toml():
    path = _write_toml("[cooperative_inference]\nsend_sensitive_prompts = true\n")
    assert ProxyConfig.from_toml(path).cip_send_sensitive_prompts is True


def test_cip_trusted_peers_from_toml():
    path = _write_toml('[cooperative_inference]\ntrusted_peers = ["node-abc", "node-xyz"]\n')
    assert ProxyConfig.from_toml(path).cip_trusted_peers == ["node-abc", "node-xyz"]


def test_min_reputation_still_loaded():
    """Regression: original min_reputation key must keep loading after refactor."""
    path = _write_toml("[cooperative_inference]\nmin_reputation = 0.75\n")
    assert ProxyConfig.from_toml(path).cip_min_reputation == 0.75


# ── to_cip_dispatch_config() round-trip ───────────────────────────────────────

def test_to_cip_dispatch_config_full_round_trip():
    """CIP-CFG-01: all §2.2 config fields survive toml→ProxyConfig→CIPDispatchConfig."""
    path = _write_toml(
        "[cooperative_inference]\n"
        "enabled = true\n"
        'strategy = "balanced"\n'
        "max_credits_per_task = 20.0\n"
        "session_credit_budget = 200.0\n"
        "send_sensitive_prompts = true\n"
        'trusted_peers = ["node-1"]\n'
    )
    cfg = ProxyConfig.from_toml(path)
    from iicp_client.proxy.cip.coordinator import CIPStrategy
    dispatch = cfg.to_cip_dispatch_config()
    assert dispatch.enabled is True
    assert dispatch.strategy == CIPStrategy.BALANCED
    assert dispatch.max_credits_per_task == 20.0
    assert dispatch.session_credit_budget == 200.0
    assert dispatch.privacy.send_sensitive_prompts is True
    assert dispatch.trusted_peers == ["node-1"]


def test_to_cip_dispatch_config_local_first_strategy():
    path = _write_toml('[cooperative_inference]\nstrategy = "local-first"\n')
    from iicp_client.proxy.cip.coordinator import CIPStrategy
    assert ProxyConfig.from_toml(path).to_cip_dispatch_config().strategy == CIPStrategy.LOCAL_FIRST


def test_to_cip_dispatch_config_remote_first_strategy():
    path = _write_toml('[cooperative_inference]\nstrategy = "remote-first"\n')
    from iicp_client.proxy.cip.coordinator import CIPStrategy
    assert ProxyConfig.from_toml(path).to_cip_dispatch_config().strategy == CIPStrategy.REMOTE_FIRST


# ── #475 / WQ-060: proxy listen-port standardization (reserved IICP band) ───────
# 9483 = main proxy, 9482 = CIP-PL1 plugin — both BELOW the 9484 node port (which
# auto-increments upward), and distinct from the Ollama backend 11434. These fail
# without the port-shift change.

def test_proxy_port_default_is_9483():
    """Main proxy listens on 9483 by default (was 11434 — collided with Ollama)."""
    assert ProxyConfig().port == 9483


def test_plugin_port_default_is_9482():
    """CIP-PL1 plugin listens on 9482 by default — distinct from proxy 9483 + Ollama 11434."""
    assert ProxyConfig().plugin_listen_port == 9482


def test_proxy_port_override_via_env(monkeypatch):
    """IICP_PROXY_PORT overrides the default — the documented migration path
    (e.g. =11434 for literal Ollama drop-in) actually works (env_prefix IICP_PROXY_)."""
    monkeypatch.setenv("IICP_PROXY_PORT", "11434")
    assert ProxyConfig().port == 11434
