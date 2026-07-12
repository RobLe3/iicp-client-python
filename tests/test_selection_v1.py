import json
from pathlib import Path

from iicp_client.selection import weighted_v1_order


def test_weighted_v1_fixture_vectors_are_deterministic():
    fixture = json.loads((Path(__file__).parents[1] / "parity" / "selection-v1.json").read_text())
    for vector in fixture["vectors"]:
        order = weighted_v1_order(
            vector["nodes"], 3, vector["random"],
            score=lambda node: node["score"], load=lambda node: node["load"], node_id=lambda node: node["node_id"],
        )
        assert [node["node_id"] for node in order] == vector["expected_order"], vector["name"]
