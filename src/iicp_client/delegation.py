# ADR-016: IICP client SDK conformance
"""ADR-045 Phase A — operator→node delegation (#407 / #2).

A fleet operator holds an ed25519 keypair and issues a compact, offline-verifiable
token asserting `node:<id>` is operated by `<operator_pubkey>` until `<not_after>`.
The node attaches it in its REGISTER payload; any federated directory verifies it
locally against the operator public key (no phone-home). Proven in research #406.

Uses the existing `cryptography` dependency (no new dep). The CANONICAL signing
bytes MUST be byte-identical to the directory verifier
(`OperatorDelegationVerifier::canonicalBytes`, PHP) and every other SDK signer:
key-sorted, no-whitespace JSON, unescaped slashes/unicode. This is pinned by a
cross-language known-answer test (KAT).
"""

from __future__ import annotations

import base64
import json
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# Canonical field order is alphabetical (node_id < not_after < operator_pub) so
# sort_keys yields the spec/PHP byte form. Do NOT reorder without re-pinning the KAT.


def canonical_bytes(node_id: str, operator_pub_b64: str, not_after: int) -> bytes:
    """Exact bytes the operator signs / the directory verifies. Must match the
    PHP `OperatorDelegationVerifier::canonicalBytes` byte-for-byte."""
    return json.dumps(
        {"node_id": node_id, "not_after": not_after, "operator_pub": operator_pub_b64},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def canonical_rename_bytes(display_name: str, operator_pub_b64: str, ts: int) -> bytes:
    """#460 — exact bytes the operator signs to rename their public ``display_name``.
    Key-sorted (display_name < operator_pub < ts), no whitespace, unescaped
    slashes/unicode. MUST be byte-identical to the directory's
    ``OperatorController::canonicalBytes`` (PHP) / ``delegation::canonical_rename_bytes``
    (Rust) and every other SDK signer. Do NOT reorder without re-pinning the KAT."""
    return json.dumps(
        {"display_name": display_name, "operator_pub": operator_pub_b64, "ts": ts},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sign_rename(
    operator_key: Ed25519PrivateKey, display_name: str, operator_pub_b64: str, ts: int
) -> str:
    """#460 — operator signs a display_name rename; returns base64 of the ed25519 signature.
    Only the operator key-holder can produce this, so the directory authenticates the
    mutation by the signature alone (no node token)."""
    sig = operator_key.sign(canonical_rename_bytes(display_name, operator_pub_b64, ts))
    return base64.b64encode(sig).decode()


def canonical_operator_self_service_bytes(action: str, fields: dict) -> bytes:
    """Canonical #599/#609 operator self-service challenge bytes.

    ``fields`` includes ``operator_pub``, ``nonce``, ``ts`` and action-specific
    values, but never ``sig``. The action is inserted and all top-level keys are
    sorted exactly as the directory verifier does.
    """
    payload = {"action": action, **{k: v for k, v in fields.items() if k != "sig"}}
    return b"iicp:operator:self-service:v1\n" + json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sign_operator_self_service(
    operator_key: Ed25519PrivateKey, action: str, fields: dict
) -> str:
    """Sign an operator acceptance or DSR request without exposing the key."""
    return base64.b64encode(
        operator_key.sign(canonical_operator_self_service_bytes(action, fields))
    ).decode()


def operator_pub_b64(private_key: Ed25519PrivateKey) -> str:
    """Base64 of the operator's 32-byte ed25519 public key (as the directory stores)."""
    return base64.b64encode(private_key.public_key().public_bytes_raw()).decode()


def issue_delegation(operator_key: Ed25519PrivateKey, node_id: str, ttl_seconds: int = 3600) -> dict:
    """Operator (offline) signs a delegation for one node. Short TTL is the
    revocation baseline (ADR-045 OPEN-3 C)."""
    pub = operator_pub_b64(operator_key)
    not_after = int(time.time()) + ttl_seconds
    sig = operator_key.sign(canonical_bytes(node_id, pub, not_after))
    return {
        "node_id": node_id,
        "operator_pub": pub,
        "not_after": not_after,
        "sig": base64.b64encode(sig).decode(),
    }


def verify_delegation(token: dict, claimed_node_id: str, now: int | None = None) -> bool:
    """Local self-consistency check (signature + node binding + expiry). The
    DIRECTORY is the authority on operator trust; this helps a node sanity-check
    its own token before sending. Returns True only if all checks pass."""
    now = int(time.time()) if now is None else now
    try:
        if token.get("node_id") != claimed_node_id:
            return False
        if now >= int(token["not_after"]):
            return False
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(token["operator_pub"]))
        pub.verify(
            base64.b64decode(token["sig"]),
            canonical_bytes(token["node_id"], token["operator_pub"], int(token["not_after"])),
        )
        return True
    except (KeyError, ValueError, InvalidSignature, Exception):  # noqa: BLE001
        return False
