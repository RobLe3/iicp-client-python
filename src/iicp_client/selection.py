"""Client-local candidate selection helpers.

The optional ranker receives only nodes that already passed IICP eligibility.
It can reorder that bounded set but cannot add a provider or perform dispatch.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar

from iicp_client.types import Node, TaskRequest

T = TypeVar("T")
CANDIDATE_EVIDENCE_SCHEMA_V0 = "iicp-candidate-evidence-v0"
RankerMode = Literal["normal", "exploration"]


@dataclass(frozen=True)
class CandidateEvidenceV0:
    """Redacted view of one eligible provider supplied to a local ranker."""

    schema_version: str
    candidate_ref: str
    models: tuple[str, ...]
    directory_score: float
    load: float
    health_label: str | None
    directory_observed_reachable: bool | None


@dataclass(frozen=True)
class RankerRequest:
    """In-process request context; the SDK never serializes it automatically."""

    request_ref: str
    intent: str
    request: TaskRequest


@dataclass(frozen=True)
class RankerDecision:
    candidate_ref: str
    policy_id: str
    mode: RankerMode


class CandidateRanker(Protocol):
    """Optional ranker for an already eligible candidate set."""

    def rank(
        self,
        request: RankerRequest,
        candidates: tuple[CandidateEvidenceV0, ...],
    ) -> RankerDecision | None: ...


@dataclass(frozen=True)
class _AppliedRanker:
    candidates: list[Node]
    decision: RankerDecision | None


def _opaque_ref(domain: str, value: str) -> str:
    return hashlib.sha256(f"iicp:{domain}:v0\n{value}".encode()).hexdigest()


def _candidate_evidence_v0(node: Node) -> CandidateEvidenceV0:
    return CandidateEvidenceV0(
        schema_version=CANDIDATE_EVIDENCE_SCHEMA_V0,
        candidate_ref=_opaque_ref("candidate", node.node_id),
        models=tuple(node.models or ()),
        directory_score=float(node.score),
        load=float(node.load),
        health_label=node.health_label,
        directory_observed_reachable=node.directory_observed_reachable,
    )


def _validate_policy_id(value: str) -> None:
    if not value or len(value) > 64 or any(not (char.isascii() and (char.isalnum() or char in "._-")) for char in value):
        raise ValueError("candidate ranker policy_id must be 1-64 ASCII letters, digits, '.', '_' or '-'")


def _apply_candidate_ranker(
    ranker: CandidateRanker,
    request: TaskRequest,
    task_id: str,
    eligible: list[Node],
    built_in_order: list[Node],
    limit: int,
) -> _AppliedRanker:
    evidence = tuple(_candidate_evidence_v0(node) for node in eligible)
    context = RankerRequest(
        request_ref=_opaque_ref("request", task_id),
        intent=request.intent,
        request=request,
    )
    decision = ranker.rank(context, evidence)
    if decision is None:
        return _AppliedRanker(candidates=built_in_order, decision=None)
    if not isinstance(decision, RankerDecision):
        raise ValueError("candidate ranker returned an invalid decision")
    _validate_policy_id(decision.policy_id)
    if decision.mode not in {"normal", "exploration"}:
        raise ValueError("candidate ranker mode must be normal or exploration")
    selected_index = next(
        (index for index, candidate in enumerate(evidence) if candidate.candidate_ref == decision.candidate_ref),
        None,
    )
    if selected_index is None:
        raise ValueError("candidate ranker selected a reference outside the eligible candidate set")
    selected = eligible[selected_index]
    reordered = [selected]
    reordered.extend(node for node in built_in_order if node.node_id != selected.node_id)
    return _AppliedRanker(candidates=reordered[:limit], decision=decision)


def _ranker_receipt_profile(decision: RankerDecision, selected_candidate_index: int) -> str:
    mode = decision.mode if selected_candidate_index == 0 else "fallback"
    return f"external_ranker/{decision.policy_id}/{mode}"


def weighted_v1_order(
    nodes: list[T],
    max_retries: int,
    random_value: float,
    *,
    top_k: int = 3,
    score=lambda n: n.score,
    load=lambda n: getattr(n, "load", 0.0),
    node_id=lambda n: n.node_id,
) -> list[T]:
    if len(nodes) <= 1:
        return nodes[:max_retries]
    pool = nodes[: max(1, min(len(nodes), top_k))]
    weights = [max(float(score(node)), 0.01) / (1.0 + max(0.0, min(float(load(node)), 1.0))) for node in pool]
    remaining = max(0.0, min(float(random_value), 0.999999999)) * sum(weights)
    chosen = pool[-1]
    for node, weight in zip(pool, weights, strict=False):
        remaining -= weight
        if remaining <= 0:
            chosen = node
            break
    return [chosen, *[node for node in nodes[:max_retries] if node_id(node) != node_id(chosen)]][:max_retries]
