# SPDX-License-Identifier: Apache-2.0
"""IICP-CX S.16 Tier-1 confidentiality: X25519-HKDF-SHA256 + AES-256-GCM.

CX-Consumer side only — encrypts task payloads for nodes that advertise cx_public_key.
No decryption here (that is the CX-Provider / adapter side).

Requires: cryptography>=42  (install as: pip install iicp-client[cx])
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any


def _require_cryptography() -> Any:
    try:
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey  # noqa: F401
        return True
    except ImportError as exc:
        raise ImportError(
            "IICP-CX encryption requires the 'cryptography' package. "
            "Install with: pip install iicp-client[cx]"
        ) from exc


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - (len(s) % 4)
    if padding < 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def encrypt_payload(
    payload: dict[str, Any],
    cx_public_key: dict[str, str],
    task_id: str,
    intent: str,
) -> dict[str, Any]:
    """Encrypt task payload for a CX-Provider node (IICP-CX S.16 §5, Tier 1).

    Returns an iicp_conf envelope dict. The caller should include this dict in
    the task body as `iicp_conf` and omit the `payload` field.

    Args:
        payload: the original task payload dict
        cx_public_key: node's cx_public_key dict (algorithm, key, key_id)
        task_id: the task UUID string
        intent: the intent URN string

    Returns:
        dict matching the iicp_conf wire format (§4.1)
    """
    _require_cryptography()
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey,
        X25519PublicKey,
    )
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    if cx_public_key.get("algorithm") != "X25519":
        raise ValueError(f"Unsupported cx_public_key algorithm: {cx_public_key.get('algorithm')}")

    node_pub_bytes = _b64url_decode(cx_public_key["key"])
    node_pub = X25519PublicKey.from_public_bytes(node_pub_bytes)

    ephem_priv = X25519PrivateKey.generate()
    shared_secret = ephem_priv.exchange(node_pub)

    nonce = os.urandom(12)
    info = f"IICP-CX-v1{task_id}{intent}".encode()
    key = HKDF(algorithm=SHA256(), length=32, salt=nonce, info=info).derive(shared_secret)

    payload_json = json.dumps(payload, separators=(",", ":")).encode()
    aad = f"{task_id}|{intent}".encode()
    ciphertext = AESGCM(key).encrypt(nonce, payload_json, aad)

    ephem_pub_bytes = ephem_priv.public_key().public_bytes_raw()

    return {
        "version": 1,
        "recipient_key_id": cx_public_key["key_id"],
        "kem_ciphertext": _b64url_encode(ephem_pub_bytes),
        "encrypted_body": _b64url_encode(ciphertext),
        "nonce": _b64url_encode(nonce),
        "aad": _b64url_encode(aad),
        "plaintext_size": len(payload_json),
    }


def decrypt_payload(iicp_conf: dict[str, Any], private_key_bytes: bytes) -> dict[str, Any]:
    """Decrypt an iicp_conf envelope (CX-Provider / adapter side).

    Args:
        iicp_conf: the iicp_conf dict from the incoming task body
        private_key_bytes: raw 32-byte X25519 private key

    Returns:
        decrypted payload dict
    """
    _require_cryptography()
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey,
        X25519PublicKey,
    )
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    priv_key = X25519PrivateKey.from_private_bytes(private_key_bytes)
    ephem_pub = X25519PublicKey.from_public_bytes(_b64url_decode(iicp_conf["kem_ciphertext"]))
    shared_secret = priv_key.exchange(ephem_pub)

    nonce = _b64url_decode(iicp_conf["nonce"])
    aad_bytes = _b64url_decode(iicp_conf["aad"])
    aad_str = aad_bytes.decode()
    task_id, intent = aad_str.split("|", 1)

    info = f"IICP-CX-v1{task_id}{intent}".encode()
    key = HKDF(algorithm=SHA256(), length=32, salt=nonce, info=info).derive(shared_secret)

    plaintext = AESGCM(key).decrypt(nonce, _b64url_decode(iicp_conf["encrypted_body"]), aad_bytes)
    return json.loads(plaintext)


# ── Tier-2 §5a.3: bidirectional (response) encryption ────────────────────────
# Byte-identical to the adapter (CX-Provider) so a node's encrypt_response interops
# with this decrypt_response. Sealed under the request's session shared secret with a
# distinct HKDF label so request/response keys differ. Pure primitives; wiring later.
_RESP_INFO_PREFIX = b"IICP-CX-RESP-v1"


def encrypt_response(response: dict[str, Any], shared_secret: bytes, task_id: str) -> dict[str, Any]:
    """Seal a RESPONSE under the request's session shared secret (IICP-CX §5a.3)."""
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    nonce = os.urandom(12)
    key = HKDF(algorithm=SHA256(), length=32, salt=nonce, info=_RESP_INFO_PREFIX + task_id.encode()).derive(
        shared_secret
    )
    aad = (task_id + "|resp").encode()
    ciphertext = AESGCM(key).encrypt(nonce, json.dumps(response).encode(), aad)
    return {"version": 1, "nonce": _b64url_encode(nonce), "encrypted_body": _b64url_encode(ciphertext)}


def decrypt_response(iicp_conf_resp: dict[str, Any], shared_secret: bytes, task_id: str) -> dict[str, Any]:
    """Open a node's encrypted RESPONSE (CX-Consumer side, IICP-CX §5a.3)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    missing = {"nonce", "encrypted_body"} - set(iicp_conf_resp)
    if missing:
        raise ValueError(f"iicp_conf_resp missing fields: {missing}")
    nonce = _b64url_decode(iicp_conf_resp["nonce"])
    key = HKDF(algorithm=SHA256(), length=32, salt=nonce, info=_RESP_INFO_PREFIX + task_id.encode()).derive(
        shared_secret
    )
    aad = (task_id + "|resp").encode()
    plaintext = AESGCM(key).decrypt(nonce, _b64url_decode(iicp_conf_resp["encrypted_body"]), aad)
    return json.loads(plaintext)
