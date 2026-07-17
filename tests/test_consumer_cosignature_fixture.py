from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from iicp_client.jcs import canonicalize_jcs

FIXTURE = Path(__file__).parents[1] / "parity/cip-consumer-cosignature-v1.json"
DOMAIN = b"IICP-CIP-CONSUMER-COSIGNATURE-V1\x00"


def decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def evaluate(value: dict[str, str]) -> dict[str, str]:
    if value["binding"] != "match":
        reason = {
            "response_hash_mismatch": "response_hash_mismatch",
            "cost_mismatch": "cost_mismatch",
            "task_node_intent_mismatch": "receipt_binding_mismatch",
        }[value["binding"]]
        return {"action": "refuse_signing", "reason": reason, "trust_weight": "0.0"}
    if value["consumer_key"] == "revoked":
        return {"action": "reject", "reason": "consumer_key_revoked", "trust_weight": "0.0"}
    if value["consumer_key"] == "rotated_outside_validity":
        return {"action": "reject", "reason": "consumer_key_not_valid_at_completion", "trust_weight": "0.0"}
    if value["time"] != "valid":
        return {"action": "reject", "reason": "receipt_expired", "trust_weight": "0.0"}
    if value["nonce"] != "fresh":
        return {"action": "reject", "reason": "dispatch_nonce_replayed", "trust_weight": "0.0"}
    if value["provider_signature"] != "valid":
        return {"action": "reject", "reason": "provider_signature_invalid", "trust_weight": "0.0"}
    if value["consumer_signature"] != "valid":
        if value["consumer_signature"] == "missing" and value["mode"] == "optional":
            return {"action": "accept_legacy", "reason": "consumer_signature_missing_optional", "trust_weight": "0.0"}
        reason = "consumer_signature_required" if value["consumer_signature"] == "missing" else "consumer_signature_invalid"
        return {"action": "reject", "reason": reason, "trust_weight": "0.0"}
    if value["relationship"] == "same_node":
        return {"action": "exclude", "reason": "self_node", "trust_weight": "0.0"}
    if value["relationship"] == "same_operator":
        return {"action": "exclude", "reason": "self_operator", "trust_weight": "0.0"}
    return {"action": "accept", "reason": "cosignature_verified", "trust_weight": "1.0"}


def test_consumer_cosignature_fixture() -> None:
    fixture = json.loads(FIXTURE.read_text())
    vector = fixture["canonical_vector"]
    encoded = canonicalize_jcs(vector["receipt"])
    assert encoded.decode() == vector["canonical_json_utf8"]
    assert hashlib.sha256(encoded).hexdigest() == vector["canonical_json_sha256"]
    digest = hashlib.sha256(DOMAIN + encoded).digest()
    assert digest.hex() == vector["receipt_digest_hex"]
    for role in ("provider", "consumer"):
        Ed25519PublicKey.from_public_bytes(decode(vector[f"{role}_public_key_b64url"])).verify(
            decode(vector[f"{role}_signature_b64url"]), digest
        )
    for case in fixture["conformance_cases"]:
        assert evaluate(case["input"]) == case["expected"], case["name"]
    for case in fixture["settlement_cases"]:
        value = case["input"]
        if value["reservation"] != "held":
            actual = {"action": "refuse_dispatch", "awards": 0, "debits": 0}
        elif value["outcome"] in {"timeout", "cancelled", "partial"}:
            actual = {"action": "release", "awards": 0, "debits": 0}
        else:
            actual = {"action": "settle_once", "awards": 1, "debits": 1}
        assert actual == case["expected"], case["name"]

    receipt_fields = set(vector["receipt"])
    assert receipt_fields.isdisjoint(fixture["privacy_contract"]["forbidden_fields"])
    assert fixture["privacy_contract"]["self_reported_metrics_have_authority"] is False


def test_full_jcs_vectors_and_invalid_number_domain() -> None:
    fixture = json.loads(FIXTURE.read_text())
    for vector in fixture["jcs_vectors"]:
        assert canonicalize_jcs(vector["input"]).decode() == vector["canonical_json_utf8"], vector["name"]

    for invalid in (float("nan"), float("inf"), 9_007_199_254_740_992):
        try:
            canonicalize_jcs({"invalid": invalid})
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError(f"invalid JCS number accepted: {invalid!r}")
