# SPDX-License-Identifier: Apache-2.0
"""Proxy configuration — IICP_PROXY_* environment variables + proxy.toml.

Configuration loading order (later overrides earlier):
  1. proxy.toml — operator-written TOML (optional; path defaults to ./proxy.toml)
  2. IICP_PROXY_* environment variables — override individual fields for CI/containers

Key sections and their roles:
  [directory]              — directory_url, directory_timeout_ms (where to discover nodes)
  [routing]                — preferred_region, max_retries, retry_base_ms,
                             circuit_breaker_threshold/reset_s (ADR-003 retry/CB policy)
  [server]                 — host:port the proxy listens on (127.0.0.1:9483 by default —
                             the reserved IICP proxy band 9480-9483, below the 9484 node port;
                             override with IICP_PROXY_PORT, e.g. =11434 for literal Ollama drop-in)
  [cooperative_inference]  — full §2.2 consumer gate config: enabled, strategy,
                             max_credits_per_task, session_credit_budget,
                             send_sensitive_prompts, trusted_peers, min_reputation
  [plugin.model_provider]  — enabled, listen_port, model_name (CIP-PL1 OpenAI adapter)

WHY pydantic-settings (not plain tomllib): env-var override support is built-in; field
validation catches operator mistakes (e.g., max_retries as a string) at startup.

WHY from_toml() flattens nested sections: pydantic-settings env_nested_delimiter
requires double-underscores in env vars (e.g. IICP_PROXY_ROUTING__MAX_RETRIES).
TOML allows natural nesting ([routing] max_retries = 3) — from_toml() bridges the two
by extracting nested sections into flat top-level keys before handing off to __init__.

Spec: spec/iicp-core.md §proxy-config. ADR: ADR-003 (retry), ADR-005 (separation).
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    # Type-only import — the real import stays function-local in
    # to_cip_dispatch_config() to avoid coupling config ↔ coordinator at module init.
    from iicp_client.proxy.cip.coordinator import CIPDispatchConfig


class ProxyConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IICP_PROXY_", extra="ignore")

    # Directory
    directory_url: str = "https://iicp.network/api"
    directory_timeout_ms: int = Field(default=5000, ge=1)

    # Routing
    preferred_region: str | None = None
    max_retries: int = Field(default=3, ge=0, le=10)
    retry_base_ms: int = Field(default=200, ge=1)
    circuit_breaker_threshold: int = Field(default=5, ge=1)
    circuit_breaker_reset_s: int = Field(default=30, ge=5)

    # Server — listens in the reserved IICP proxy band 9480-9483 (below the 9484
    # node port, which auto-increments UPWARD for extra nodes). Override via
    # IICP_PROXY_PORT (e.g. 11434) for literal Ollama drop-in. See iicp-framing §11.1.
    host: str = "127.0.0.1"
    port: int = Field(default=9483, ge=1, le=65535)

    # Auth
    node_token_env: str = "IICP_NODE_TOKEN"

    # Phase 2 — peer cache
    peer_cache_ttl_s: float = Field(default=30.0, ge=5.0)

    # Phase 3 — redundancy
    redundancy_fan_out: int = Field(default=3, ge=2, le=10)

    # Phase 5 — CIP consumer mode (§2.2 full [cooperative_inference] config — CIP-CFG-01)
    # min_reputation: nodes below this score are excluded from CIP discover results.
    cip_min_reputation: float = Field(default=0.0, ge=0.0, le=1.0)
    # enabled MUST default false (§2.2 ¶1) — no remote dispatch without explicit opt-in.
    cip_enabled: bool = False
    cip_strategy: str = "local-first"           # local-first | remote-first | balanced
    cip_max_credits_per_task: float = Field(default=10.0, gt=0.0)
    cip_session_credit_budget: float | None = None  # None = unlimited session ceiling
    cip_send_sensitive_prompts: bool = False     # §10.2: MUST default false
    cip_trusted_peers: list[str] = Field(default_factory=list)

    # CIP-PL1 — model-provider plugin (default: disabled). Listens in the reserved
    # IICP proxy band (9482), distinct from the main proxy (9483) and the Ollama
    # backend (11434) so all three coexist on one host.
    plugin_enabled: bool = False
    plugin_listen_port: int = Field(default=9482, ge=1, le=65535)
    plugin_model_name: str = "iicp"

    @classmethod
    def from_toml(cls, path: str = "proxy.toml") -> ProxyConfig:
        p = Path(path)
        data: dict[str, Any] = {}
        if p.exists():
            with p.open("rb") as f:
                raw = tomllib.load(f)
            # Flatten [cooperative_inference] into top-level fields (CIP-CFG-01: all §2.2 keys)
            cip = raw.pop("cooperative_inference", {})
            for key, dest in (
                ("min_reputation", "cip_min_reputation"),
                ("enabled", "cip_enabled"),
                ("strategy", "cip_strategy"),
                ("max_credits_per_task", "cip_max_credits_per_task"),
                ("session_credit_budget", "cip_session_credit_budget"),
                ("send_sensitive_prompts", "cip_send_sensitive_prompts"),
                ("trusted_peers", "cip_trusted_peers"),
            ):
                if key in cip:
                    raw[dest] = cip[key]
            # Flatten [plugin.model_provider] into top-level fields
            plugin = raw.pop("plugin", {}).get("model_provider", {})
            if "enabled" in plugin:
                raw["plugin_enabled"] = plugin["enabled"]
            if "listen_port" in plugin:
                raw["plugin_listen_port"] = plugin["listen_port"]
            if "model_name" in plugin:
                raw["plugin_model_name"] = plugin["model_name"]
            data = raw
        return cls(**data)

    def to_cip_dispatch_config(self) -> CIPDispatchConfig:
        """Build a CIPDispatchConfig from loaded proxy.toml settings (CIP-CFG-01).

        Called by the proxy request handlers to configure the §2.2 consumer gate.
        Import is function-local to avoid coupling config ↔ coordinator at module init.
        """
        from iicp_client.proxy.cip.coordinator import CIPDispatchConfig, CIPPrivacyConfig, CIPStrategy

        return CIPDispatchConfig(
            enabled=self.cip_enabled,
            strategy=CIPStrategy(self.cip_strategy),
            max_credits_per_task=self.cip_max_credits_per_task,
            session_credit_budget=self.cip_session_credit_budget,
            trusted_peers=list(self.cip_trusted_peers),
            min_reputation=self.cip_min_reputation,
            privacy=CIPPrivacyConfig(send_sensitive_prompts=self.cip_send_sensitive_prompts),
        )
