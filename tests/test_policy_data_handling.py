import json
from pathlib import Path

from iicp_client.policy_data_handling import evaluate_policy_data_handling

FIXTURE = json.loads((Path(__file__).parents[1] / "parity/policy-data-handling-v0.json").read_text())

def test_shared_policy_data_handling_vectors():
    for case in FIXTURE["cases"]:
        decision = evaluate_policy_data_handling(case["requirement"], case["declaration"], case.get("context"))
        assert decision.reason == case["expected"], case["id"]
        assert decision.eligible is (case["expected"] == "compatible"), case["id"]

def test_unknown_optional_requirement_does_not_weaken_known_hard_gate():
    decision = evaluate_policy_data_handling(
        {"version":"0-draft","data_class":"public","remote_routing":"local_only","future_hint":"x"},
        {"version":"0-draft","accepted_data_classes":["public"]},
    )
    assert decision.reason == "remote_routing_forbidden"
