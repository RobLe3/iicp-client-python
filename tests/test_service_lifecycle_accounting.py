import json
from pathlib import Path

from iicp_client.service_lifecycle_accounting import decide_lifecycle_accounting


def test_service_lifecycle_accounting_fixture() -> None:
    fixture = json.loads(
        (Path(__file__).parents[1] / "parity/service-lifecycle-accounting-v1.json").read_text()
    )
    for case in fixture["cases"]:
        decision = decide_lifecycle_accounting(case["input"])
        assert decision.__dict__ == case["expected"], case["id"]


def test_invalid_input_fails_closed() -> None:
    decision = decide_lifecycle_accounting({"operation": "settle"})
    assert decision.decision == "reject_invalid_input"
    assert decision.new_execution is False
