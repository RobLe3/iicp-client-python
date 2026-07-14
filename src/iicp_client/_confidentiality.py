# SPDX-License-Identifier: Apache-2.0
"""IICP-CX S.16 Tier-1 confidentiality: X25519-HKDF-SHA256 + AES-256-GCM.

CX-Consumer side encrypts task payloads for nodes that advertise cx_public_key.
CX-Provider side exposes key helpers and decrypts incoming iicp_conf envelopes.

Requires: cryptography>=42  (install as: pip install iicp-client[cx])
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
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


def _cx_key_dir() -> Path:
    if os.environ.get("IICP_CX_KEY_DIR"):
        return Path(os.environ["IICP_CX_KEY_DIR"]).expanduser()
    base = Path(os.environ.get("IICP_HOME", Path.home() / ".iicp")).expanduser()
    return base / "cx"


def _cx_key_path(node_id: str, endpoint: str = "") -> Path:
    stable_name = node_id or endpoint or "default"
    digest = hashlib.sha256(stable_name.encode("utf-8")).hexdigest()[:24]
    return _cx_key_dir() / f"{digest}.json"


def _public_key_from_private(private_key_bytes: bytes) -> dict[str, str]:
    _require_cryptography()
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    priv = X25519PrivateKey.from_private_bytes(private_key_bytes)
    pub_bytes = priv.public_key().public_bytes_raw()
    key_id = "cx-" + hashlib.sha256(pub_bytes).hexdigest()[:16]
    return {
        "algorithm": "X25519",
        "encoding": "base64url",
        "key": _b64url_encode(pub_bytes),
        "key_id": key_id,
    }


def load_or_create_node_cx_key(node_id: str, endpoint: str = "") -> tuple[dict[str, str], bytes]:
    """Load or create the provider node's persistent CX X25519 key.

    The private key stays local under ``$IICP_CX_KEY_DIR`` or ``$IICP_HOME/cx``.
    The returned public half is safe to advertise as ``cx_public_key``.
    """
    _require_cryptography()
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    path = _cx_key_path(node_id, endpoint)
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        private_key_bytes = _b64url_decode(str(data["private_key"]))
        return _public_key_from_private(private_key_bytes), private_key_bytes

    path.parent.mkdir(parents=True, exist_ok=True)
    priv = X25519PrivateKey.generate()
    private_key_bytes = priv.private_bytes_raw()
    public_key = _public_key_from_private(private_key_bytes)
    payload = {
        "version": 1,
        "algorithm": "X25519",
        "private_key": _b64url_encode(private_key_bytes),
        "public_key": public_key,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return public_key, private_key_bytes


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
    envelope, _shared_secret = encrypt_payload_with_context(payload, cx_public_key, task_id, intent)
    return envelope


def encrypt_payload_with_context(
    payload: dict[str, Any],
    cx_public_key: dict[str, Any],
    task_id: str,
    intent: str,
) -> tuple[dict[str, Any], bytes]:
    """Encrypt a request and retain the session secret for response decryption."""
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
    }, shared_secret


def decrypt_payload(iicp_conf: dict[str, Any], private_key_bytes: bytes) -> dict[str, Any]:
    """Decrypt an iicp_conf envelope (CX-Provider / adapter side).

    Args:
        iicp_conf: the iicp_conf dict from the incoming task body
        private_key_bytes: raw 32-byte X25519 private key

    Returns:
        decrypted payload dict
    """
    payload, _shared_secret = decrypt_payload_with_context(iicp_conf, private_key_bytes)
    return payload


def decrypt_payload_with_context(
    iicp_conf: dict[str, Any], private_key_bytes: bytes
) -> tuple[dict[str, Any], bytes]:
    """Decrypt a request and retain the session secret for response encryption."""
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
    return json.loads(plaintext), shared_secret


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
