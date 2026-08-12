from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from iicp_client.selection import (
    CandidateEvidenceV0,
    RankerDecision,
    RankerRequest,
    _apply_candidate_ranker,
    _ranker_receipt_profile,
)
from iicp_client.types import Node, TaskRequest

FIXTURE = json.loads((Path(__file__).parent / "fixtures/candidate-ranker-v0.json").read_text())
REPLAY_FIXTURE = json.loads((Path(__file__).parent / "fixtures/candidate-ranker-benchmark-replay-v1.json").read_text())


def _node(raw: dict[str, object]) -> Node:
    return Node(
        node_id=str(raw["node_id"]),
        endpoint=str(raw["endpoint"]),
        score=float(raw["directory_score"]),
        available=True,
        region="eu",
        load=float(raw["load"]),
        models=[str(model) for model in raw["models"]],  # type: ignore[union-attr]
        health_label=str(raw["health_label"]),
        directory_observed_reachable=bool(raw["directory_observed_reachable"]),
    )


NODES = {str(raw["node_id"]): _node(raw) for raw in FIXTURE["nodes"]}
REQUEST = TaskRequest(intent=FIXTURE["request"]["intent"], payload={"marker": FIXTURE["request"]["payload_marker"]})


class _FixtureRanker:
    def __init__(self, case: dict[str, object]) -> None:
        self.case = case
        self.observed: tuple[CandidateEvidenceV0, ...] = ()

    def rank(
        self,
        request: RankerRequest,
        candidates: tuple[CandidateEvidenceV0, ...],
    ) -> RankerDecision | None:
        self.observed = candidates
        assert request.request_ref == FIXTURE["request"]["request_ref"]
        assert request.intent == FIXTURE["request"]["intent"]
        assert request.request is REQUEST
        ranker = self.case["ranker"]
        assert isinstance(ranker, dict)
        if ranker["outcome"] == "decline":
            return None
        if ranker["outcome"] == "error":
            raise RuntimeError(str(ranker["message"]))
        return RankerDecision(
            candidate_ref=str(ranker["candidate_ref"]),
            policy_id=str(ranker["policy_id"]),
            mode=str(ranker["mode"]),  # type: ignore[arg-type]
        )


def _run(case: dict[str, object]):
    eligible = [NODES[node_id] for node_id in FIXTURE["eligible_node_ids"]]
    built_in = [NODES[node_id] for node_id in FIXTURE["built_in_order"]]
    ranker = _FixtureRanker(case)
    applied = _apply_candidate_ranker(
        ranker,
        REQUEST,
        FIXTURE["request"]["task_id"],
        eligible,
        built_in,
        3,
    )
    return ranker, applied


@pytest.mark.parametrize(
    "case",
    [case for case in FIXTURE["cases"] if "expected_order" in case],
    ids=lambda case: case["id"],
)
def test_shared_ordering_and_receipt_cases(case: dict[str, object]) -> None:
    ranker, applied = _run(case)
    assert [node.node_id for node in applied.candidates] == case["expected_order"]
    refs_by_id = {raw["node_id"]: raw["candidate_ref"] for raw in FIXTURE["nodes"]}
    expected_refs = {refs_by_id[node_id] for node_id in FIXTURE["eligible_node_ids"]}
    assert {candidate.candidate_ref for candidate in ranker.observed} == expected_refs
    assert len(ranker.observed) == 2
    if applied.decision is None:
        assert case["expected_primary_receipt"] is None
    else:
        assert _ranker_receipt_profile(applied.decision, 0) == case["expected_primary_receipt"]
        assert _ranker_receipt_profile(applied.decision, 1) == case["expected_fallback_receipt"]


@pytest.mark.parametrize(
    "case",
    [case for case in FIXTURE["cases"] if "expected_error_contains" in case],
    ids=lambda case: case["id"],
)
def test_shared_fail_closed_cases(case: dict[str, object]) -> None:
    with pytest.raises(Exception, match=str(case["expected_error_contains"])):
        _run(case)


def test_candidate_evidence_is_redacted_and_ineligible_node_is_absent() -> None:
    ranker, _ = _run(FIXTURE["cases"][0])
    encoded = json.dumps([asdict(candidate) for candidate in ranker.observed], sort_keys=True)
    for forbidden in FIXTURE["excluded_evidence_terms"]:
        assert forbidden not in encoded
    assert [candidate.schema_version for candidate in ranker.observed] == [FIXTURE["evidence_schema"]] * 2
    assert [list(candidate.models) for candidate in ranker.observed] == [["model-a"], ["model-b"]]


class _ReplayRanker:
    def __init__(self, selected_ref: str) -> None:
        self.selected_ref = selected_ref

    def rank(
        self,
        _request: RankerRequest,
        candidates: tuple[CandidateEvidenceV0, ...],
    ) -> RankerDecision:
        assert self.selected_ref in {candidate.candidate_ref for candidate in candidates}
        return RankerDecision(
            candidate_ref=self.selected_ref,
            policy_id=REPLAY_FIXTURE["policy_id"],
            mode=REPLAY_FIXTURE["mode"],
        )


@pytest.mark.parametrize("case", REPLAY_FIXTURE["cases"], ids=lambda case: case["task_id"])
def test_iicp_heterogeneous_benchmark_decisions_replay(case: dict[str, object]) -> None:
    node_definitions = {raw["node_id"]: raw for raw in REPLAY_FIXTURE["nodes"]}
    eligible = [
        Node(
            node_id=node_id,
            endpoint=f"https://{index}.invalid",
            score=1.0,
            available=True,
            region="test",
            load=0.0,
            models=list(node_definitions[node_id]["models"]),
        )
        for index, node_id in enumerate(case["eligible_node_ids"])
    ]
    request = TaskRequest(intent="urn:iicp:intent:llm:chat:v1", payload={"task": case["task_id"]})
    applied = _apply_candidate_ranker(
        _ReplayRanker(str(case["selected_candidate_ref"])),
        request,
        str(case["task_id"]),
        eligible,
        eligible.copy(),
        len(eligible),
    )
    assert applied.candidates[0].node_id == case["selected_node_id"]
    assert _ranker_receipt_profile(applied.decision, 0) == case["expected_primary_receipt"]
    assert _ranker_receipt_profile(applied.decision, 1) == case["expected_fallback_receipt"]
