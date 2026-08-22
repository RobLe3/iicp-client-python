# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from iicp_client.restricted_membership import (
    MembershipPolicy,
    MembershipRefusal,
    verify_gossip,
    verify_membership,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures/restricted-trust-domain-membership-v0.json").read_text())
POLICY = MembershipPolicy(
    domain_id="domain-test-a",
    authority_id="did:iicp:test:directory-a",
    authority_key_id="did:iicp:test:directory-a#key-1",
    authority_public_key_ed25519=FIXTURE["authority_public_key_ed25519"],
    minimum_generation=7,
    maximum_clock_skew_seconds=60,
)


def refusal(call) -> str:
    with pytest.raises(MembershipRefusal) as caught:
        call()
    return caught.value.code


def test_membership_vectors_match_shared_fixture() -> None:
    fixture_path = Path(__file__).parent / "fixtures/restricted-trust-domain-membership-v0.json"
    assert hashlib.sha256(fixture_path.read_bytes()).hexdigest() == "78cb70b19dabed5be0175555cf2b4bb123dd4bc77ce36b67b745f311f3d941d4"
    for vector in FIXTURE["vectors"]:
        def call(vector=vector) -> None:
            verify_membership(vector["envelope"], POLICY, "did:iicp:test:node-a", "peers", 1_800_000_100)

        if vector["expected"] == "valid":
            call()
        else:
            assert refusal(call) == "membership_domain_mismatch"


def test_gossip_vectors_match_shared_fixture() -> None:
    for vector in FIXTURE["gossip_vectors"]:
        def call(vector=vector) -> None:
            verify_gossip(
                vector["gossip"],
                vector["membership"],
                POLICY,
                vector["payload_utf8"].encode(),
                1_800_000_010,
                replay_seen=bool(vector.get("seen_replay_ids")),
            )
        if vector["expected"] == "valid":
            call()
        else:
            assert refusal(call) == {"replay_detected": "gossip_replay"}[vector["expected"]]


def test_lifecycle_and_scope_refusals_are_bounded() -> None:
    valid = FIXTURE["vectors"][0]["envelope"]
    assert refusal(lambda: verify_membership(valid, POLICY, "other", "peers", 1_800_000_100)) == "membership_subject_mismatch"
    assert refusal(lambda: verify_membership(valid, POLICY, "did:iicp:test:node-a", "missing", 1_800_000_100)) == "membership_scope_missing"
    assert refusal(lambda: verify_membership(valid, POLICY, "did:iicp:test:node-a", "peers", 1_800_000_300)) == "membership_expired"
