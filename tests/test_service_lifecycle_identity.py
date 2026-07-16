import json
from pathlib import Path

from iicp_client.service_lifecycle_identity import evaluate_lifecycle_identity

FIXTURE = json.loads((Path(__file__).parents[1] / "parity/service-lifecycle-identity-v1.json").read_text())


def test_lifecycle_identity_fixture() -> None:
    for case in FIXTURE["cases"]:
        assert evaluate_lifecycle_identity(case, FIXTURE["audit_retention_seconds"]) == case["expected"], case["id"]
