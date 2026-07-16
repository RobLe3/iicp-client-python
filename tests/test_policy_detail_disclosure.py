import json
from pathlib import Path

from iicp_client.policy_detail_disclosure import (
    ALLOWED_DETAIL_FIELDS,
    evaluate_policy_detail_disclosure,
    verify_policy_detail_consumer_token,
)

FIXTURE = json.loads((Path(__file__).parents[1] / "parity/policy-detail-disclosure-v0.json").read_text())


def test_policy_detail_disclosure_fixture() -> None:
    assert tuple(FIXTURE["allowed_detail_fields"]) == ALLOWED_DETAIL_FIELDS
    for case in FIXTURE["cases"]:
        decision = evaluate_policy_detail_disclosure(case["context"])
        assert decision.status == case["expected"]["status"], case["id"]
        assert decision.reason == case["expected"]["reason"], case["id"]
        if decision.status == 200:
            assert decision.body is not None
            assert set(decision.body["details"]) <= set(ALLOWED_DETAIL_FIELDS)
            serialized = json.dumps(decision.body)
            for forbidden in ("must-not-leak", "private.example", "backend_topology", "natural_person_contact"):
                assert forbidden not in serialized


def test_unrecognized_auth_state_fails_as_invalid() -> None:
    decision = evaluate_policy_detail_disclosure({"consumer_auth": "self_asserted", "disclosure_allowed": True})
    assert (decision.status, decision.reason) == (401, "consumer_auth_invalid")


def test_consumer_token_crypto_vectors() -> None:
    vector = FIXTURE["crypto_vectors"]
    args = (
        vector["public_key_hex"],
        vector["expected_target_node_id"],
        vector["expected_intent"],
        vector["evaluated_at_unix"],
    )
    status, claims = verify_policy_detail_consumer_token(vector["valid_consumer_token"], *args)
    assert status == "valid" and claims and claims["sub"] == vector["expected_subject"]
    assert verify_policy_detail_consumer_token(vector["expired_consumer_token"], *args)[0] == "expired"
    assert verify_policy_detail_consumer_token(vector["tampered_consumer_token"], *args)[0] == "invalid"
