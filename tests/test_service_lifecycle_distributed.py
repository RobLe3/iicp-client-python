import json
from pathlib import Path

from iicp_client.service_lifecycle_distributed import evaluate_distributed_lifecycle

FIXTURE = json.loads((Path(__file__).parents[1] / "parity/service-lifecycle-distributed-v1.json").read_text())


def test_distributed_lifecycle_fixture() -> None:
    for vector in FIXTURE["vectors"]:
        assert evaluate_distributed_lifecycle(vector) == vector["expected"], vector["id"]
