"""
Replica response signature verification (P6-4.2b, S.13 v0.3.6 §6.5 / DIR-FED-20).

Verifies the `X-IICP-Replica-Sig` Ed25519 signature on discovery responses
from replica directories. The canonical signing input is:

    SHA256_bin( method + ":" + path + ":" + query_canonical + ":" + snapshot_seq + ":" + SHA256_hex(response_body) )

Where `query_canonical` is the query string with parameters sorted by name
(URL-encoded, no leading `?`). Signature is hex-encoded (128 chars).

Public keys come from the replica's DID document (`/.well-known/did.json`),
fetched + cached separately by the caller. This module is a pure verifier:
no IO, no caching — give it the bytes and the key, get a bool back.
"""
from __future__ import annotations

import hashlib
import logging
from urllib.parse import parse_qsl, quote

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

logger = logging.getLogger(__name__)


def canonicalize_query(query: str) -> str:
    """Sort query parameters by name + URL-encode. Empty string for empty query.

    Per S.13 §6.5: parameters MUST be sorted by name so client + server agree
    on the canonical signing input. Falls back to lexicographic sort on value
    for repeated keys.
    """
    if not query:
        return ""
    pairs = parse_qsl(query, keep_blank_values=True)
    pairs.sort(key=lambda kv: (kv[0], kv[1]))
    # RFC 3986 percent-encoding (matches PHP rawurlencode); spaces → %20, not '+'.
    # Must match the server-side encoder in
    # directory/app/Http/Middleware/SignReplicaResponse.php::canonicalizeQuery
    return "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in pairs)


def signing_input(
    method: str,
    path: str,
    query: str,
    snapshot_seq: int | str,
    response_body: bytes,
) -> bytes:
    """Build the canonical signing input per S.13 §6.5. Returns 32-byte SHA256."""
    body_hash_hex = hashlib.sha256(response_body).hexdigest()
    canonical = ":".join([
        method.upper(),
        path,
        canonicalize_query(query),
        str(snapshot_seq),
        body_hash_hex,
    ])
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def verify_replica_sig(
    method: str,
    path: str,
    query: str,
    snapshot_seq: int | str,
    response_body: bytes,
    sig_hex: str,
    pub_key_raw: bytes,
) -> bool:
    """Verify an Ed25519 signature on a replica discovery response.

    Returns True iff the signature is valid for the given inputs and pubkey.
    Any malformed input (bad hex, wrong length, invalid pubkey) returns False
    without raising — caller logs and rejects.
    """
    if not isinstance(sig_hex, str) or len(sig_hex) != 128:
        return False
    try:
        sig_bytes = bytes.fromhex(sig_hex)
    except ValueError:
        return False
    if len(sig_bytes) != 64:
        return False
    if not isinstance(pub_key_raw, bytes) or len(pub_key_raw) != 32:
        return False
    try:
        pub_key = Ed25519PublicKey.from_public_bytes(pub_key_raw)
    except Exception:  # noqa: BLE001 — cryptography may raise UnsupportedAlgorithm
        return False
    message = signing_input(method, path, query, snapshot_seq, response_body)
    try:
        pub_key.verify(sig_bytes, message)
        return True
    except InvalidSignature:
        return False
