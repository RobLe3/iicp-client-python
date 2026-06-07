# SPDX-License-Identifier: Apache-2.0
"""CIP receipt + replay-protection primitives — extracted from coordinator.py (#333 Priority #2a).

Owns the worker→coordinator receipt path:
  - `CIPReceipt` — coordinator-side per-worker result receipt (HMAC-SHA256 signed)
  - `CIPWorkerReceipt` — adapter→directory ledger receipt (forwarded by coordinator)
  - `sign_receipt` / `verify_receipt_signature` — TC-9a signing pair
  - `make_session_key` — §10.4 cip_session_key derivation
  - `ReplayCache` — TC-9b in-memory nonce store with sliding TTL window

Originally lived inside `coordinator.py` (448 lines). Extracted here so the
coordinator file's single concern becomes "dispatch + gate decisions + ledger
forwarding" without the receipt-construction machinery cluttering it.

TC-9a/TC-9b/TC-9c references are to spec/iicp-cooperative-inference.md threat
model and §7 / §10 / ADR-012.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class CIPReceipt:
    """TC-9a/TC-9b: signed result receipt issued by a worker on task completion.

    Workers MUST sign receipts using HMAC-SHA256 keyed with the cip_session_key
    (§10.3, ADR-012). Coordinators MUST call verify_receipt_signature() before
    accepting any receipt. The `nonce` prevents replay attacks (TC-9b).
    """

    task_id: str
    worker_id: str
    tokens_used: int
    credits_charged: float
    issued_at: float                                              # UNIX timestamp
    nonce: str = field(default_factory=lambda: secrets.token_hex(16))  # 32-char hex
    signature: str | None = None  # TC-9a: HMAC-SHA256 hex; None = unsigned (MUST reject)


class ReplayCache:
    """TC-9b in-memory nonce store for CIP receipt replay protection.

    Nonces are retained for `window_seconds` after first observation (default
    300 s = 5 min). Any receipt whose nonce appears in the cache MUST be
    rejected by the coordinator. Thread-safe via threading.Lock.
    """

    def __init__(self, window_seconds: float = 300.0) -> None:
        self._window = window_seconds
        self._seen: dict[str, float] = {}  # nonce → expiry (monotonic)
        self._lock = Lock()

    def is_replay(self, nonce: str) -> bool:
        """Return True if `nonce` was seen within the replay window.

        Side effect: on first observation, marks the nonce as seen and
        stores its expiry. Expired entries are pruned on each call.
        """
        now = time.monotonic()
        with self._lock:
            self._evict(now)
            if nonce in self._seen:
                return True
            self._seen[nonce] = now + self._window
            return False

    def _evict(self, now: float) -> None:
        expired = [n for n, exp in self._seen.items() if exp <= now]
        for n in expired:
            del self._seen[n]


def _receipt_canonical(receipt: CIPReceipt) -> bytes:
    """Return deterministic byte string over all receipt fields for signing."""
    return (
        f"{receipt.task_id}|{receipt.worker_id}|{receipt.tokens_used}"
        f"|{receipt.credits_charged}|{receipt.issued_at}|{receipt.nonce}"
    ).encode()


def sign_receipt(receipt: CIPReceipt, secret: str) -> str:
    """TC-9a: compute HMAC-SHA256 signature over canonical receipt fields.

    Workers call this before sending the receipt; the signature is placed in
    receipt.signature. Secret is the cip_session_key exchanged during dispatch.
    """
    return hmac.new(secret.encode(), _receipt_canonical(receipt), hashlib.sha256).hexdigest()


def verify_receipt_signature(receipt: CIPReceipt, secret: str) -> bool:
    """TC-9a: verify that receipt.signature matches the expected HMAC-SHA256.

    Returns False (reject) if signature is None or does not match. Uses
    hmac.compare_digest to prevent timing side-channels.
    """
    if not receipt.signature:
        return False
    expected = sign_receipt(receipt, secret)
    return hmac.compare_digest(expected, receipt.signature)


def make_session_key(task_id: str) -> str:
    """§10.4: cip_session_key derived from task_id + per-session random salt.

    Each call produces a unique key — salt is freshly generated per dispatch.
    Workers MUST echo this key; coordinator MUST discard responses that omit it.
    """
    salt = secrets.token_bytes(16)
    return hashlib.sha256(task_id.encode() + salt).hexdigest()


@dataclass
class CIPWorkerReceipt:
    """Receipt from a remote CIP worker (adapter), forwarded to the directory ledger.

    The adapter signs this with its node_hmac_key (TC-9c); the directory validates
    the signature. The coordinator cannot verify the HMAC directly (it has no access
    to node_hmac_key) but MUST check session binding and replay status before forwarding.

    Phase 5: response_hash is the SHA-256 of the canonical result JSON. The coordinator
    MUST verify this hash against the received result before forwarding the award.
    """

    task_id: str
    worker_node_id: str
    tokens_used: int
    nonce: str
    signature: str
    issued_at: str
    expires_at: str | None = None  # required by CreditsController.php; issued_at + 300s
    cip_session_key: str | None = None
    cip_parent_task_id: str | None = None
    response_hash: str | None = None  # SHA-256 hex of canonical result JSON (TC-9c Phase 5)

    @classmethod
    def from_dict(cls, d: dict) -> CIPWorkerReceipt:
        return cls(
            task_id=d["task_id"],
            worker_node_id=d["worker_node_id"],
            tokens_used=int(d["tokens_used"]),
            nonce=d["nonce"],
            signature=d["signature"],
            issued_at=d["issued_at"],
            expires_at=d.get("expires_at"),
            cip_session_key=d.get("cip_session_key"),
            cip_parent_task_id=d.get("cip_parent_task_id"),
            response_hash=d.get("response_hash"),
        )
