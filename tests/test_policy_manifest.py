import base64
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from iicp_client.identity import OperatorIdentity
from iicp_client.policy_manifest import canonical_manifest, load_and_sign_policy_manifest


def test_policy_manifest_signs_canonical_operator_bound_payload(tmp_path):
    seed = bytes([7]) * 32
    key = Ed25519PrivateKey.from_private_bytes(seed)
    op = OperatorIdentity(
        operator_id=base64.b64encode(key.public_key().public_bytes_raw()).decode(),
        created_at="2026-01-01T00:00:00Z",
        display_name="KAT",
        operator_secret=base64.b64encode(seed).decode(),
    )
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": "1", "jurisdiction": "DE", "retention": {"task_payload": "none"}}))
    manifest = load_and_sign_policy_manifest(path, op, now=datetime(2026, 7, 10, tzinfo=UTC))
    op.signing_key().public_key().verify(
        base64.b64decode(manifest["signature"]["signature"]), canonical_manifest(manifest)
    )
    assert manifest["signature"]["public_key"] == op.operator_id
    assert manifest["signature"]["signature"] == (
        "Horps0SnJ4lenW97Z/vAEEihQ4/ICfBFo//uF4r808FuZzopAXzz2V3vgFXarl1FdPMXwndIo/7qP2/aXMZrAw=="
    )
    assert "operator_secret" not in json.dumps(manifest)

    bad = replace(op, operator_id=base64.b64encode(bytes(32)).decode())
    with pytest.raises(ValueError, match="does not match"):
        load_and_sign_policy_manifest(path, bad, now=datetime(2026, 7, 10, tzinfo=UTC))


def test_pre_normative_profile_fixture_is_complete_and_reasoned():
    fixture = json.loads((Path(__file__).resolve().parents[1] / "parity/profile-compatibility-v0.json").read_text())
    assert fixture["fixture_version"] == "0.3.0-draft"
    assert fixture["status"] == "pre-normative"
    assert fixture["result_contract"]["unsupported_status"] == "unsupported_pre_normative_profile"
    assert len(fixture["scenarios"]) == 11
    assert all(item["expected_reason"] for item in fixture["scenarios"])


def test_profile_fixture_scenarios_use_native_compatibility_evaluator():
    from iicp_client.profile_compatibility import evaluate_pre_normative_profile

    fixture = json.loads((Path(__file__).resolve().parents[1] / "parity/profile-compatibility-v0.json").read_text())
    for scenario in fixture["scenarios"]:
        decision = evaluate_pre_normative_profile(
            scenario["request"], scenario["provider"], fixture["intent_aliases"], scenario.get("now_s", 0),
        )
        assert decision.eligible == (scenario["expected"] == "eligible"), scenario["name"]
        assert decision.reason == scenario["expected_reason"], scenario["name"]


def test_profile_fixture_native_policy_scenarios_use_real_routing_gate():
    from iicp_client.routing_policy import filter_nodes_for_routing_policy
    from iicp_client.types import Node, RoutingPolicy

    fixture = json.loads((Path(__file__).resolve().parents[1] / "parity/profile-compatibility-v0.json").read_text())
    for scenario in fixture["native_policy_scenarios"]:
        raw = scenario["node"]
        node = Node(
            node_id=f"fixture-{scenario['name']}", endpoint="https://node.example.test", score=0.5,
            available=True, region=raw["region"],
            cx_public_key={"algorithm": "X25519", "key": "fixture", "key_id": "fixture"} if raw.get("cx_public_key") else None,
            node_policy_manifest=raw.get("node_policy_manifest"),
        )
        decision = filter_nodes_for_routing_policy([node], RoutingPolicy(**scenario["policy"]))
        assert decision.eligible == []
        assert decision.rejected_reasons == [scenario["expected_reason"]]
