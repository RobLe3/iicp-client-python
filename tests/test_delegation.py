# ADR-016: IICP client SDK conformance
"""ADR-045 Phase A — operator→node delegation signing (#407)."""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from iicp_client.delegation import (
    canonical_bytes,
    canonical_rename_bytes,
    issue_delegation,
    operator_pub_b64,
    sign_rename,
    verify_delegation,
)

# Cross-language known-answer test — MUST equal the PHP
# OperatorDelegationVerifier::canonicalBytes output for the same inputs, or
# directory verification of SDK-signed delegations silently fails.
KAT = b'{"node_id":"node-kat-1","not_after":1893456000,"operator_pub":"T3BQdWJLZXlCYXNlNjQ="}'

# #460 — rename canonical bytes KAT. MUST equal PHP OperatorController::canonicalBytes
# and the Rust delegation::canonical_rename_bytes for the same inputs (cross-impl rename).
RENAME_KAT = b'{"display_name":"New Name","operator_pub":"T3BQdWI=","ts":1893456000}'


def test_canonical_bytes_matches_cross_language_kat():
    assert canonical_bytes("node-kat-1", "T3BQdWJLZXlCYXNlNjQ=", 1893456000) == KAT


def test_canonical_rename_bytes_matches_cross_language_kat():
    assert canonical_rename_bytes("New Name", "T3BQdWI=", 1893456000) == RENAME_KAT


def test_sign_rename_verifies_with_operator_pubkey():
    # The operator_pub used to sign IS the operator_id (== base64 ed25519 pubkey, #464),
    # so the directory verifies the rename with the very key it stores.
    op = Ed25519PrivateKey.generate()
    pub = operator_pub_b64(op)
    sig = sign_rename(op, "Rebel Two", pub, 1893456000)
    op.public_key().verify(
        base64.b64decode(sig), canonical_rename_bytes("Rebel Two", pub, 1893456000)
    )


def test_issue_then_verify_round_trip():
    op = Ed25519PrivateKey.generate()
    tok = issue_delegation(op, "node-1", ttl_seconds=3600)
    assert tok["node_id"] == "node-1"
    assert tok["operator_pub"] == operator_pub_b64(op)
    assert verify_delegation(tok, "node-1")


def test_verify_rejects_node_id_mismatch():
    op = Ed25519PrivateKey.generate()
    tok = issue_delegation(op, "node-1")
    assert not verify_delegation(tok, "node-evil")


def test_verify_rejects_expired():
    op = Ed25519PrivateKey.generate()
    tok = issue_delegation(op, "node-1", ttl_seconds=-1)
    assert not verify_delegation(tok, "node-1")


def test_verify_rejects_tampered_signature():
    op = Ed25519PrivateKey.generate()
    tok = issue_delegation(op, "node-1")
    tok["not_after"] += 1  # signature no longer covers the bytes
    assert not verify_delegation(tok, "node-1")
