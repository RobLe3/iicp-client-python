import json
from pathlib import Path

from iicp_client.dispatch_ticket import (
    DispatchRouteTicketClaims,
    policy_manifest_binding_matches,
    verify_dispatch_route_ticket,
)


def test_canonical_dispatch_ticket_fixture_verifies():
    fixture = json.loads((Path(__file__).parents[1] / "parity" / "dispatch-route-ticket-v1.json").read_text())
    claims = fixture["valid"]["claims"]
    assert verify_dispatch_route_ticket(
        fixture["valid"]["token"],
        fixture["public_key_hex"],
        claims["iss"],
        claims["node_id"],
        claims["intent"],
        now_s=1_800_000_000,
    )
    assert not verify_dispatch_route_ticket(
        fixture["valid"]["token"] + "0",
        fixture["public_key_hex"],
        claims["iss"],
        claims["node_id"],
        claims["intent"],
        now_s=1_800_000_000,
    )


def test_canonical_dispatch_ticket_vectors_fail_closed():
    fixture = json.loads((Path(__file__).parents[1] / "parity" / "dispatch-route-ticket-v1.json").read_text())
    fixture["valid"]["claims"]
    for vector in fixture["validation_vectors"]:
        token = (
            fixture["valid"]["token"] + ("0" if vector["token"] == "valid+0" else "")
            if vector["token"].startswith("valid")
            else fixture["wrong_audience"]["token"]
            if vector["token"] == "wrong_audience"
            else vector["token"]
        )
        result = verify_dispatch_route_ticket(
            token,
            fixture["public_key_hex"],
            vector["issuer"],
            vector["node_id"],
            vector["intent"],
            now_s=vector["now_s"],
        )
        assert (result is not None) == (vector["expected"] == "valid"), vector["name"]


def test_policy_manifest_binding_is_additive_and_fail_closed_when_present():
    claims = DispatchRouteTicketClaims(
        v=1,
        typ="dispatch-route-ticket",
        iss="https://directory.example",
        aud="iicp.directory.dispatch",
        jti="0" * 24,
        node_id="node",
        intent="urn:iicp:intent:llm:chat:v1",
        iat=1,
        exp=2,
        policy_manifest_sha256="a" * 64,
    )
    matching = {"node_policy_manifest": {"verification": {"canonical_sha256": "a" * 64}}}
    altered = {"node_policy_manifest": {"verification": {"canonical_sha256": "b" * 64}}}
    assert policy_manifest_binding_matches(claims, matching)
    assert not policy_manifest_binding_matches(claims, altered)
    assert not policy_manifest_binding_matches(claims, {})
    assert policy_manifest_binding_matches(
        DispatchRouteTicketClaims(**{**claims.__dict__, "policy_manifest_sha256": None}),
        {},
    )
