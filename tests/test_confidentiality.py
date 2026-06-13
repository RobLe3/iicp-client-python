"""Tests for IICP-CX S.16 Tier-1 confidentiality (X25519-HKDF-SHA256 + AES-256-GCM)."""
from __future__ import annotations

import base64

import pytest

from iicp_client._confidentiality import decrypt_payload, encrypt_payload


def _generate_test_keypair():
    """Generate a fresh X25519 keypair for testing."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    priv = X25519PrivateKey.generate()
    pub_bytes = priv.public_key().public_bytes_raw()
    priv_bytes = priv.private_bytes_raw()

    key_id = pub_bytes[:8].hex()
    pub_b64 = base64.urlsafe_b64encode(pub_bytes).rstrip(b"=").decode()

    cx_public_key = {"algorithm": "X25519", "key": pub_b64, "key_id": key_id}
    return cx_public_key, priv_bytes


def test_encrypt_returns_iicp_conf_envelope():
    """encrypt_payload produces a valid iicp_conf dict with all required fields."""
    cx_public_key, _ = _generate_test_keypair()
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    env = encrypt_payload(payload, cx_public_key, task_id="task-001", intent="urn:iicp:intent:llm:chat:v1")

    assert env["version"] == 1
    assert env["recipient_key_id"] == cx_public_key["key_id"]
    assert "kem_ciphertext" in env
    assert "encrypted_body" in env
    assert "nonce" in env
    assert "aad" in env
    assert env["plaintext_size"] > 0


def test_encrypt_decrypt_roundtrip():
    """Encrypted payload decrypts back to the original payload."""
    cx_public_key, priv_bytes = _generate_test_keypair()
    payload = {"messages": [{"role": "user", "content": "hello world"}]}
    task_id = "task-123"
    intent = "urn:iicp:intent:llm:chat:v1"

    env = encrypt_payload(payload, cx_public_key, task_id, intent)
    recovered = decrypt_payload(env, priv_bytes)

    assert recovered == payload


def test_different_nonce_each_call():
    """Each encrypt call produces a unique nonce (no nonce reuse)."""
    cx_public_key, _ = _generate_test_keypair()
    payload = {"x": 1}
    env1 = encrypt_payload(payload, cx_public_key, "t1", "urn:iicp:intent:llm:chat:v1")
    env2 = encrypt_payload(payload, cx_public_key, "t1", "urn:iicp:intent:llm:chat:v1")
    assert env1["nonce"] != env2["nonce"]


def test_wrong_key_decrypt_fails():
    """Decrypting with a different private key raises an error."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    cx_public_key, _ = _generate_test_keypair()
    wrong_priv = X25519PrivateKey.generate().private_bytes_raw()

    env = encrypt_payload({"x": 1}, cx_public_key, "t1", "urn:iicp:intent:llm:chat:v1")
    with pytest.raises((InvalidTag, ValueError)):
        decrypt_payload(env, wrong_priv)


def test_tampered_ciphertext_fails():
    """A tampered encrypted_body raises an error (GCM authentication tag check)."""
    from cryptography.exceptions import InvalidTag

    cx_public_key, priv_bytes = _generate_test_keypair()
    env = encrypt_payload({"x": 1}, cx_public_key, "t1", "urn:iicp:intent:llm:chat:v1")

    tampered = env.copy()
    raw = base64.urlsafe_b64decode(env["encrypted_body"] + "==")
    raw = bytes([raw[0] ^ 0xFF]) + raw[1:]
    tampered["encrypted_body"] = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    with pytest.raises((InvalidTag, ValueError)):
        decrypt_payload(tampered, priv_bytes)


def test_unsupported_algorithm_raises():
    """Unsupported cx_public_key algorithm raises ValueError."""
    bad_key = {"algorithm": "RSA", "key": "abc", "key_id": "00000000"}
    with pytest.raises(ValueError, match="Unsupported"):
        encrypt_payload({}, bad_key, "t1", "urn:iicp:intent:llm:chat:v1")


def test_response_roundtrip():
    """Tier-2 §5a.3: response sealing round-trips under a shared secret."""
    import os

    from iicp_client._confidentiality import decrypt_response, encrypt_response

    shared = os.urandom(32)
    resp = {"choices": [{"message": {"role": "assistant", "content": "answer"}}]}
    env = encrypt_response(resp, shared, "task-resp-1")
    assert set(env) == {"version", "nonce", "encrypted_body"}
    assert decrypt_response(env, shared, "task-resp-1") == resp
