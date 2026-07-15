from __future__ import annotations

import json
from pathlib import Path

from iicp_client.dispatch_ticket_trust import (
    LocalReplayCache,
    TicketBindings,
    TrustBundle,
    verify_dispatch_ticket_v2,
)


def fixture() -> dict:
    return json.loads((Path(__file__).parents[1] / "parity" / "dispatch-ticket-trust-v2-crypto.json").read_text())


def test_runtime_verifier_consumes_portable_vectors() -> None:
    data = fixture()
    all_keys = {item["key_id"]: item for item in data["keys"]}
    for vector in data["vectors"]:
        bundle = TrustBundle.from_dict({
            "bundle_version": 4,
            "keys": [all_keys[key_id] for key_id in vector["trust_bundle_key_ids"]],
        })
        claims = vector["claims"]
        replay = LocalReplayCache()
        if vector["jti_seen"]:
            replay.remember(claims["jti"], claims["expires_at"])
        decision = verify_dispatch_ticket_v2(
            claims,
            vector["signature_b64url"],
            bundle,
            TicketBindings(claims["issuer"], claims["provider_id"], claims["intent"], claims["constraints_digest"]),
            now=vector["now"],
            minimum_bundle_version=4,
            replay_cache=replay,
        )
        assert decision.code == vector["expected"], vector["id"]


def test_bundle_rollback_and_binding_mismatch_fail_closed() -> None:
    data = fixture()
    vector = data["vectors"][0]
    bundle = TrustBundle.from_dict({"bundle_version": 3, "keys": [data["keys"][0]]})
    bindings = TicketBindings(vector["claims"]["issuer"], "wrong-provider", vector["claims"]["intent"], vector["claims"]["constraints_digest"])
    assert verify_dispatch_ticket_v2(vector["claims"], vector["signature_b64url"], bundle, bindings, now=vector["now"], minimum_bundle_version=4).code == "reject_bundle_rollback"
    bundle = TrustBundle.from_dict({"bundle_version": 4, "keys": [data["keys"][0]]})
    assert verify_dispatch_ticket_v2(vector["claims"], vector["signature_b64url"], bundle, bindings, now=vector["now"]).code == "reject_claim_mismatch"
