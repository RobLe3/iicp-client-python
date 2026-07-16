import json
from pathlib import Path

from iicp_client.policy_operational_evidence import evaluate_policy_operational_evidence

FIXTURE = json.loads((Path(__file__).parents[1] / "parity/policy-operational-evidence-v0.json").read_text())


def test_policy_operational_evidence_fixture() -> None:
    for case in FIXTURE["cases"]:
        decision = evaluate_policy_operational_evidence(case["requirement"], case["context"], FIXTURE["evaluated_at"])
        assert decision.reason == case["expected"], case["id"]
        assert decision.eligible is (case["expected"] == "compatible"), case["id"]
