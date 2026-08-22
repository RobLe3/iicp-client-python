# SPDX-License-Identifier: Apache-2.0
"""Restricted trust-domain membership and gossip verification.

This is a direct projection of the shared pre-normative IICP fixture. Directory
bearer credentials are deliberately outside this module and must not enter peer
gossip.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from dataclasses import dataclass
from typing import Any, cast

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MEMBERSHIP_SCHEMA = "iicp.restricted-trust-domain.membership-assertion.v0"
RESTRICTED_PROFILE = "urn:iicp:profile:restricted-trust-domain:v1"
_MEMBERSHIP_DOMAIN = b"IICP-RTD-MEMBERSHIP-V0\n"
_GOSSIP_DOMAIN = b"IICP-RTD-GOSSIP-V0\n"


class MembershipRefusal(ValueError):
    """Bounded refusal safe for cross-SDK comparison."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class MembershipPolicy:
    domain_id: str
    authority_id: str
    authority_key_id: str
    authority_public_key_ed25519: str
    minimum_generation: int
    maximum_clock_skew_seconds: int


def _refuse(code: str) -> None:
    raise MembershipRefusal(code)


def _b64url(value: Any, length: int) -> bytes:
    if not isinstance(value, str):
        _refuse("membership_malformed")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        _refuse("membership_malformed")
    if len(raw) != length or base64.urlsafe_b64encode(raw).decode().rstrip("=") != value:
        _refuse("membership_malformed")
    return raw


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        _refuse("membership_malformed")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        _refuse("membership_malformed")


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_shape(envelope: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    _exact_keys(envelope, {"assertion", "signature"})
    assertion = _mapping(envelope.get("assertion"))
    signature = _mapping(envelope.get("signature"))
    _exact_keys(
        assertion,
        {
            "schema", "profile", "assertion_id", "domain_id", "subject", "issuer",
            "issued_at", "expires_at", "generation", "scopes", "audience",
        },
    )
    subject = _mapping(assertion.get("subject"))
    issuer = _mapping(assertion.get("issuer"))
    _exact_keys(subject, {"kind", "id", "key_id", "public_key_ed25519"})
    _exact_keys(issuer, {"id", "key_id"})
    if set(signature) not in ({"algorithm", "value"}, {"algorithm", "key_id", "value"}):
        _refuse("membership_malformed")
    try:
        uuid.UUID(str(assertion.get("assertion_id")))
    except (ValueError, TypeError, AttributeError):
        _refuse("membership_malformed")
    if not all(_nonempty(value) for value in (
        assertion.get("domain_id"), subject.get("id"), subject.get("key_id"),
        issuer.get("id"), issuer.get("key_id"),
    )):
        _refuse("membership_malformed")
    if not all(_integer(assertion.get(key)) for key in ("issued_at", "expires_at", "generation")):
        _refuse("membership_malformed")
    scopes = assertion.get("scopes")
    audience = assertion.get("audience")
    if (
        not isinstance(scopes, list) or not scopes or not all(_nonempty(item) for item in scopes)
        or not isinstance(audience, list) or not audience or not all(_nonempty(item) for item in audience)
        or assertion["expires_at"] <= assertion["issued_at"]
    ):
        _refuse("membership_malformed")
    return assertion, signature


def verify_membership(
    envelope: dict[str, Any],
    policy: MembershipPolicy,
    expected_subject: str,
    required_scope: str,
    now: int,
) -> None:
    assertion, signature = _validate_shape(_mapping(envelope))
    subject = assertion["subject"]
    issuer = assertion["issuer"]
    if (
        assertion["schema"] != MEMBERSHIP_SCHEMA
        or assertion["profile"] != RESTRICTED_PROFILE
        or signature.get("algorithm") != "Ed25519"
    ):
        _refuse("membership_unsupported")
    if issuer["id"] != policy.authority_id or issuer["key_id"] != policy.authority_key_id:
        _refuse("membership_authority_invalid")
    if assertion["domain_id"] != policy.domain_id or policy.domain_id not in assertion["audience"]:
        _refuse("membership_domain_mismatch")
    if subject["kind"] != "node" or subject["id"] != expected_subject:
        _refuse("membership_subject_mismatch")
    if assertion["issued_at"] > now + policy.maximum_clock_skew_seconds:
        _refuse("membership_not_yet_valid")
    if assertion["expires_at"] <= now:
        _refuse("membership_expired")
    if assertion["generation"] < policy.minimum_generation:
        _refuse("membership_generation_revoked")
    if required_scope not in assertion["scopes"]:
        _refuse("membership_scope_missing")
    try:
        key = Ed25519PublicKey.from_public_bytes(_b64url(policy.authority_public_key_ed25519, 32))
        key.verify(
            _b64url(signature.get("value"), 64),
            _MEMBERSHIP_DOMAIN + rfc8785.dumps(assertion),
        )
    except InvalidSignature:
        _refuse("membership_signature_invalid")


def verify_gossip(
    gossip: dict[str, Any],
    membership: dict[str, Any],
    policy: MembershipPolicy,
    payload: bytes,
    now: int,
    *,
    replay_seen: bool = False,
) -> None:
    gossip = _mapping(gossip)
    _exact_keys(gossip, {"proof", "signature"})
    proof = _mapping(gossip.get("proof"))
    signature = _mapping(gossip.get("signature"))
    _exact_keys(
        proof,
        {"sender_id", "domain_id", "sent_at", "replay_id", "payload_sha256", "membership_assertion_id"},
    )
    _exact_keys(signature, {"algorithm", "key_id", "value"})
    verify_membership(membership, policy, str(proof.get("sender_id", "")), "peers", now)
    try:
        uuid.UUID(str(proof.get("replay_id")))
    except (ValueError, TypeError, AttributeError):
        _refuse("membership_malformed")
    assertion = membership["assertion"]
    if signature.get("algorithm") != "Ed25519" or signature.get("key_id") != assertion["subject"]["key_id"]:
        _refuse("membership_unsupported")
    if proof.get("domain_id") != policy.domain_id:
        _refuse("membership_domain_mismatch")
    if proof.get("membership_assertion_id") != assertion["assertion_id"]:
        _refuse("membership_subject_mismatch")
    if replay_seen:
        _refuse("gossip_replay")
    sent_at_value = proof.get("sent_at")
    if not _integer(sent_at_value):
        _refuse("gossip_stale")
    sent_at = cast(int, sent_at_value)
    if sent_at > now + policy.maximum_clock_skew_seconds or now - sent_at > policy.maximum_clock_skew_seconds:
        _refuse("gossip_stale")
    if proof.get("payload_sha256") != hashlib.sha256(payload).hexdigest():
        _refuse("gossip_payload_mismatch")
    try:
        key = Ed25519PublicKey.from_public_bytes(_b64url(assertion["subject"]["public_key_ed25519"], 32))
        key.verify(_b64url(signature.get("value"), 64), _GOSSIP_DOMAIN + rfc8785.dumps(proof))
    except InvalidSignature:
        _refuse("membership_signature_invalid")
