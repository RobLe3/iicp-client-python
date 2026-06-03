# ADR-016: IICP client SDK conformance
"""ADR-045 Phase A — operator→node delegation signing (#407)."""

from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from iicp_client.delegation import (
    canonical_bytes,
    issue_delegation,
    operator_pub_b64,
    verify_delegation,
)

# Cross-language known-answer test — MUST equal the PHP
# OperatorDelegationVerifier::canonicalBytes output for the same inputs, or
# directory verification of SDK-signed delegations silently fails.
KAT = b'{"node_id":"node-kat-1","not_after":1893456000,"operator_pub":"T3BQdWJLZXlCYXNlNjQ="}'


def test_canonical_bytes_matches_cross_language_kat():
    assert canonical_bytes("node-kat-1", "T3BQdWJLZXlCYXNlNjQ=", 1893456000) == KAT


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
