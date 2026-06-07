# SPDX-License-Identifier: Apache-2.0
"""CIP-V06: CIPAggregationResult — coordinator RESPONSE aggregation object (S.12 §4.3).

When a proxy acts as CIP coordinator, every RESPONSE that involved dispatching at
least one worker MUST include a `cip_aggregation` object in the trace (§4.3 MUST).
This model is the canonical Python representation of that object.

Spec normative requirements (S.12 §4.3):
  - policy, replicas_dispatched, replicas_responded, selected_worker_id are MUST fields
  - aggregation_latency_ms is SHOULD
  - cip_vote_count and cip_quorum_threshold are MUST for majority_vote policy
  - selected_worker_id MUST be null when replicas_responded == 0
  - replicas_responded MUST NOT exceed replicas_dispatched
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

_VALID_CIP_POLICIES = frozenset({"best_of_n", "majority_vote", "map_reduce"})


class CIPAggregationResult(BaseModel):
    """CIP-V06: §4.3 cip_aggregation object included in coordinator RESPONSE trace."""

    model_config = ConfigDict(extra="ignore")

    policy: str = Field(description="CIP policy used — best_of_n | majority_vote | map_reduce")
    replicas_dispatched: int = Field(ge=0, description="Number of workers dispatched")
    replicas_responded: int = Field(ge=0, description="Number of workers that responded")
    selected_worker_id: str | None = Field(
        default=None, description="Worker whose result was used; null when none responded"
    )
    aggregation_latency_ms: int | None = Field(
        default=None, ge=0, description="Elapsed ms from last response to aggregation complete"
    )
    cip_vote_count: int | None = Field(
        default=None, ge=0, description="Agreeing majority count (majority_vote only)"
    )
    cip_quorum_threshold: int | None = Field(
        default=None, ge=1, description="Quorum required (majority_vote only)"
    )

    @model_validator(mode="before")
    @classmethod
    def validate_cross_fields(cls, values: Any) -> Any:
        """CIP-V06: enforce cross-field invariants from S.12 §4.3."""
        if isinstance(values, dict):
            dispatched = values.get("replicas_dispatched", 0)
            responded = values.get("replicas_responded", 0)
            worker_id = values.get("selected_worker_id")
            policy = values.get("policy", "")
            vote_count = values.get("cip_vote_count")
            quorum_threshold = values.get("cip_quorum_threshold")

            if responded > dispatched:
                raise ValueError(
                    f"replicas_responded ({responded}) must not exceed "
                    f"replicas_dispatched ({dispatched})"
                )
            if responded == 0 and worker_id is not None:
                raise ValueError(
                    "selected_worker_id must be null when replicas_responded == 0 (S.12 §4.3)"
                )
            # S.12 §4.3: cip_vote_count and cip_quorum_threshold MUST be non-null
            # for majority_vote policy — null values indicate incomplete aggregation
            if policy == "majority_vote":
                if vote_count is None:
                    raise ValueError(
                        "cip_vote_count must be non-null for majority_vote policy (S.12 §4.3)"
                    )
                if quorum_threshold is None:
                    raise ValueError(
                        "cip_quorum_threshold must be non-null for majority_vote policy (S.12 §4.3)"
                    )
        return values
