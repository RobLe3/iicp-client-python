"""Tests for replica_sig_verifier — P6-4.2b verifier helper."""
from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from iicp_client.proxy.clients.replica_sig_verifier import (
    canonicalize_query,
    signing_input,
    verify_replica_sig,
)


def _new_keypair() -> tuple[Ed25519PrivateKey, bytes]:
    priv = Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return priv, pub_raw


def _sign(priv: Ed25519PrivateKey, message: bytes) -> str:
    return priv.sign(message).hex()


# --- canonicalize_query ---------------------------------------------------

def test_canonicalize_query_empty():
    assert canonicalize_query("") == ""


def test_canonicalize_query_single_param():
    assert canonicalize_query("intent=urn:iicp:foo") == "intent=urn%3Aiicp%3Afoo"


def test_canonicalize_query_spaces_use_percent20_not_plus():
    # RFC 3986 percent-encoding — must match PHP rawurlencode (space → %20)
    assert canonicalize_query("q=hello+world") == "q=hello%20world"


def test_canonicalize_query_sorts_by_name():
    # Out-of-order input → sorted output
    assert canonicalize_query("z=1&a=2&m=3") == "a=2&m=3&z=1"


def test_canonicalize_query_handles_repeated_keys():
    # Repeated keys sorted by value secondarily
    canonical = canonicalize_query("k=b&k=a&k=c")
    assert canonical == "k=a&k=b&k=c"


# --- signing_input --------------------------------------------------------

def test_signing_input_deterministic():
    body = b'{"nodes":[]}'
    a = signing_input("GET", "/v1/discover", "intent=foo", 42, body)
    b = signing_input("GET", "/v1/discover", "intent=foo", 42, body)
    assert a == b
    assert len(a) == 32


def test_signing_input_method_case_insensitive():
    body = b"{}"
    upper = signing_input("GET", "/v1/discover", "", 1, body)
    lower = signing_input("get", "/v1/discover", "", 1, body)
    assert upper == lower


def test_signing_input_query_order_does_not_matter():
    body = b"{}"
    a = signing_input("GET", "/v1/discover", "a=1&b=2", 1, body)
    b = signing_input("GET", "/v1/discover", "b=2&a=1", 1, body)
    assert a == b, "canonicalization MUST normalize query order"


def test_signing_input_body_tamper_changes_hash():
    a = signing_input("GET", "/v1/discover", "", 1, b'{"nodes":[]}')
    b = signing_input("GET", "/v1/discover", "", 1, b'{"nodes":[{"node_id":"bad"}]}')
    assert a != b


# --- verify_replica_sig ---------------------------------------------------

def test_verify_valid_signature():
    priv, pub = _new_keypair()
    body = b'{"nodes":[{"node_id":"n1"}],"count":1}'
    message = signing_input("GET", "/v1/discover", "intent=urn:iicp:foo", 42, body)
    sig = _sign(priv, message)

    assert verify_replica_sig(
        "GET", "/v1/discover", "intent=urn:iicp:foo", 42, body, sig, pub
    ) is True


def test_verify_rejects_tampered_body():
    priv, pub = _new_keypair()
    body = b'{"nodes":[{"node_id":"n1"}]}'
    message = signing_input("GET", "/v1/discover", "", 42, body)
    sig = _sign(priv, message)
    # Body mutated after signing → must fail
    tampered = b'{"nodes":[{"node_id":"attacker"}]}'
    assert verify_replica_sig("GET", "/v1/discover", "", 42, tampered, sig, pub) is False


def test_verify_rejects_wrong_pubkey():
    priv, _ = _new_keypair()
    _, other_pub = _new_keypair()
    body = b"{}"
    message = signing_input("GET", "/v1/discover", "", 1, body)
    sig = _sign(priv, message)
    assert verify_replica_sig("GET", "/v1/discover", "", 1, body, sig, other_pub) is False


def test_verify_rejects_wrong_path():
    priv, pub = _new_keypair()
    body = b"{}"
    sig = _sign(priv, signing_input("GET", "/v1/discover", "", 1, body))
    # Path replayed onto a different endpoint MUST not validate
    assert verify_replica_sig("GET", "/v1/node/abc", "", 1, body, sig, pub) is False


def test_verify_rejects_wrong_snapshot_seq():
    priv, pub = _new_keypair()
    body = b"{}"
    sig = _sign(priv, signing_input("GET", "/v1/discover", "", 42, body))
    # snapshot_seq replay attack — old sig MUST not validate against new seq
    assert verify_replica_sig("GET", "/v1/discover", "", 99, body, sig, pub) is False


def test_verify_rejects_malformed_sig_hex():
    _, pub = _new_keypair()
    assert verify_replica_sig("GET", "/", "", 1, b"", "not-hex", pub) is False
    assert verify_replica_sig("GET", "/", "", 1, b"", "deadbeef", pub) is False  # too short
    assert verify_replica_sig("GET", "/", "", 1, b"", "z" * 128, pub) is False  # bad hex


def test_verify_rejects_malformed_pubkey():
    priv, _ = _new_keypair()
    body = b"{}"
    sig = _sign(priv, signing_input("GET", "/v1/discover", "", 1, body))
    assert verify_replica_sig("GET", "/v1/discover", "", 1, body, sig, b"") is False
    assert verify_replica_sig("GET", "/v1/discover", "", 1, body, sig, b"\x00" * 16) is False


def test_verify_query_order_round_trip():
    """Critical: server signs with query in one order; client receives query
    in the same order over the wire but MUST canonicalize both sides identically."""
    priv, pub = _new_keypair()
    body = b'{"nodes":[]}'
    # Server signs with the canonicalized form
    server_query = "a=1&b=2&c=3"
    sig = _sign(priv, signing_input("GET", "/v1/discover", server_query, 1, body))
    # Client receives query in different lexical order — must still verify
    client_query = "c=3&a=1&b=2"
    assert verify_replica_sig("GET", "/v1/discover", client_query, 1, body, sig, pub) is True
