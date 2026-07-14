"""Public types for iicp-client (ADR-016 §1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from iicp_client.errors import IicpError


RoutingProfile = str


@dataclass
class RoutingPolicy:
    """Client-side pre-dispatch routing policy.

    The policy is evaluated after prompt-free directory discovery and before
    a task payload is sent to any remote executor.
    """

    profile: RoutingProfile = "standard"
    allowed_regions: list[str] | None = None
    require_encryption: bool | None = None
    require_policy_manifest: bool | None = None
    require_no_payload_retention: bool | None = None
    allow_remote_executor: bool | None = None
    known_operator_only: bool | None = None
    required_manifest_identity_level: str | None = None


@dataclass
class ClientConfig:
    directory_url: str = "https://iicp.network/api"
    region: str | None = None
    timeout_ms: int = 30_000
    max_retries: int = 3
    tls_verify: bool = True
    # DEPRECATED/no-op (#360): IICP-CX encryption is mandatory — the client always
    # encrypts when the node advertises a cx_public_key, regardless of this flag.
    use_confidentiality: bool = True
    routing_epsilon: float = 0.05  # ε-greedy exploration probability (R4); 0.0 disables
    routing_strategy: str = "epsilon"  # deterministic | epsilon | softmax_top_k | weighted_v1 (opt-in)
    routing_top_k: int = 3
    routing_softmax_tau: float = 0.04
    # Phase 2 (#496): caller's JWT from directory registration; used to acquire consumer tokens.
    node_token: str | None = None
    # Phase 6 (#585): default client-side policy applied before remote dispatch.
    routing_policy: RoutingPolicy = field(default_factory=RoutingPolicy)
    # auto prefers short-lived ticketed routes and falls back only when an older
    # directory explicitly lacks the endpoint. ticketed and legacy force a mode.
    route_discovery_mode: str = "auto"
    profile_request: ProfileRequest | None = None


@dataclass
class TaskConstraints:
    timeout_ms: int = 30_000
    qos: str = "interactive"
    region: str | None = None


@dataclass
class TaskAuth:
    node_token: str | None = None


@dataclass
class TaskRequest:
    intent: str
    payload: dict[str, Any]
    constraints: TaskConstraints = field(default_factory=TaskConstraints)
    auth: TaskAuth = field(default_factory=TaskAuth)
    # #488 — requester node identity for self-query neutrality at the directory.
    source_node_id: str | None = None
    # Phase 6 (#585): optional per-request override for remote routing policy.
    routing_policy: RoutingPolicy | None = None


@dataclass
class TaskMetrics:
    latency_ms: int
    tokens_used: int | None
    node_id: str


@dataclass
class TaskResponse:
    task_id: str
    status: str
    result: dict[str, Any] | None
    metrics: TaskMetrics
    error: IicpError | None = None
    generated_by_ai: bool = True
    dispatch_ticket_id_prefix: str | None = None
    routing_receipt: dict[str, Any] | None = None


@dataclass
class ChatMessage:
    role: str
    content: str


@dataclass
class ChatOptions:
    model: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    timeout_ms: int | None = None
    qos: str = "interactive"
    node_token: str | None = None
    routing_policy: RoutingPolicy | None = None


@dataclass
class ChatChoice:
    message: ChatMessage
    finish_reason: str


@dataclass
class ChatUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass
class ChatResponse:
    id: str
    choices: list[ChatChoice]
    usage: ChatUsage
    model: str
    iicp_node_id: str
    generated_by_ai: bool = True


@dataclass
class DiscoverOptions:
    region: str | None = None
    qos: str | None = None
    min_reputation: float | None = None
    model: str | None = None
    limit: int = 10
    # Browser-like consumers can request client-side filtering to endpoints a
    # normal HTTPS page may call. Native clients keep the default False so IPv6
    # HTTP/native nodes remain eligible.
    browser_usable_only: bool = False
    profile_request: ProfileRequest | None = None


@dataclass
class ProfileRequest:
    """Optional additive directory capability request for a draft profile."""

    profile_id: str
    profile_version: str
    profile_fixture_sha256: str
    required: bool = False


@dataclass
class ProfileNegotiation:
    requested: bool
    status: str | None = None
    reason: str | None = None
    dispatch_allowed: bool | None = None


@dataclass
class Node:
    node_id: str
    endpoint: str
    score: float
    available: bool
    region: str
    load: float = 0.0
    latency_estimate_ms: int | None = None
    reputation_score: float | None = None
    # ADR-044 — composed health label (healthy/degraded/impaired/critical/offline)
    # and ADR-043 8-category network exposure. Both optional: present only when
    # the directory is on v1.10.0+; None against older directories.
    health_label: str | None = None
    exposure_mode: str | None = None
    # IICP-CX S.16 §3.1 — X25519 public key for E2E payload confidentiality.
    # Present only when the node registered with cx_public_key (v1.10.7+).
    cx_public_key: dict[str, Any] | None = None
    # #397 — transport protocols the node speaks (e.g. ["https", "iicp-native"]).
    transport: list[str] | None = None
    # Additive routing-signal split from directory v1.10.50+.
    directory_observed_reachable: bool | None = None
    route_evidence: str | None = None
    routing_hint: str | None = None
    browser_usable: bool | None = None
    # Phase-1 compliance: public, self-attested node policy manifest.
    node_policy_manifest: dict[str, Any] | None = None
    dispatch_ticket_id_prefix: str | None = None


@dataclass
class NodeList:
    nodes: list[Node]
    query_ms: int
    profile_negotiation: ProfileNegotiation | None = None
