# SPDX-License-Identifier: Apache-2.0
"""Tests for CIPAggregationResult — CIP-V06 §4.3 coordinator RESPONSE aggregation object.

Covers normative requirements from S.12 §4.3:
  - MUST fields present and typed correctly
  - replicas_responded MUST NOT exceed replicas_dispatched
  - selected_worker_id MUST be null when replicas_responded == 0
  - majority_vote policy MUST include cip_vote_count and cip_quorum_threshold (field presence)
  - aggregation_latency_ms SHOULD be non-negative
"""
import pytest
from pydantic import ValidationError

from iicp_client.proxy.cip.aggregation import CIPAggregationResult

# ---------------------------------------------------------------------------
# CIP-V06: valid objects
# ---------------------------------------------------------------------------


class TestCIPAggregationResultValid:
    def test_best_of_n_minimal(self):
        result = CIPAggregationResult(
            policy="best_of_n",
            replicas_dispatched=3,
            replicas_responded=3,
            selected_worker_id="worker-abc",
        )
        assert result.policy == "best_of_n"
        assert result.replicas_dispatched == 3
        assert result.replicas_responded == 3
        assert result.selected_worker_id == "worker-abc"
        assert result.aggregation_latency_ms is None
        assert result.cip_vote_count is None
        assert result.cip_quorum_threshold is None

    def test_majority_vote_with_quorum_fields(self):
        result = CIPAggregationResult(
            policy="majority_vote",
            replicas_dispatched=5,
            replicas_responded=5,
            selected_worker_id="worker-xyz",
            cip_vote_count=3,
            cip_quorum_threshold=3,
        )
        assert result.policy == "majority_vote"
        assert result.cip_vote_count == 3
        assert result.cip_quorum_threshold == 3

    def test_majority_vote_missing_vote_count_rejected(self):
        """S.12 §4.3: majority_vote policy MUST have non-null cip_vote_count."""
        with pytest.raises(ValidationError, match="cip_vote_count must be non-null"):
            CIPAggregationResult(
                policy="majority_vote",
                replicas_dispatched=3,
                replicas_responded=3,
                selected_worker_id="w1",
                cip_vote_count=None,
                cip_quorum_threshold=2,
            )

    def test_majority_vote_missing_quorum_threshold_rejected(self):
        """S.12 §4.3: majority_vote policy MUST have non-null cip_quorum_threshold."""
        with pytest.raises(ValidationError, match="cip_quorum_threshold must be non-null"):
            CIPAggregationResult(
                policy="majority_vote",
                replicas_dispatched=3,
                replicas_responded=3,
                selected_worker_id="w1",
                cip_vote_count=2,
                cip_quorum_threshold=None,
            )

    def test_best_of_n_null_quorum_fields_accepted(self):
        """Non-majority_vote policies do not require cip_vote_count/cip_quorum_threshold."""
        result = CIPAggregationResult(
            policy="best_of_n",
            replicas_dispatched=3,
            replicas_responded=3,
            selected_worker_id="w1",
            cip_vote_count=None,
            cip_quorum_threshold=None,
        )
        assert result.cip_vote_count is None
        assert result.cip_quorum_threshold is None

    def test_map_reduce_with_latency(self):
        result = CIPAggregationResult(
            policy="map_reduce",
            replicas_dispatched=4,
            replicas_responded=4,
            selected_worker_id="worker-mr",
            aggregation_latency_ms=42,
        )
        assert result.aggregation_latency_ms == 42

    def test_none_responded_null_worker(self):
        """selected_worker_id MUST be null when replicas_responded == 0 (S.12 §4.3)."""
        result = CIPAggregationResult(
            policy="best_of_n",
            replicas_dispatched=3,
            replicas_responded=0,
            selected_worker_id=None,
        )
        assert result.selected_worker_id is None

    def test_partial_response_accepted(self):
        """replicas_responded < replicas_dispatched is valid (some workers timed out)."""
        result = CIPAggregationResult(
            policy="best_of_n",
            replicas_dispatched=5,
            replicas_responded=3,
            selected_worker_id="worker-1",
        )
        assert result.replicas_responded == 3
        assert result.replicas_dispatched == 5

    def test_zero_dispatched_zero_responded(self):
        result = CIPAggregationResult(
            policy="best_of_n",
            replicas_dispatched=0,
            replicas_responded=0,
            selected_worker_id=None,
        )
        assert result.replicas_dispatched == 0
        assert result.replicas_responded == 0

    def test_aggregation_latency_zero_accepted(self):
        result = CIPAggregationResult(
            policy="best_of_n",
            replicas_dispatched=1,
            replicas_responded=1,
            selected_worker_id="w1",
            aggregation_latency_ms=0,
        )
        assert result.aggregation_latency_ms == 0

    def test_extra_fields_ignored(self):
        """model_config extra='ignore' — unknown fields silently dropped."""
        result = CIPAggregationResult(
            policy="best_of_n",
            replicas_dispatched=1,
            replicas_responded=1,
            selected_worker_id="w1",
            unknown_future_field="ignored",
        )
        assert not hasattr(result, "unknown_future_field")

    def test_cip_quorum_threshold_minimum_one(self):
        """cip_quorum_threshold ge=1 — value of 1 accepted."""
        result = CIPAggregationResult(
            policy="majority_vote",
            replicas_dispatched=1,
            replicas_responded=1,
            selected_worker_id="w1",
            cip_vote_count=1,
            cip_quorum_threshold=1,
        )
        assert result.cip_quorum_threshold == 1


