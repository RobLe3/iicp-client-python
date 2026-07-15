from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _canonical(claims: dict) -> bytes:
    return json.dumps(claims, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _decision(vector: dict, keys: dict[str, dict], signature_valid: bool) -> str:
    key_id = vector["claims"]["key_id"]
    if key_id not in vector["trust_bundle_key_ids"]:
        return "reject_unknown_key"
    key = keys[key_id]
    if key["state"] == "revoked":
        return "reject_key_revoked"
    if not key["valid_from"] <= vector["now"] <= key["valid_until"]:
        return "reject_key_expired"
    if not signature_valid:
        return "reject_signature"
    if vector["jti_seen"]:
        return "reject_local_replay"
    return "accept_anchored"


def test_dispatch_ticket_v2_signed_vectors_are_portable() -> None:
    fixture = json.loads((Path(__file__).parents[1] / "parity" / "dispatch-ticket-trust-v2-crypto.json").read_text())
    domain = _decode(fixture["domain_separator_b64url"])
    keys = {key["key_id"]: key for key in fixture["keys"]}
    for vector in fixture["vectors"]:
        key = keys[vector["claims"]["key_id"]]
        public_key = Ed25519PublicKey.from_public_bytes(_decode(key["public_key_b64url"]))
        try:
            public_key.verify(
                _decode(vector["signature_b64url"]),
                domain + _canonical(vector["claims"]),
            )
            signature_valid = True
        except InvalidSignature:
            signature_valid = False
        assert signature_valid is vector["expected_signature_valid"], vector["id"]
        assert _decision(vector, keys, signature_valid) == vector["expected"], vector["id"]
