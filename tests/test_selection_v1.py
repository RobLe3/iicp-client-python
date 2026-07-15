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


def test_weighted_v1_distribution_and_top_k_boundary():
    fixture = json.loads((Path(__file__).parents[1] / "parity" / "selection-v1.json").read_text())
    for vector in fixture["distribution_vectors"]:
        counts = {node["node_id"]: 0 for node in vector["nodes"]}
        for index in range(vector["sample_count"]):
            order = weighted_v1_order(
                vector["nodes"],
                len(vector["nodes"]),
                (index + 0.5) / vector["sample_count"],
                top_k=vector["top_k"],
                score=lambda node: node["score"],
                load=lambda node: node["load"],
                node_id=lambda node: node["node_id"],
            )
            counts[order[0]["node_id"]] += 1
        assert counts == vector["expected_first_counts"], vector["name"]