# ---------------------------------------------------------------------------
# CIP-V06: cross-field invariant — replicas_responded MUST NOT exceed replicas_dispatched
# ---------------------------------------------------------------------------


class TestReplicasRespondedExceedsDispatched:
    def test_responded_exceeds_dispatched_rejected(self):
        """S.12 §4.3 MUST: replicas_responded MUST NOT exceed replicas_dispatched."""
        with pytest.raises(ValidationError, match="replicas_responded"):
            CIPAggregationResult(
                policy="best_of_n",
                replicas_dispatched=2,
                replicas_responded=3,
                selected_worker_id="w1",
            )

    def test_responded_far_exceeds_dispatched_rejected(self):
        with pytest.raises(ValidationError):
            CIPAggregationResult(
                policy="best_of_n",
                replicas_dispatched=1,
                replicas_responded=100,
                selected_worker_id="w1",
            )

    def test_responded_equals_dispatched_accepted(self):
        result = CIPAggregationResult(
            policy="best_of_n",
            replicas_dispatched=3,
            replicas_responded=3,
            selected_worker_id="w1",
        )
        assert result.replicas_responded == result.replicas_dispatched


# ---------------------------------------------------------------------------
# CIP-V06: cross-field invariant — selected_worker_id MUST be null when responded == 0
# ---------------------------------------------------------------------------


class TestSelectedWorkerIdNullWhenNoneResponded:
    def test_worker_id_set_when_responded_zero_rejected(self):
        """S.12 §4.3 MUST: selected_worker_id must be null when replicas_responded == 0."""
        with pytest.raises(ValidationError, match="selected_worker_id must be null"):
            CIPAggregationResult(
                policy="best_of_n",
                replicas_dispatched=3,
                replicas_responded=0,
                selected_worker_id="w1",
            )

    def test_worker_id_null_when_responded_zero_accepted(self):
        result = CIPAggregationResult(
            policy="best_of_n",
            replicas_dispatched=3,
            replicas_responded=0,
            selected_worker_id=None,
        )
        assert result.selected_worker_id is None


# ---------------------------------------------------------------------------
# CIP-V06: field-level constraints
# ---------------------------------------------------------------------------


class TestFieldLevelConstraints:
    def test_replicas_dispatched_negative_rejected(self):
        with pytest.raises(ValidationError):
            CIPAggregationResult(
                policy="best_of_n",
                replicas_dispatched=-1,
                replicas_responded=0,
                selected_worker_id=None,
            )

    def test_replicas_responded_negative_rejected(self):
        with pytest.raises(ValidationError):
            CIPAggregationResult(
                policy="best_of_n",
                replicas_dispatched=3,
                replicas_responded=-1,
                selected_worker_id=None,
            )

    def test_aggregation_latency_negative_rejected(self):
        with pytest.raises(ValidationError):
            CIPAggregationResult(
                policy="best_of_n",
                replicas_dispatched=1,
                replicas_responded=1,
                selected_worker_id="w1",
                aggregation_latency_ms=-1,
            )

    def test_cip_vote_count_negative_rejected(self):
        with pytest.raises(ValidationError):
            CIPAggregationResult(
                policy="majority_vote",
                replicas_dispatched=3,
                replicas_responded=3,
                selected_worker_id="w1",
                cip_vote_count=-1,
                cip_quorum_threshold=2,
            )

    def test_cip_quorum_threshold_zero_rejected(self):
        """cip_quorum_threshold ge=1 — zero is invalid."""
        with pytest.raises(ValidationError):
            CIPAggregationResult(
                policy="majority_vote",
                replicas_dispatched=3,
                replicas_responded=3,
                selected_worker_id="w1",
                cip_vote_count=2,
                cip_quorum_threshold=0,
            )

    def test_missing_policy_rejected(self):
        with pytest.raises(ValidationError):
            CIPAggregationResult(
                replicas_dispatched=1,
                replicas_responded=1,
                selected_worker_id="w1",
            )

    def test_missing_replicas_dispatched_rejected(self):
        with pytest.raises(ValidationError):
            CIPAggregationResult(
                policy="best_of_n",
                replicas_responded=1,
                selected_worker_id="w1",
            )

    def test_missing_replicas_responded_rejected(self):
        with pytest.raises(ValidationError):
            CIPAggregationResult(
                policy="best_of_n",
                replicas_dispatched=1,
                selected_worker_id="w1",
            )
