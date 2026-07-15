"""Opt-in verifier for the pre-normative dispatch-ticket trust v2 profile.

This module deliberately does not alter the default v1 same-origin ticket path.
Applications opt in by supplying an independently obtained trust bundle.
"""

from __future__ import annotations

import base64
import json
import threading
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PROFILE = "dispatch_ticket_v2"
DOMAIN = b"IICP-DISPATCH-TICKET-V2\0"


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def canonical_claims(claims: dict[str, Any]) -> bytes:
    return json.dumps(claims, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


@dataclass(frozen=True)
class TrustKey:
    key_id: str
    public_key_b64url: str
    state: str
    valid_from: int
    valid_until: int
    allowed_profiles: frozenset[str] = field(default_factory=lambda: frozenset({PROFILE}))
    issuers: frozenset[str] = field(default_factory=frozenset)
    audiences: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class TrustBundle:
    bundle_version: int
    keys: dict[str, TrustKey]
    issuer: str | None = None
    valid_from: int | None = None
    valid_until: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrustBundle:
        keys: dict[str, TrustKey] = {}
        for item in data.get("keys", []):
            key_id = str(item["key_id"])
            if key_id in keys:
                raise ValueError(f"duplicate trust key: {key_id}")
            state = str(item["state"])
            if state not in {"active", "retiring", "revoked"}:
                raise ValueError(f"invalid trust key state: {state}")
            keys[key_id] = TrustKey(
                key_id=key_id,
                public_key_b64url=str(item["public_key_b64url"]),
                state=state,
                valid_from=int(item["valid_from"]),
                valid_until=int(item["valid_until"]),
                allowed_profiles=frozenset(item.get("allowed_profiles", [PROFILE])),
                issuers=frozenset(item.get("issuers", [])),
                audiences=frozenset(item.get("audiences", [])),
            )
        return cls(
            bundle_version=int(data["bundle_version"]),
            keys=keys,
            issuer=data.get("issuer"),
            valid_from=data.get("valid_from"),
            valid_until=data.get("valid_until"),
        )


@dataclass(frozen=True)
class TicketBindings:
    issuer: str
    provider_id: str
    intent: str
    constraints_digest: str
    audience: str | None = None


@dataclass(frozen=True)
class TrustDecision:
    accepted: bool
    code: str
    anchored: bool = False
    key_id: str | None = None


class LocalReplayCache:
    def __init__(self) -> None:
        self._seen: dict[str, int] = {}
        self._lock = threading.Lock()

    def contains(self, jti: str, now: int) -> bool:
        with self._lock:
            self._seen = {key: expiry for key, expiry in self._seen.items() if expiry > now}
            return jti in self._seen

    def remember(self, jti: str, expires_at: int) -> None:
        with self._lock:
            self._seen[jti] = expires_at


def verify_dispatch_ticket_v2(
    claims: dict[str, Any],
    signature_b64url: str,
    bundle: TrustBundle,
    bindings: TicketBindings,
    *,
    now: int,
    minimum_bundle_version: int = 0,
    replay_cache: LocalReplayCache | None = None,
) -> TrustDecision:
    """Verify an already parsed v2 claims object against caller-controlled trust."""

    if bundle.bundle_version < minimum_bundle_version:
        return TrustDecision(False, "reject_bundle_rollback")
    if bundle.valid_from is not None and now < bundle.valid_from:
        return TrustDecision(False, "reject_bundle_not_yet_valid")
    if bundle.valid_until is not None and now > bundle.valid_until:
        return TrustDecision(False, "reject_bundle_expired")
    required = {"profile", "issuer", "provider_id", "intent", "expires_at", "jti", "constraints_digest", "key_id"}
    if not required.issubset(claims) or claims.get("profile") != PROFILE:
        return TrustDecision(False, "reject_required_profile_downgrade")
    key_id = claims.get("key_id")
    if not isinstance(key_id, str) or key_id not in bundle.keys:
        return TrustDecision(False, "reject_unknown_key", key_id=key_id if isinstance(key_id, str) else None)
    key = bundle.keys[key_id]
    if key.state == "revoked":
        return TrustDecision(False, "reject_key_revoked", key_id=key_id)
    if not key.valid_from <= now <= key.valid_until:
        return TrustDecision(False, "reject_key_expired", key_id=key_id)
    if PROFILE not in key.allowed_profiles:
        return TrustDecision(False, "reject_profile_not_allowed", key_id=key_id)
    if key.issuers and claims["issuer"] not in key.issuers:
        return TrustDecision(False, "reject_issuer", key_id=key_id)
    if key.audiences and claims.get("audience") not in key.audiences:
        return TrustDecision(False, "reject_audience", key_id=key_id)
    expected = {
        "issuer": bindings.issuer,
        "provider_id": bindings.provider_id,
        "intent": bindings.intent,
        "constraints_digest": bindings.constraints_digest,
    }
    if any(claims.get(name) != value for name, value in expected.items()):
        return TrustDecision(False, "reject_claim_mismatch", key_id=key_id)
    if bindings.audience is not None and claims.get("audience") != bindings.audience:
        return TrustDecision(False, "reject_claim_mismatch", key_id=key_id)
    try:
        expires_at = int(claims["expires_at"])
        jti = claims["jti"]
        if expires_at <= now or not isinstance(jti, str) or not jti:
            return TrustDecision(False, "reject_claim_mismatch", key_id=key_id)
        Ed25519PublicKey.from_public_bytes(_decode(key.public_key_b64url)).verify(
            _decode(signature_b64url), DOMAIN + canonical_claims(claims)
        )
    except (InvalidSignature, ValueError, TypeError):
        return TrustDecision(False, "reject_signature", key_id=key_id)
    if replay_cache is not None and replay_cache.contains(jti, now):
        return TrustDecision(False, "reject_local_replay", key_id=key_id)
    if replay_cache is not None:
        replay_cache.remember(jti, expires_at)
    return TrustDecision(True, "accept_anchored", anchored=True, key_id=key_id)
