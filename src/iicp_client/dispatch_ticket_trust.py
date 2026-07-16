"""Opt-in verifier for the pre-normative dispatch-ticket trust v2 profile.

This module deliberately does not alter the default v1 same-origin ticket path.
Applications opt in by supplying an independently obtained trust bundle.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
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


def canonical_trust_bundle(bundle: TrustBundle) -> bytes:
    data: dict[str, Any] = {
        "bundle_version": bundle.bundle_version,
        "keys": [
            {
                "key_id": key.key_id,
                "public_key_b64url": key.public_key_b64url,
                "state": key.state,
                "valid_from": key.valid_from,
                "valid_until": key.valid_until,
                "allowed_profiles": sorted(key.allowed_profiles),
                "issuers": sorted(key.issuers),
                "audiences": sorted(key.audiences),
            }
            for key in sorted(bundle.keys.values(), key=lambda item: item.key_id)
        ],
    }
    if bundle.issuer is not None:
        data["issuer"] = bundle.issuer
    if bundle.valid_from is not None:
        data["valid_from"] = bundle.valid_from
    if bundle.valid_until is not None:
        data["valid_until"] = bundle.valid_until
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class TrustBundleStoreError(RuntimeError):
    """Base error for the opt-in local trust store."""


class TrustBundleStoreCorrupt(TrustBundleStoreError):
    pass


class TrustBundleStoreLocked(TrustBundleStoreError):
    pass


@dataclass(frozen=True)
class StoredTrustBundle:
    bundle: TrustBundle
    canonical_bytes: bytes
    digest: str
    high_water: int


@dataclass(frozen=True)
class TrustBundleInstallResult:
    status: str
    state: StoredTrustBundle | None


@dataclass(frozen=True)
class AdminRecoveryAuthorization:
    reason: str
    minimum_high_water: int = 0


class FileTrustBundleStore:
    """Owner-local atomic trust bundle store; never enabled by default dispatch."""

    def __init__(self, path: str | Path, *, lock_timeout_s: float = 2.0) -> None:
        self.path = Path(path).expanduser()
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.lock_timeout_s = max(0.0, lock_timeout_s)

    def _prepare_directory(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        mode = stat.S_IMODE(self.path.parent.stat().st_mode)
        if mode & 0o077:
            raise TrustBundleStoreError("trust store directory must be owner-only")

    def _acquire_lock(self) -> int:
        self._prepare_directory()
        deadline = time.monotonic() + self.lock_timeout_s
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, f"{os.getpid()}\n".encode())
                os.fsync(fd)
                return fd
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise TrustBundleStoreLocked("trust store lock is held") from None
                time.sleep(0.01)

    def _release_lock(self, fd: int) -> None:
        os.close(fd)
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass

    def load(self) -> StoredTrustBundle | None:
        if not self.path.exists():
            return None
        metadata = self.path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise TrustBundleStoreCorrupt("trust store must be a regular file, not a link")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise TrustBundleStoreCorrupt("trust store file must be owner-only")
        try:
            raw = self.path.read_bytes()
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError("state exceeds size limit")
            state = json.loads(raw)
            canonical_bytes = base64.b64decode(state["bundle_b64"], validate=True)
            digest = "sha256:" + hashlib.sha256(canonical_bytes).hexdigest()
            if digest != state["bundle_digest"]:
                raise ValueError("bundle digest mismatch")
            bundle = TrustBundle.from_dict(json.loads(canonical_bytes))
            high_water = state["high_water"]
            stored_version = state["bundle_version"]
            if (
                not isinstance(high_water, int)
                or isinstance(high_water, bool)
                or not isinstance(stored_version, int)
                or isinstance(stored_version, bool)
                or high_water < 0
                or bundle.bundle_version < 0
                or stored_version != bundle.bundle_version
                or high_water < bundle.bundle_version
            ):
                raise ValueError("bundle version/high-water mismatch")
            if canonical_trust_bundle(bundle) != canonical_bytes:
                raise ValueError("bundle is not canonical")
            return StoredTrustBundle(bundle, canonical_bytes, digest, high_water)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TrustBundleStoreCorrupt(f"invalid trust store: {exc}") from exc

    def _commit(self, bundle: TrustBundle, high_water: int) -> StoredTrustBundle:
        if (
            not isinstance(bundle.bundle_version, int)
            or isinstance(bundle.bundle_version, bool)
            or bundle.bundle_version < 0
            or not isinstance(high_water, int)
            or isinstance(high_water, bool)
            or high_water < bundle.bundle_version
        ):
            raise TrustBundleStoreError("bundle version and high-water mark must be non-negative integers")
        canonical_bytes = canonical_trust_bundle(bundle)
        digest = "sha256:" + hashlib.sha256(canonical_bytes).hexdigest()
        state = {
            "bundle_b64": base64.b64encode(canonical_bytes).decode("ascii"),
            "bundle_digest": digest,
            "bundle_version": bundle.bundle_version,
            "high_water": high_water,
        }
        payload = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
        with NamedTemporaryFile(dir=self.path.parent, prefix=self.path.name + ".tmp-", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            os.fchmod(tmp.fileno(), 0o600)
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        try:
            os.replace(tmp_path, self.path)
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
        return StoredTrustBundle(bundle, canonical_bytes, digest, high_water)

    def install(
        self, bundle: TrustBundle, *, expected_current_version: int | None = None
    ) -> TrustBundleInstallResult:
        if (
            not isinstance(bundle.bundle_version, int)
            or isinstance(bundle.bundle_version, bool)
            or bundle.bundle_version < 0
        ):
            raise TrustBundleStoreError("bundle version must be a non-negative integer")
        fd = self._acquire_lock()
        try:
            current = self.load()
            current_version = current.bundle.bundle_version if current else None
            if expected_current_version != current_version and expected_current_version is not None:
                return TrustBundleInstallResult("conflict", current)
            candidate_bytes = canonical_trust_bundle(bundle)
            candidate_digest = "sha256:" + hashlib.sha256(candidate_bytes).hexdigest()
            high_water = current.high_water if current else 0
            if bundle.bundle_version < high_water:
                return TrustBundleInstallResult("stale", current)
            if current and bundle.bundle_version == current_version:
                status = "unchanged" if candidate_digest == current.digest else "conflict"
                return TrustBundleInstallResult(status, current)
            installed = self._commit(bundle, max(high_water, bundle.bundle_version))
            return TrustBundleInstallResult("installed", installed)
        finally:
            self._release_lock(fd)

    def recover(
        self, bundle: TrustBundle, authorization: AdminRecoveryAuthorization | None
    ) -> TrustBundleInstallResult:
        if (
            not isinstance(bundle.bundle_version, int)
            or isinstance(bundle.bundle_version, bool)
            or bundle.bundle_version < 0
        ):
            raise TrustBundleStoreError("bundle version must be a non-negative integer")
        if authorization is None or not authorization.reason.strip():
            return TrustBundleInstallResult("recovery_required", None)
        fd = self._acquire_lock()
        try:
            try:
                current = self.load()
            except TrustBundleStoreCorrupt:
                current = None
            high_water = max(
                bundle.bundle_version,
                authorization.minimum_high_water,
                current.high_water if current else 0,
            )
            return TrustBundleInstallResult("recovered", self._commit(bundle, high_water))
        finally:
            self._release_lock(fd)


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
