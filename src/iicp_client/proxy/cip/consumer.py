# SPDX-License-Identifier: Apache-2.0
"""Phase 5A CIP Consumer Mode — safe-default stub.

The consumer allows the proxy to use remote IICP nodes as inference fallback
when no local model is available. Off by default; requires explicit operator
configuration per CIP-S1 (S.12) and ADR-012.

Safety boundary: consumer mode only receives responses — it never exposes
system prompt, local tools, or private memory to remote nodes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_VALID_CIP_POLICIES = frozenset({"best_of_n", "majority_vote", "map_reduce"})


@dataclass
class CIPConsumerConfig:
    """Configuration for CIP Phase 5A Consumer Mode.

    All fields default to the safest possible state (off / local-only).
    Operators must explicitly opt in by setting ``enabled=True`` in
    ``proxy.toml`` under ``[cooperative_inference]``.
    """

    enabled: bool = False
    policy: str = "local_only"
    replicas: int = 1
    fallback_to_local: bool = True
    coordinator_timeout_ms: int = 30_000

    def __post_init__(self) -> None:
        # Clamp replicas to [1, 10] — safety boundary on fan-out
        self.replicas = max(1, min(10, self.replicas))
        # Clamp timeout to [1, 60_000] ms
        self.coordinator_timeout_ms = max(1, min(60_000, self.coordinator_timeout_ms))

    def is_remote_allowed(self) -> bool:
        """Return True only when consumer mode is on and policy permits remote."""
        return self.enabled and self.policy != "local_only"


class CIPCallFields(BaseModel):
    """CIP-V01/CIP-V02: wire-boundary validation for the `cip` object in a CIP CALL.

    A Coordinator MUST validate these fields before dispatching (S.12 §4.1).
    Invalid values → 422 IICP-E028 at parse time, except majority_vote even
    replicas which → IICP-E025 (S.12 §3.2).
    """

    model_config = ConfigDict(extra="ignore")

    policy: str = Field(description="CIP aggregation policy — best_of_n | majority_vote | map_reduce")
    replicas: int = Field(ge=1, le=10, description="Number of worker nodes to fan out to [1, 10]")
    quorum: int | None = None

    @field_validator("policy", mode="before")
    @classmethod
    def validate_policy(cls, v: Any) -> str:
        """CIP-V01: cip.policy must be one of the three normative values (S.12 §4.1)."""
        if v not in _VALID_CIP_POLICIES:
            raise ValueError(
                f"cip.policy must be one of {sorted(_VALID_CIP_POLICIES)}, got: {v!r}"
            )
        return str(v)

    @model_validator(mode="after")
    def validate_majority_vote_replicas(self) -> CIPCallFields:
        """CIP-V02a + CIP-V07: cross-field validation after field-level checks pass.

        CIP-V02a: majority_vote requires odd replicas ≥ 3 → IICP-E025 (S.12 §3.2).
        CIP-V07:  quorum MUST be null OR a positive integer ≤ replicas → IICP-E028 (S.12 §4.1).

        Uses mode="after" so both policy and replicas are already validated and typed.
        Fires at model construction — callers do not need to invoke this explicitly.
        """
        if self.policy == "majority_vote":
            if self.replicas < 3 or self.replicas % 2 == 0:
                raise ValueError(
                    f"IICP-E025: majority_vote requires odd replicas ≥ 3, "
                    f"got: {self.replicas} (S.12 §3.2)"
                )
        if self.quorum is not None:
            if self.quorum < 1:
                raise ValueError(
                    f"IICP-E028: cip.quorum must be a positive integer, got: {self.quorum} (S.12 §4.1)"
                )
            if self.quorum > self.replicas:
                raise ValueError(
                    f"IICP-E028: cip.quorum ({self.quorum}) must not exceed "
                    f"cip.replicas ({self.replicas}) (S.12 §4.1)"
                )
        return self


_consumer_config = CIPConsumerConfig()


def get_consumer_config() -> CIPConsumerConfig:
    return _consumer_config


def configure_consumer(**kwargs: object) -> None:
    global _consumer_config
    _consumer_config = CIPConsumerConfig(**kwargs)  # type: ignore[arg-type]
